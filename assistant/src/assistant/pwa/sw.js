// The server stamps a hash of every shell file here, so any asset change is a
// new version and existing installs offer an update; nothing to bump by hand.
const CACHE = 'noxide-shell-__INSTANCE_VERSION__';
const AGENT_NAME = "__AGENT_NAME__";
const SHELL = ['/', '/app.js', '/theme.js', '/style.css', '/icon.svg', '/icon-192.png', '/icon-512.png', '/manifest.webmanifest'];
self.addEventListener('install', event => { event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))); });
self.addEventListener('activate', event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('noxide-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())); });
self.addEventListener('message', event => {
  if (event.data?.type === 'ACTIVATE_UPDATE') event.waitUntil(self.skipWaiting());
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== 'GET' || !SHELL.includes(url.pathname)) return;
  // Serve one installed shell version, even if the server is down or upgrading.
  // API/vault responses never enter the cache. The browser updates sw.js itself.
  event.respondWith(caches.open(CACHE).then(async cache => {
    const cached = await cache.match(url.pathname);
    return cached || fetch(event.request);
  }));
});
self.addEventListener('push', event => {
  let name = AGENT_NAME;
  let body;
  let thread = null;
  try {
    const data = event.data.json();
    if (typeof data.agent_name === 'string' && data.agent_name.trim()) name = data.agent_name;
    if (typeof data.body === 'string' && data.body.trim()) body = data.body;
    if (typeof data.thread === 'string' && data.thread) thread = data.thread;
    // The app badge counts unread replies; the server sends the total with each push.
    if (Number.isInteger(data.unread) && data.unread >= 0) self.navigator?.setAppBadge?.(data.unread)?.catch?.(() => {});
  } catch {}
  event.waitUntil(self.registration.showNotification(name, {
    body: body || `You have a new update. Open ${name} to read it.`, icon: '/icon-192.png', badge: '/icon-192.png', tag: 'noxide-update', data: {thread}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const thread = typeof event.notification.data?.thread === 'string' ? event.notification.data.thread : null;
  // The thread rides the URL only when no window is open; an open app is told directly.
  const target = new URL(thread ? '/#chat/' + encodeURIComponent(thread) : '/#chat', self.location.origin);
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async clients => {
    // The open app switches its own hash: a full navigation would drop an open
    // draft, and WindowClient.navigate() rejects for clients this worker does
    // not control, which used to abort the click before anything opened.
    for (const client of clients) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      try { await client.focus(); } catch { continue; }
      client.postMessage({type: 'OPEN_CHAT', thread});
      return;
    }
    return self.clients.openWindow(target.href);
  }));
});
