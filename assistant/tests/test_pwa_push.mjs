import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const worker = readFileSync(new URL('../src/assistant/pwa/sw.js', import.meta.url), 'utf8')
  .replace('"__AGENT_NAME__"', JSON.stringify('Juniper'));

test('push displays the agent name and reply and sets the badge', async () => {
  const handlers = {};
  let notification, badge;
  vm.runInNewContext(worker, {
    self: {
      addEventListener: (name, handler) => { handlers[name] = handler; },
      registration: { showNotification: async (title, options) => { notification = { title, ...options }; } },
      navigator: { setAppBadge: async count => { badge = count; } },
    },
  });
  let pending;
  // Objects built inside the worker's vm context have a foreign Object prototype,
  // which strict deepEqual rejects; compare their JSON instead.
  const data = () => JSON.parse(JSON.stringify(notification.data));
  handlers.push({
    data: { json: () => ({ body: 'Hello there! This is the test reminder', agent_name: 'Cedar', unread: 3 }) },
    waitUntil: promise => { pending = promise; },
  });
  await pending;
  assert.equal(notification.title, 'Cedar');
  assert.equal(notification.body, 'Hello there! This is the test reminder');
  // The test button's push belongs to no thread; the click still opens the chat.
  assert.deepEqual(data(), { thread: null });
  assert.equal(badge, 3);
  badge = undefined;

  // A reply's push names its thread, so the click can open it in reply mode.
  // The title stays the agent name: threads have no name of their own.
  handlers.push({
    data: { json: () => ({ body: 'Recorded.', agent_name: 'Cedar', thread: 'reply:u1', unread: 1 }) },
    waitUntil: promise => { pending = promise; },
  });
  await pending;
  assert.equal(notification.title, 'Cedar');
  assert.equal(notification.body, 'Recorded.');
  assert.deepEqual(data(), { thread: 'reply:u1' });
  assert.equal(badge, 1);
  badge = undefined;

  // A thread that is not a non-empty string is no thread at all.
  for (const thread of ['', 7, {id: 'x'}, null]) {
    handlers.push({ data: { json: () => ({ body: 'Hi', thread }) }, waitUntil: promise => { pending = promise; } });
    await pending;
    assert.deepEqual(data(), { thread: null }, String(thread));
  }

  // Previously queued pushes have no body; malformed pushes still display safely.
  for (const source of [{ json: () => ({}) }, { json: () => { throw Error('bad JSON'); } }]) {
    handlers.push({ data: source, waitUntil: promise => { pending = promise; } });
    await pending;
    assert.equal(notification.title, 'Juniper');
    assert.equal(notification.body, 'You have a new update. Open Juniper to read it.');
    assert.deepEqual(data(), { thread: null });
    assert.equal(badge, undefined);
  }
});

test('worker activation requires an explicit update message', async () => {
  const handlers = {};
  let activations = 0;
  vm.runInNewContext(worker, { self: {
    addEventListener: (name, handler) => { handlers[name] = handler; },
    skipWaiting: async () => { activations++; },
  } });
  assert.equal(activations, 0);
  handlers.message({data: {type: 'unrelated'}});
  assert.equal(activations, 0);
  let pending;
  handlers.message({data: {type: 'ACTIVATE_UPDATE'}, waitUntil: promise => { pending = promise; }});
  await pending;
  assert.equal(activations, 1);
});

test('worker serves installed shell without network and never handles API data', async () => {
  const handlers = {};
  const cached = {body: 'installed shell'};
  let network = 0, matched;
  vm.runInNewContext(worker, {
    URL,
    self: {location: {origin: 'https://nox.test'}, addEventListener: (name, handler) => { handlers[name] = handler; }},
    caches: {open: async () => ({match: async path => { matched = path; return cached; }})},
    fetch: async () => { network++; },
  });
  let response;
  handlers.fetch({request: {url: 'https://nox.test/?launch=1', method: 'GET'}, respondWith: promise => { response = promise; }});
  assert.equal(await response, cached);
  assert.equal(matched, '/');
  assert.equal(network, 0);
  for (const path of ['/api/session', '/api/messages', '/api/now']) {
    handlers.fetch({request: {url: 'https://nox.test' + path, method: 'GET'}, respondWith: () => assert.fail('Private response intercepted')});
  }
});

function loadClickWorker(clients, {openWindow} = {}) {
  const handlers = {};
  const opened = [];
  vm.runInNewContext(worker, {
    URL,
    self: {
      location: {origin: 'https://nox.test'},
      addEventListener: (name, handler) => { handlers[name] = handler; },
      clients: {
        matchAll: async () => clients,
        openWindow: openWindow || (async url => { opened.push(url); }),
      },
    },
  });
  return {handlers, opened};
}

function fakeClient(url, {focus} = {}) {
  const client = {url, focused: 0, messages: []};
  client.focus = focus || (async () => { client.focused++; return client; });
  // Structured clone crosses the vm boundary; deepEqual would trip on the foreign Object prototype.
  client.postMessage = message => { client.messages.push(JSON.parse(JSON.stringify(message))); };
  return client;
}

async function click(handlers, data) {
  let pending, closed = 0;
  handlers.notificationclick({notification: {close: () => { closed++; }, data}, waitUntil: promise => { pending = promise; }});
  await pending;
  return closed;
}

test('notification click focuses the open app and asks it for the thread', async () => {
  // The page switches its own hash: a full navigation would drop an open draft
  // and WindowClient.navigate() rejects for clients this worker does not control.
  // The thread travels in the message, never in the URL, so the page keeps its state.
  const foreign = fakeClient('https://other.test/');
  const app = fakeClient('https://nox.test/#now');
  const {handlers, opened} = loadClickWorker([foreign, app]);
  assert.equal(await click(handlers, {thread: 'reply:u1'}), 1);
  assert.equal(app.focused, 1);
  assert.deepEqual(app.messages, [{type: 'OPEN_CHAT', thread: 'reply:u1'}]);
  assert.equal(foreign.focused, 0);
  assert.deepEqual(foreign.messages, []);
  assert.deepEqual(opened, []);
  // A notification without a thread (the test button, an old notification) still opens the chat.
  for (const data of [{thread: null}, {}, undefined]) {
    assert.equal(await click(handlers, data), 1);
    assert.deepEqual(app.messages.at(-1), {type: 'OPEN_CHAT', thread: null});
  }
  assert.equal(app.messages.length, 4);
  assert.deepEqual(opened, []);
});

test('notification click opens a window on the thread when the app is closed', async () => {
  const {handlers, opened} = loadClickWorker([]);
  assert.equal(await click(handlers, {thread: 'reply:u1'}), 1);
  // The thread rides the hash, encoded, so the page can pick it out of #chat/<thread>.
  assert.deepEqual(opened, ['https://nox.test/#chat/reply%3Au1']);
  assert.equal(await click(handlers, {thread: null}), 1);
  assert.equal(await click(handlers, undefined), 1);
  assert.deepEqual(opened, ['https://nox.test/#chat/reply%3Au1', 'https://nox.test/#chat', 'https://nox.test/#chat']);
});

test('notification click falls back to a new window when the open app cannot be focused', async () => {
  const app = fakeClient('https://nox.test/#chat', {focus: async () => { throw new TypeError('not allowed'); }});
  const {handlers, opened} = loadClickWorker([app]);
  await click(handlers, {thread: 'abc'});
  assert.deepEqual(app.messages, []);
  assert.deepEqual(opened, ['https://nox.test/#chat/abc']);
});
