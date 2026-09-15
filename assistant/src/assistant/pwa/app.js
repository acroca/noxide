const $ = (selector, root = document) => root.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const paths = {
  chat: 'M20 11a8 8 0 0 1-8 8H7l-4 3v-7a8 8 0 1 1 17-4Z',
  page: 'M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7',
  settings: 'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  refresh: 'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6',
  send: 'M12 19V5m-5 5 5-5 5 5',
  image: 'M4 5h16v14H4zM4 15l5-5 4 4 3-3 4 4M15.5 9.5a1 1 0 1 1-2 0 1 1 0 0 1 2 0',
  mic: 'M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM5 11a7 7 0 0 0 14 0M12 18v3M8 21h8',
};
const MAX_IMAGES = 4, IMAGE_EDGE = 2000, KEEP_ORIGINAL_BYTES = 4 * 1024 * 1024, MAX_RECORDING_MS = 5 * 60 * 1000;
const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
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
let ackVisible = () => {};
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
async function api(path, data, method, background = false) {
  const options = { method: method || (data === undefined ? 'GET' : 'POST'), credentials: 'same-origin', headers: { 'X-Noxide': '1' } };
  if (data !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(data);
  }
  // Background acknowledgements never hold up an app update.
  const mutation = options.method !== 'GET' && !background;
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
async function upload(path, blob) {
  // Raw-body uploads (images, recordings); JSON calls go through api().
  pendingMutations++; updateBanner();
  try {
    let response, result;
    try {
      response = await fetch(`/api/${path}`, { method: 'POST', credentials: 'same-origin', headers: { 'X-Noxide': '1', 'Content-Type': blob.type }, body: blob, signal: AbortSignal.timeout(180000) });
      if (response.status >= 500) throw new Error('Server unavailable');
      result = await response.json();
    } catch {
      unavailable();
      throw new Error(`Cannot reach ${agentName} right now.`);
    }
    serverUnavailable = false;
    connectivity();
    if (!response.ok) throw new Error(result.error || 'Upload failed');
    return result;
  } finally { pendingMutations--; updateBanner(); }
}
async function prepareImage(file) {
  // Phones hand over HEIC and multi-megabyte originals: decode on the device
  // and re-encode as JPEG within 2000px, keeping small accepted files as they are.
  let bitmap;
  try { bitmap = await createImageBitmap(file); }
  catch {
    if (IMAGE_TYPES.includes(file.type) && file.size <= KEEP_ORIGINAL_BYTES) return file;
    throw new Error(`Couldn't read ${file.name || 'that image'}.`);
  }
  const scale = Math.min(1, IMAGE_EDGE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && IMAGE_TYPES.includes(file.type) && file.size <= KEEP_ORIGINAL_BYTES) { bitmap.close(); return file; }
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.85));
  if (!blob) throw new Error(`Couldn't convert ${file.name || 'that image'}.`);
  return blob;
}
function attachmentsOf(message) {
  try { return JSON.parse(message.metadata || '{}').attachments || []; } catch { return []; }
}
function thumbnails(message) {
  const paths = attachmentsOf(message);
  if (!paths.length) return '';
  return `<div class="message-images">${paths.map(p => { const src = '/api/attachment?path=' + encodeURIComponent(p); return `<a href="${src}" target="_blank" rel="noopener"><img src="${src}" alt="Attached image" loading="lazy"></a>`; }).join('')}</div>`;
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
        <div><button id="reset-chat" class="quiet" type="button">Reset context</button></div>
      </header>
      <button id="older-messages" class="quiet older" hidden>Load earlier messages</button>
      <div id="chat-thread" class="chat-thread" role="log" aria-label="${escape(name)} messages"><div class="loading">Loading messages…</div></div>
      <form id="chat-form" class="chat-composer">
        <div id="composer-images" class="composer-images" hidden></div>
        <button id="attach-image" class="tool-button" type="button" aria-label="Attach image">${icon('image')}</button>
        <input id="image-input" type="file" accept="image/*" multiple hidden>
        <textarea aria-label="Message to ${escape(name)}" rows="2" maxlength="20000" enterkeyhint="enter" placeholder="Message ${escape(name)}…">${escape(draft(topic))}</textarea>
        <button id="record-voice" class="tool-button" type="button" aria-label="Record voice message" hidden>${icon('mic')}</button>
        <button class="send-button" type="submit" aria-label="Send message">${icon('send')}</button>
      </form>
      <p id="chat-status" class="chat-status" role="status">Writing in ${escape(name)} <button id="discard-recording" class="quiet danger" type="button" hidden>Discard recording</button></p>
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
  let latest = [], acked = 0, images = [], activity = '', recorder = null, recordStarted = 0, recordTicker = null;
  const button = $('#chat-form .send-button'), area = $('#chat-form textarea'), form = $('#chat-form');
  const atEnd = thread => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 100;
  function markSeen() {
    // Tell the server this device is showing the newest reply: focused, on this
    // topic, scrolled to the end. Other devices then skip the push for it.
    // Merely being open, or reading older messages, acknowledges nothing.
    if (!active() || document.hidden || !document.hasFocus() || !atEnd($('#chat-thread'))) return;
    const newest = Math.max(0, ...latest.filter(m => m.role === 'assistant').map(m => m.created));
    if (newest <= acked) return;
    const previous = acked;
    acked = newest;
    api('seen', { space: topic, through: newest }, undefined, true).catch(() => { if (acked === newest) acked = previous; });
  }
  ackVisible = markSeen;
  // Closing the keyboard restores the bottom nav's home-indicator inset, which
  // shrinks the thread; a shrinking scroller keeps its offset and hides the
  // end. Stay anchored to the end across resizes unless the reader scrolled up.
  const thread = $('#chat-thread');
  let stickToEnd = true;
  thread.addEventListener('scroll', () => { stickToEnd = atEnd(thread); });
  new ResizeObserver(() => { if (stickToEnd) thread.scrollTop = thread.scrollHeight; }).observe(thread);
  function updateComposer() {
    if (!active()) return;
    button.disabled = !loaded || busy || sending || !navigator.onLine || Boolean(recorder);
    $('#attach-image').disabled = sending || images.length >= MAX_IMAGES;
    $('#discard-recording').hidden = !recorder;
    $('#chat-status').firstChild.textContent = (!navigator.onLine ? 'Offline. Your draft stays on this device.'
      : activity ? activity
      : busy ? 'Finish or clear the pending message before sending another.' : `Writing in ${name}`) + ' ';
  }
  function setActivity(text) { activity = text; updateComposer(); }
  function renderImages() {
    const strip = $('#composer-images');
    strip.hidden = !images.length;
    strip.innerHTML = images.map((image, i) => `<figure><img src="${image.url}" alt="Image ${i + 1} to attach"><button type="button" data-remove="${i}" aria-label="Remove image ${i + 1}">×</button></figure>`).join('');
    strip.querySelectorAll('[data-remove]').forEach(b => b.addEventListener('mousedown', event => event.preventDefault()));
    strip.querySelectorAll('[data-remove]').forEach(b => b.addEventListener('click', () => {
      const [removed] = images.splice(Number(b.dataset.remove), 1);
      URL.revokeObjectURL(removed.url);
      renderImages(); updateComposer();
    }));
  }
  async function addImages(files) {
    for (const file of files) {
      if (!active()) return;
      if (images.length >= MAX_IMAGES) { toast(`Up to ${MAX_IMAGES} images per message.`); break; }
      try {
        const blob = await prepareImage(file);
        images.push({ blob, url: URL.createObjectURL(blob), path: null });
      } catch (e) { toast(e.message); }
    }
    renderImages(); updateComposer();
  }
  function clearImages() {
    images.forEach(image => URL.revokeObjectURL(image.url));
    images = []; renderImages();
  }
  // Composer buttons leave focus in the textarea: the keyboard stays open
  // across a send, and blurring would shift the layout under the tap.
  form.querySelectorAll('button').forEach(b => b.addEventListener('mousedown', event => event.preventDefault()));
  $('#attach-image').addEventListener('click', () => $('#image-input').click());
  $('#image-input').addEventListener('change', event => { addImages([...event.target.files]); event.target.value = ''; });
  area.addEventListener('paste', event => {
    // Screenshots and copied pictures land as files; text pastes stay untouched.
    const files = [...(event.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
    if (!files.length) return;
    event.preventDefault();
    addImages(files);
  });
  form.addEventListener('dragover', event => { if ([...event.dataTransfer.types].includes('Files')) { event.preventDefault(); form.classList.add('dragover'); } });
  form.addEventListener('dragleave', () => form.classList.remove('dragover'));
  form.addEventListener('drop', event => {
    form.classList.remove('dragover');
    const files = [...event.dataTransfer.files].filter(f => f.type.startsWith('image/') || /\.hei[cf]$/i.test(f.name));
    if (!files.length) return;
    event.preventDefault();
    addImages(files);
  });
  const voiceSupported = Boolean(session?.voice && window.MediaRecorder && navigator.mediaDevices?.getUserMedia);
  $('#record-voice').hidden = !voiceSupported;
  function stopRecording(discard = false) {
    if (!recorder) return;
    recorder.discard = discard;
    if (recorder.state !== 'inactive') recorder.stop();
  }
  async function startRecording() {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { toast('Microphone permission was not granted. You can change it in your device settings.'); return; }
    const mimeType = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'].find(t => MediaRecorder.isTypeSupported(t));
    const chunks = [];
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    recorder.addEventListener('dataavailable', event => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener('stop', async () => {
      const { discard } = recorder, type = recorder.mimeType || mimeType || 'audio/webm';
      stream.getTracks().forEach(track => track.stop());
      clearInterval(recordTicker); recorder = null;
      $('#record-voice').classList.remove('recording'); $('#record-voice').setAttribute('aria-label', 'Record voice message');
      if (!active()) return;
      const blob = new Blob(chunks, { type: type.split(';')[0] });
      if (discard || blob.size < 1000) { setActivity(''); if (!discard) toast('Nothing was recorded.'); return; }
      setActivity('Transcribing…');
      try {
        const { text } = await upload('transcribe', blob);
        if (!active()) return;
        area.value = area.value.trim() ? area.value.replace(/\s*$/, ' ') + text : text;
        area.dispatchEvent(new Event('input'));
        area.focus();
      } catch (e) { toast(e.message); } finally { setActivity(''); }
    });
    recorder.start(1000);
    recordStarted = Date.now();
    $('#record-voice').classList.add('recording'); $('#record-voice').setAttribute('aria-label', 'Stop recording');
    const tick = () => {
      const seconds = Math.round((Date.now() - recordStarted) / 1000);
      setActivity(`Recording ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} · tap the mic to stop`);
      if (Date.now() - recordStarted >= MAX_RECORDING_MS) stopRecording();
    };
    tick(); recordTicker = setInterval(tick, 1000);
  }
  $('#record-voice').addEventListener('click', () => { if (recorder) stopRecording(); else startRecording().catch(e => toast(e.message)); });
  $('#discard-recording').addEventListener('click', () => stopRecording(true));
  async function loadMessages(loadOlder = false) {
    const data = await api('messages?space=' + encodeURIComponent(topic) + (loadOlder ? `&before=${cursor}` : ''));
    if (!active()) return;
    if (loadOlder) { older = [...data.messages, ...older]; cursor = data.before; await loadMessages(); return; }
    if (!older.length) cursor = data.before;
    $('#older-messages').hidden = cursor === null;
    const nextSignature = JSON.stringify([data.messages, older, data.generation]);
    loaded = true;
    busy = data.messages.some(m => m.role === 'user' && m.source !== 'telegram' && !['done', 'dismissed'].includes(m.status));
    latest = data.messages;
    updateComposer();
    if (nextSignature === signature) { markSeen(); return; }
    const initial = !signature;
    signature = nextSignature;
    const thread = $('#chat-thread'), nearBottom = atEnd(thread);
    const ids = new Set();
    const messages = [...older, ...data.messages].filter(m => { if (ids.has(m.id)) return false; ids.add(m.id); return true; });
    // A context reset starts a new generation: mark where the model stopped
    // seeing earlier messages, including a reset nothing has followed yet.
    const divider = '<div class="context-divider" role="separator">Context reset</div>';
    const trailing = messages.length && messages[messages.length - 1].generation < data.generation ? divider : '';
    thread.innerHTML = messages.length ? messages.map((m, i) => `
      ${i && m.generation !== messages[i - 1].generation ? divider : ''}
      <article class="message message-${escape(m.role)}">
        <div class="message-meta"><strong>${m.role === 'user' ? 'You' : escape(agentName)}</strong><time>${escape(dateLabel(m.created))}</time>${m.role === 'assistant' && !m.reply_to ? '' : `<span>${m.source === 'telegram' ? 'Telegram' : 'Web'}</span>`}${m.role === 'assistant' && ['failed','partial','pending'].includes(m.delivery) ? `<span>Telegram delivery: ${escape(m.delivery)}</span>` : ''}</div>
        <div class="message-body">${m.role === 'user' ? thumbnails(m) + escape(m.text) : markdown(m.text)}</div>
        ${m.role === 'user' && !['done', 'dismissed'].includes(m.status) ? `<div class="message-status"><span>${escape(m.error || ({ queued: 'Queued…', running: 'Working…' }[m.status] || m.status))}</span>${m.source !== 'telegram' && ['failed', 'interrupted', 'unavailable'].includes(m.status) ? `<button data-retry="${escape(m.id)}">Retry</button>` : ''}</div>` : ''}
      </article>`).join('') + trailing : `<div class="chat-empty"><h1>${escape(name)}</h1><p>No messages yet. Send a message to start.</p></div>`;
    $$('[data-retry]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      try { await api('retry', { id: b.dataset.retry }); await loadMessages(); }
      catch (e) { toast(e.message); } finally { b.disabled = false; }
    }));
    if (initial || nearBottom) thread.scrollTop = thread.scrollHeight;
    markSeen();
  }
  $('#older-messages').addEventListener('click', () => loadMessages(true).catch(e => toast(e.message)));
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
    if (sending || busy || !loaded || recorder) return;
    const text = area.value.trim(); if (!text && !images.length) return;
    if (!navigator.onLine) { toast('You’re offline. Your draft has not been sent.'); return; }
    sending = true; updateComposer();
    try {
      for (const [i, image] of images.entries()) {
        // Uploaded paths stick to the image, so a retried send reuses them.
        if (image.path) continue;
        setActivity(`Uploading image ${i + 1} of ${images.length}…`);
        image.path = (await upload('attachments', image.blob)).path;
      }
      setActivity('');
      const attachments = images.map(image => image.path);
      let pending;
      try { pending = JSON.parse(localStorage.getItem(submissionKey(topic))); } catch {}
      if (pending?.text !== text || JSON.stringify(pending?.attachments || []) !== JSON.stringify(attachments)) pending = { id: crypto.randomUUID(), text, attachments };
      localStorage.setItem(submissionKey(topic), JSON.stringify(pending));
      await api('messages', { id: pending.id, space: topic, text, attachments });
      // Do not erase a new draft typed while the request was in flight.
      if (draft(topic).trim() === text) localStorage.removeItem(draftKey(topic));
      localStorage.removeItem(submissionKey(topic));
      if (area.value.trim() === text) area.value = '';
      clearImages();
      await loadMessages();
    } catch (e) { toast(e.message); } finally { sending = false; setActivity(''); }
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
window.addEventListener('focus', () => ackVisible());
document.addEventListener('visibilitychange', () => ackVisible());
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
