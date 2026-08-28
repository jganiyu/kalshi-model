self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

// Network-only by design: financial data and API responses are never cached.
self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request, { cache: "no-store" }));
});
