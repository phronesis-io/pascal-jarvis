const CACHE_NAME = 'jarvis-shell-v5';
const SHELL = [
  '/static/style.css?v=20260722-matter3',
  '/static/manifest.webmanifest',
  '/static/app-icon.svg',
  '/static/app-icon-192.png',
  '/static/app-icon-512.png',
  '/static/offline.html',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/static/offline.html')));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      fetch(request).then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
        }
        return response;
      }).catch(() => caches.match(request)),
    );
  }
});

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {}; }
  event.waitUntil(self.registration.showNotification(data.title || 'Jarvis', {
    body: data.body || '有一条新的事项动态',
    icon: '/static/app-icon-192.png',
    badge: '/static/app-icon-192.png',
    data: {url: data.url || '/items'},
    tag: data.matter_id || 'jarvis-update',
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = event.notification.data?.url || '/items';
  event.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true})
    .then(windows => {
      const existing = windows.find(client => new URL(client.url).pathname === target);
      return existing ? existing.focus() : clients.openWindow(target);
    }));
});
