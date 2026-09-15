import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const worker = readFileSync(new URL('../src/assistant/pwa/sw.js', import.meta.url), 'utf8')
  .replace('"__AGENT_NAME__"', JSON.stringify('Juniper'));

test('push displays the channel name and reply, retaining topic navigation', async () => {
  const handlers = {};
  let notification;
  vm.runInNewContext(worker, {
    self: {
      addEventListener: (name, handler) => { handlers[name] = handler; },
      registration: { showNotification: async (title, options) => { notification = { title, ...options }; } },
    },
  });
  let pending;
  handlers.push({
    data: { json: () => ({ space: 'topic:10', body: 'Hello there! This is the test reminder', agent_name: 'Cedar', channel_name: 'Health' }) },
    waitUntil: promise => { pending = promise; },
  });
  await pending;
  assert.equal(notification.title, 'Health');
  assert.equal(notification.body, 'Hello there! This is the test reminder');
  assert.equal(notification.data.url, '/#chat/topic%3A10');

  // Previously queued pushes have no body; malformed pushes still display safely.
  for (const data of [{ json: () => ({ space: 'general' }) }, { json: () => { throw Error('bad JSON'); } }]) {
    handlers.push({ data, waitUntil: promise => { pending = promise; } });
    await pending;
    assert.equal(notification.title, 'General');
    assert.equal(notification.body, 'You have a new update. Open Juniper to read it.');
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
