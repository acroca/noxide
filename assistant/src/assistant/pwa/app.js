const $ = (selector, root = document) => root.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const paths = {
  chat: 'M20 11a8 8 0 0 1-8 8H7l-4 3v-7a8 8 0 1 1 17-4Z',
  page: 'M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7',
  settings: 'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  refresh: 'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6',
  send: 'M12 19V5m-5 5 5-5 5 5',
};
function icon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]}"/></svg>`;
}
$$('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
let session, poll, installPrompt, toastTimer, version = 0;
let agentName = $('meta[name="agent-name"]').content;
let topics = [], currentTopic = 'general';
let serverUnavailable = false, booting = false, pendingMutations = 0;
let waitingWorker = null, reloadRequested = false, workerChanged = false;
let hadController = Boolean(navigator.serviceWorker?.controller);
const draftKey = topic => `noxide-draft:${topic}`;
const submissionKey = topic => `noxide-submission:${topic}`;
const chatURL = topic => '#chat/' + encodeURIComponent(topic);
const topicName = topic => topics.find(t => t.id === topic)?.name || 'General';
function toast(text) {
  $('#toast').textContent = text;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
}
function unavailable() {
  serverUnavailable = true;
  connectivity();
  if (!session) {
    $('#shell').hidden = true;
    $('#unavailable').hidden = false;
  }
}
async function api(path, data, method) {
  const options = { method: method || (data === undefined ? 'GET' : 'POST'), credentials: 'same-origin', headers: { 'X-Noxide': '1' } };
  if (data !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(data);
  }
  const mutation = options.method !== 'GET';
  if (mutation) { pendingMutations++; updateBanner(); }
  try {
    let response, result;
    try {
      response = await fetch(`/api/${path}`, {...options, signal: AbortSignal.timeout(12000)});
      if (response.status >= 500) throw new Error('Server unavailable');
      result = await response.json();
    } catch {
      unavailable();
      throw new Error(`Cannot reach ${agentName} right now. Your draft is still here.`);
    }
    serverUnavailable = false;
    connectivity();
    if (!response.ok) {
      throw Object.assign(new Error(result.error || 'Request failed'), {status: response.status});
    }
    return result;
  } finally {
    if (mutation) { pendingMutations--; updateBanner(); }
  }
}
function inline(text) {
  return escape(text).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}
function markdown(text) {
  const out = [];
  let list = false, fence = false, code = [], table = false;
  const closeList = () => { if (list) { out.push('</ul>'); list = false; } };
  const closeTable = () => { if (table) { out.push('</tbody></table>'); table = false; } };
  for (const line of String(text || '').split('\n')) {
    if (/^\s*```/.test(line)) {
      closeList(); closeTable();
      if (fence) { out.push(`<pre>${escape(code.join('\n'))}</pre>`); code = []; }
      fence = !fence; continue;
    }
    if (fence) { code.push(line); continue; }
    const heading = line.match(/^(#{1,4})\s+(.+)/);
    const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)/);
    if (heading) { closeList(); closeTable(); out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); }
    else if (bullet) {
      closeTable(); if (!list) { out.push('<ul>'); list = true; }
      out.push(`<li>${inline(bullet[1])}</li>`);
    } else if (/^\s*\|/.test(line)) {
      closeList(); if (/^\s*\|[\s:|-]+$/.test(line)) continue;
      if (!table) { out.push('<table><tbody>'); table = true; }
      out.push('<tr>' + line.trim().replace(/^\||\|$/g, '').split('|').map(cell => `<td>${inline(cell.trim())}</td>`).join('') + '</tr>');
    } else { closeList(); closeTable(); if (line.trim()) out.push(`<p>${inline(line)}</p>`); }
  }
  closeList(); closeTable(); if (fence) out.push(`<pre>${escape(code.join('\n'))}</pre>`);
  return `<div class="markdown">${out.join('')}</div>`;
}
function dateLabel(seconds) {
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: session?.timezone }).format(new Date(seconds * 1000));
}
function draft(topic) {
  try { return localStorage.getItem(draftKey(topic)) || ''; } catch { return ''; }
}
function renderChat(topic, pageVersion) {
  currentTopic = topic;
  const name = topicName(topic);
  $('#main').className = 'chat-main';
  $('#main').innerHTML = `
    <section class="chat-panel" aria-label="Chat">
      <header class="chat-header">
        <div class="channel-picker"><span class="topic-label">Topic</span>
          <button id="chat-topic" type="button" aria-label="Change topic: ${escape(name)}" aria-haspopup="dialog" aria-controls="topic-picker">${escape(name)} <span aria-hidden="true">▾</span></button>
        </div>
        <div><button id="reset-chat" class="quiet" type="button">Reset context</button><button id="clear-chat" class="quiet danger" type="button">Delete chat</button></div>
      </header>
      <button id="older-messages" class="quiet older" hidden>Load earlier messages</button>
      <div id="chat-thread" class="chat-thread" role="log" aria-label="${escape(name)} messages"><div class="loading">Loading messages…</div></div>
      <form id="chat-form" class="chat-composer">
        <textarea aria-label="Message to ${escape(name)}" rows="2" maxlength="20000" enterkeyhint="enter" placeholder="Message ${escape(name)}…" required>${escape(draft(topic))}</textarea>
        <button class="send-button" type="submit" aria-label="Send message">${icon('send')}</button>
      </form>
      <p id="chat-status" class="chat-status" role="status">Writing in ${escape(name)}</p>
    </section>`;
  $('#chat-topic').addEventListener('click', () => {
    const picker = $('#topic-picker');
    $('#topic-options').innerHTML = topics.filter(t => !t.legacy).map(t =>
      `<a href="${chatURL(t.id)}" ${t.id === topic ? 'aria-current="page"' : ''}>${escape(t.name)}</a>`
    ).join('') + (topics.some(t => t.legacy) ? '<h3>Previous web chats</h3>' + topics.filter(t => t.legacy).map(t =>
      `<a href="${chatURL(t.id)}" ${t.id === topic ? 'aria-current="page"' : ''}>${escape(t.name)}</a>`
    ).join('') : '');
    picker.showModal();
  });
  const active = () => version === pageVersion && Boolean($('#chat-thread'));
  let signature = '', cursor = null, older = [], busy = false, sending = false, loaded = false;
  const button = $('#chat-form button'), area = $('#chat-form textarea');
  function updateComposer() {
    if (!active()) return;
    button.disabled = !loaded || busy || sending || !navigator.onLine;
    $('#chat-status').textContent = !navigator.onLine ? 'Offline. Your draft stays on this device.'
      : busy ? 'Finish or clear the pending message before sending another.' : `Writing in ${name}`;
  }
  async function loadMessages(loadOlder = false) {
    const data = await api('messages?space=' + encodeURIComponent(topic) + (loadOlder ? `&before=${cursor}` : ''));
    if (!active()) return;
    if (loadOlder) { older = [...data.messages, ...older]; cursor = data.before; await loadMessages(); return; }
    if (!older.length) cursor = data.before;
    $('#older-messages').hidden = cursor === null;
    const nextSignature = JSON.stringify([data.messages, older]);
    loaded = true;
    busy = data.messages.some(m => m.role === 'user' && m.source !== 'telegram' && !['done', 'dismissed'].includes(m.status));
    updateComposer();
    if (nextSignature === signature) return;
    const initial = !signature;
    signature = nextSignature;
    const thread = $('#chat-thread'), nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 100;
    const ids = new Set();
    const messages = [...older, ...data.messages].filter(m => { if (ids.has(m.id)) return false; ids.add(m.id); return true; });
    thread.innerHTML = messages.length ? messages.map(m => `
      <article class="message message-${escape(m.role)}">
        <div class="message-meta"><strong>${m.role === 'user' ? 'You' : escape(agentName)}</strong><time>${escape(dateLabel(m.created))}</time><span>${m.source === 'telegram' ? 'Telegram' : 'Web'}</span>${m.role === 'assistant' && ['failed','partial','pending'].includes(m.delivery) ? `<span>Telegram delivery: ${escape(m.delivery)}</span>` : ''}</div>
        <div class="message-body">${m.role === 'user' ? escape(m.text) : markdown(m.text)}</div>
        ${m.role === 'user' && !['done', 'dismissed'].includes(m.status) ? `<div class="message-status"><span>${escape(m.error || ({ queued: 'Queued…', running: 'Working…' }[m.status] || m.status))}</span>${m.source !== 'telegram' && ['failed', 'interrupted', 'unavailable'].includes(m.status) ? `<button data-retry="${escape(m.id)}">Retry</button>` : ''}</div>` : ''}
      </article>`).join('') : `<div class="chat-empty"><h1>${escape(name)}</h1><p>No messages yet. Send a message to start.</p></div>`;
    $$('[data-retry]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      try { await api('retry', { id: b.dataset.retry }); await loadMessages(); }
      catch (e) { toast(e.message); } finally { b.disabled = false; }
    }));
    if (initial || nearBottom) thread.scrollTop = thread.scrollHeight;
  }
  $('#older-messages').addEventListener('click', () => loadMessages(true).catch(e => toast(e.message)));
  $('#clear-chat').addEventListener('click', async () => {
    if (!confirm(`Delete the saved conversation for ${name}, including its Telegram and web archive and model context? This cannot be undone. Messages in the Telegram app, vault notes, and backups are not deleted.`)) return;
    try {
      await api('clear', { space: topic });
      localStorage.removeItem(draftKey(topic)); localStorage.removeItem(submissionKey(topic));
      if (active()) await route();
    } catch (e) { toast(e.message); }
  });
  $('#reset-chat').addEventListener('click', async () => {
    if (!confirm(`Start fresh in ${name}? The shared Telegram/web context will reset. Saved messages remain visible and can still be retrieved through history tools.`)) return;
    try { await api('reset', {space: topic}); if (active()) await route(); toast('Context reset. Saved conversation kept.'); }
    catch (e) { toast(e.message); }
  });
  area.addEventListener('input', () => {
    try { localStorage.setItem(draftKey(topic), area.value); } catch { toast('This browser cannot save drafts. Keep this tab open.'); }
  });
  area.addEventListener('keydown', event => {
    // Touch-first devices keep the keyboard's Return key for newlines.
    if (window.matchMedia('(pointer: coarse)').matches) return;
    // IME confirmation can report Enter, including keyCode 229 in Safari.
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (!event.repeat && !button.disabled) $('#chat-form').requestSubmit(button);
  });
  $('#chat-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (sending || busy || !loaded) return;
    const text = area.value.trim(); if (!text) return;
    if (!navigator.onLine) { toast('You’re offline. Your draft has not been sent.'); return; }
    sending = true; updateComposer();
    try {
      let pending;
      try { pending = JSON.parse(localStorage.getItem(submissionKey(topic))); } catch {}
      if (pending?.text !== text) pending = { id: crypto.randomUUID(), text };
      localStorage.setItem(submissionKey(topic), JSON.stringify(pending));
      await api('messages', { id: pending.id, space: topic, text });
      // Do not erase a new draft typed while the request was in flight.
      if (draft(topic).trim() === text) localStorage.removeItem(draftKey(topic));
      localStorage.removeItem(submissionKey(topic));
      if (area.value.trim() === text) area.value = '';
      await loadMessages();
    } catch (e) { toast(e.message); } finally { sending = false; updateComposer(); }
  });
  updateComposer();
  loadMessages().catch(e => { if (active()) { $('#chat-thread').textContent = e.message; toast(e.message); } });
  poll = setInterval(() => { if (!document.hidden && navigator.onLine) loadMessages().catch(() => {}); }, 2200);
}
async function route() {
  if (!session) return;
  clearInterval(poll);
  const pageVersion = ++version;
  const [requested, ...parts] = location.hash.slice(1).split('/');
  const view = requested === 'now' || requested === 'today' ? 'now' : 'chat';
  let topic;
  try { topic = requested === 'chat' ? decodeURIComponent(parts.join('/')) || 'general' : 'general'; }
  catch { topic = 'general'; }
  $$('[data-nav]').forEach(a => { a.classList.toggle('active', a.dataset.nav === view); a.setAttribute('aria-current', a.dataset.nav === view ? 'page' : 'false'); });
  try {
    const data = await api('topics');
    if (pageVersion !== version) return;
    topics = data.topics;
    $('#topic-links').innerHTML = topics.filter(t => !t.legacy).map(t => `<a href="${chatURL(t.id)}" ${view === 'chat' && topic === t.id ? 'class="active" aria-current="page"' : ''}><span aria-hidden="true">#</span>${escape(t.name)}</a>`).join('');
    if (view === 'now') {
      $('#breadcrumb').textContent = 'Now'; document.title = `${agentName} · Now`;
      $('#main').className = 'now-main';
      $('#main').innerHTML = '<header class="now-header"><h1>Now</h1><span>wiki/now.md · Read only</span></header><pre id="now-content" class="now-content">Loading…</pre>';
      const page = await api('now');
      if (pageVersion === version) $('#now-content').textContent = page.content || 'wiki/now.md is empty or has not been created yet.';
    } else {
      if (!topics.some(t => t.id === topic)) { toast('That topic is no longer available. Showing General.'); topic = 'general'; history.replaceState(null, '', '#chat'); }
      $('#breadcrumb').textContent = 'Chat / ' + topicName(topic); document.title = agentName + ' · ' + topicName(topic);
      renderChat(topic, pageVersion);
    }
  } catch (e) {
    // Keep any open composer/draft visible when the server disappears.
    if (pageVersion === version && !$('#chat-form')) {
      $('#main').className = ''; $('#main').textContent = e.message;
    }
  }
}
async function boot() {
  if (booting) return;
  booting = true;
  $('#retry-connection').disabled = true;
  try {
    session = await api('session');
    agentName = session.agent_name;
    $$('[data-agent-name]').forEach(el => { el.textContent = agentName; });
    $('#unavailable-title').textContent = `${agentName} is unavailable`;
    $('#offline-banner').textContent = `${agentName} is unreachable. Your draft stays here; nothing is sent automatically.`;
    $('#unavailable').hidden = true; $('#shell').hidden = false;
    await route();
  } catch (e) {
    unavailable();
    if (!session) $('#unavailable-detail').textContent = e.message + ' Check that the assistant and Tailscale are running.';
  } finally { booting = false; $('#retry-connection').disabled = false; }
}
$('#retry-connection').addEventListener('click', boot);
$('#refresh').addEventListener('click', route);
$('#close-topics').addEventListener('click', () => $('#topic-picker').close());
$('#topic-options').addEventListener('click', event => {
  if (event.target.closest('a')) $('#topic-picker').close();
});
window.addEventListener('hashchange', route);
function connectivity() {
  const offline = !navigator.onLine || serverUnavailable;
  $('#offline-banner').hidden = !offline;
  $('#connection').textContent = offline ? `${agentName} is unreachable · drafts stay here` : 'Connected to your vault';
}
window.addEventListener('online', () => { connectivity(); if (session) route(); else boot(); });
window.addEventListener('offline', connectivity);
async function openSettings() {
  if (!$('#settings').open) $('#settings').showModal();
  const supported = 'serviceWorker' in navigator && 'PushManager' in window;
  $('#push-toggle').disabled = !supported || !session?.push_key;
  $('#push-status').textContent = !session?.push_key ? 'Push is not configured. Set pwa.push_contact on your server.' : !supported ? 'Install this app on your Home Screen, or use a browser that supports web push.' : 'Notifications are optional and controlled by this device.';
  if (supported) {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    $('#push-toggle').textContent = subscription ? 'Disable notifications' : 'Enable notifications';
    $('#push-test').hidden = !subscription;
  }
}
$('#settings-button').addEventListener('click', () => openSettings().catch(e => toast(e.message)));
$('#mobile-settings').addEventListener('click', () => openSettings().catch(e => toast(e.message)));
$('#push-toggle').addEventListener('click', async () => {
  const button = $('#push-toggle'); button.disabled = true;
  try {
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (subscription) { await api('push', { endpoint: subscription.endpoint }, 'DELETE'); await subscription.unsubscribe(); toast('Notifications disabled on this device.'); }
    else {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('Notification permission was not granted. You can change it in browser settings.');
      const raw = atob(session.push_key.replaceAll('-', '+').replaceAll('_', '/'));
      subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, c => c.charCodeAt(0)) });
      try { await api('push', subscription.toJSON()); } catch (e) { await subscription.unsubscribe(); throw e; }
      toast('This device is ready for notifications.');
    }
    await openSettings();
  } catch (e) { $('#push-status').textContent = e.message; } finally { button.disabled = false; }
});
$('#push-test').addEventListener('click', async () => { try { await api('push/test', {}); toast('Test notification requested.'); } catch (e) { toast(e.message); } });
$('#clear-local-drafts').addEventListener('click', async () => {
  if (!confirm('Clear saved drafts on this device? Sent messages and notifications are not affected.')) return;
  try {
    Object.keys(localStorage).filter(k => k.startsWith('noxide-draft:')).forEach(k => localStorage.removeItem(k));
    const area = $('#chat-form textarea');
    if (area) area.value = '';
    toast('Local drafts cleared.');
  } catch (e) { toast(e.message); }
});
window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); installPrompt = e; $('#install').hidden = false; });
$('#install').addEventListener('click', async () => { if (installPrompt) { await installPrompt.prompt(); installPrompt = null; $('#install').hidden = true; } });
function updateBanner() {
  $('#update-banner').hidden = !waitingWorker && !workerChanged;
  $('#reload-update').disabled = pendingMutations > 0 || reloadRequested;
  $('#update-label').textContent = pendingMutations > 0 ? 'Update available. Waiting for your request to finish…'
    : reloadRequested ? 'Updating…' : 'Update available. Reload when you’re ready.';
}
function saveOpenDraft() {
  const area = $('#chat-form textarea');
  if (area) localStorage.setItem(draftKey(currentTopic), area.value);
}
$('#reload-update').addEventListener('click', () => {
  if (pendingMutations || reloadRequested) return;
  try { saveOpenDraft(); }
  catch { toast('Your draft could not be saved. The app has not reloaded.'); return; }
  if (workerChanged) { location.reload(); return; }
  if (!waitingWorker) return;
  reloadRequested = true; updateBanner();
  waitingWorker.postMessage({type: 'ACTIVATE_UPDATE'});
  setTimeout(() => {
    if (!reloadRequested) return;
    reloadRequested = false; updateBanner();
    toast('Update has not activated yet. Try Reload again.');
  }, 10000);
});
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    // Initial installation takes control too; it is not an app update.
    if (!hadController) { hadController = true; return; }
    workerChanged = true;
    if (reloadRequested) {
      try { saveOpenDraft(); location.reload(); }
      catch { reloadRequested = false; toast('Could not save your draft. Reload cancelled.'); }
    }
    updateBanner(); // Other open tabs offer reload rather than losing their drafts.
  });
  navigator.serviceWorker.register('/sw.js', {updateViaCache: 'none'}).then(registration => {
    const checkWaiting = () => {
      if (registration.waiting && navigator.serviceWorker.controller) {
        waitingWorker = registration.waiting; updateBanner();
      }
    };
    const watchInstalling = () => registration.installing?.addEventListener('statechange', checkWaiting);
    checkWaiting(); watchInstalling();
    registration.addEventListener('updatefound', watchInstalling);
    const checkUpdate = () => { if (navigator.onLine && !document.hidden) registration.update().catch(() => {}); };
    document.addEventListener('visibilitychange', checkUpdate);
    window.addEventListener('online', checkUpdate);
    setInterval(checkUpdate, 60 * 60 * 1000);
  }).catch(() => {});
}
connectivity(); boot();
