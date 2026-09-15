// Bump this when packaged shell assets change so existing installs offer an update.
const CACHE = 'noxide-shell-v15-__INSTANCE_VERSION__';
const AGENT_NAME = "__AGENT_NAME__";
const SHELL = ['/', '/app.js', '/style.css', '/icon.svg', '/icon-192.png', '/icon-512.png', '/manifest.webmanifest'];
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
  let space = 'general';
  let name = AGENT_NAME;
  let channel;
  let body;
  try {
    const data = event.data.json();
    if (typeof data.space === 'string') space = data.space;
    if (typeof data.agent_name === 'string' && data.agent_name.trim()) name = data.agent_name;
    if (typeof data.channel_name === 'string' && data.channel_name.trim()) channel = data.channel_name;
    if (typeof data.body === 'string' && data.body.trim()) body = data.body;
  } catch {}
  event.waitUntil(self.registration.showNotification(channel || (space === 'general' ? 'General' : name), {
    body: body || `You have a new update. Open ${name} to read it.`, icon: '/icon-192.png', badge: '/icon-192.png', tag: 'noxide-update', data: {url: '/#chat/'+encodeURIComponent(space)}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/#chat', self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async clients => {
    for (const client of clients) { if (new URL(client.url).origin === self.location.origin) { await client.navigate(target.href); return client.focus(); } }
    return self.clients.openWindow(target.href);
  }));
});
