// fireside service worker: what makes the app installable, and what keeps the
// shell reachable if the network blinks. It never touches API calls or audio
// streams, so a cached page can never serve stale book data or stale audio.
const CACHE = 'fireside-shell-v1';
const SHELL = ['/', '/index.html', '/favicon.ico', '/manifest.webmanifest',
               '/icon-192.png', '/icon-512.png', '/icon-maskable-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE)
    .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' }))))
    .then(() => self.skipWaiting())
    .catch(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Anything under an API prefix, or any non-GET, passes straight through.
const PASSTHRU = ['/chapter', '/text', '/library', '/job', '/progress', '/estimate',
  '/stats', '/pron', '/connections', '/prefs', '/position', '/last', '/me',
  '/health', '/plugins', '/voices', '/cover', '/auth', '/import', '/render',
  '/speak', '/claim', '/ack', '/listen', '/credential', '/source-creds',
  '/book', '/stop', '/voices'];

function isShell(path) { return SHELL.indexOf(path) !== -1; }

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  let u;
  try { u = new URL(req.url); } catch (_) { return; }
  if (u.origin !== self.location.origin) return;
  const p = u.pathname;
  if (PASSTHRU.some((x) => p === x || p.indexOf(x + '/') === 0)) return;

  // A page load: try the network first so a fresh build always wins, fall back
  // to the cached shell when offline.
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try { return await fetch(req); }
      catch (_) {
        const c = await caches.open(CACHE);
        return (await c.match('/')) || (await c.match('/index.html')) || Response.error();
      }
    })());
    return;
  }
  // Shell assets: cache first, refreshed in the background.
  if (isShell(p)) {
    e.respondWith((async () => {
      const c = await caches.open(CACHE);
      const hit = await c.match(p);
      if (hit) return hit;
      const net = await fetch(req);
      if (net && net.ok) c.put(p, net.clone());
      return net;
    })());
  }
});
