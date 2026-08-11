const CACHE_NAME = 'jarvis-shell-v10';
const SHELL = [
  '/static/style.css?v=20260731-navguard',
  '/static/navigation.js?v=20260731-navguard2',
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

// Web Push handlers removed with the mobile gateway (REQ-120, 2026-08-11):
// nothing sends pushes anymore, so a handler here would be dead code.
