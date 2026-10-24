// harness service worker — minimal cache-first for the static shell.
// Installs the PWA so users can pin it to their home screen (iOS / Android).
// API responses are NOT cached; only the static assets + the / landing page.
//
// Versioning (Phase 14, 2026-10-17): bump CACHE_NAME to invalidate any
// stale shell installed by an older PWA. Mal's iOS / PWA-installed
// clients were using an old app.js that hit /api/v1/chat (the old
// conversation-id-based endpoint) instead of /api/v1/entities/<name>/chat,
// so sending a message in the HMI silently failed. Bumping the cache
// version forces every installed PWA to fetch the latest bundle.
const CACHE_NAME = 'harness-v8';
const PRECACHE = [
  '/',
  '/static/app.css',
  '/static/app.js',
  '/static/group.js',
  '/static/manifest.json',
  '/static/icon.svg',
  '/static/icon-180.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
      .then(() => {
        // Tell every open page to reload so it picks up the new shell
        return self.clients.matchAll({ includeUncontrolled: true }).then((cs) => {
          for (const c of cs) c.postMessage({ type: 'sw-updated', version: CACHE_NAME });
        });
      })
  );
});

self.addEventListener('message', (event) => {
  // Allow the page to ask the SW to skip waiting (no-op now, but
  // standard pattern for future updates).
  if (event.data && event.data.type === 'skip-waiting') self.skipWaiting();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(req).then((r) => {
        const copy = r.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, copy));
        return r;
      }).catch(() => caches.match(req).then((r) => r || caches.match('/')))
    );
    return;
  }
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req).then((r) => {
      if (r.ok) {
        const copy = r.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, copy));
      }
      return r;
    }))
  );
});
