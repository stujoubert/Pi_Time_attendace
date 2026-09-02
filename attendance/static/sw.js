// Service worker - network first, no HTML caching
// This ensures CSS/HTML changes always take effect immediately
const CACHE = 'attendance-v4';
const STATIC_ASSETS = [
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  // Delete ALL old caches
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Never cache HTML pages - always fetch fresh
  if (e.request.headers.get('accept')?.includes('text/html') ||
      url.pathname.endsWith('.html') ||
      url.pathname === '/' ||
      !url.pathname.includes('.')) {
    e.respondWith(fetch(e.request));
    return;
  }

  // Cache only static assets (icons, etc)
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request))
    );
    return;
  }

  // Everything else - network only
  e.respondWith(fetch(e.request));
});
