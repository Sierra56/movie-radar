const CACHE = 'movie-radar-v1';
const APP_SHELL = ['/', '/manifest.json', '/static/icon.svg'];

self.addEventListener('install', (e) => {
    e.waitUntil(caches.open(CACHE).then((c) => c.addAll(APP_SHELL)));
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', (e) => {
    const url = new URL(e.request.url);
    if (e.request.method !== 'GET') return;

    // Постеры и статика — cache-first
    if (url.pathname.startsWith('/posters/') || url.pathname.startsWith('/static/')) {
        e.respondWith(
            caches.match(e.request).then(
                (cached) =>
                    cached ||
                    fetch(e.request).then((res) => {
                        const copy = res.clone();
                        caches.open(CACHE).then((c) => c.put(e.request, copy));
                        return res;
                    })
            )
        );
        return;
    }

    // Навигация — network-first с офлайн-фолбэком на кэш '/'
    if (e.request.mode === 'navigate') {
        e.respondWith(
            fetch(e.request)
                .then((res) => {
                    const copy = res.clone();
                    caches.open(CACHE).then((c) => c.put('/', copy));
                    return res;
                })
                .catch(() => caches.match('/'))
        );
        return;
    }
});