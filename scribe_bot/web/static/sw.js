// Service worker (lets the phone install Scribe Bot as an app). It has NO fetch handler on purpose (Brian, 2026-11-22:
// taps on talks sometimes took seconds or never opened): with a fetch handler, Chrome had to wake this worker before
// every page change, and on Android a sleeping worker can take seconds (once 172 s) to wake. Static files are cached
// by the browser instead (versioned ?v= URLs, cached for a year). Old caches from the previous version are removed.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
