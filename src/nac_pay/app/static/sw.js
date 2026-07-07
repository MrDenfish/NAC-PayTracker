/* NAC Pay Tracker — service worker (read-only offline).
 *
 * Strategy:
 *   - App shell (CSS/JS/icons/offline page) is precached on install.
 *   - Page navigations are NETWORK-FIRST: always fresh online, a cached copy
 *     when offline, the offline page as a last resort. Successful page loads
 *     are cached so anything you visit online is available offline.
 *   - A PREWARM message from the page seeds the cache with every month/view
 *     so the whole app is browsable offline without visiting each page first.
 *   - Static assets are CACHE-FIRST (they're content-hash versioned).
 *
 * __CACHE_VERSION__ is substituted server-side (see pwa.py) with the static
 * asset hash, so a deploy that changes any asset rotates the cache.
 */

const VERSION = "__CACHE_VERSION__";
const CACHE = "nacpay-" + VERSION;
const OFFLINE_URL = "/static/offline.html";

const SHELL = [
  OFFLINE_URL,
  "/static/pwa-register.js?v=" + VERSION,
  "/static/styles.css?v=" + VERSION,
  "/static/icons/icon-192.png?v=" + VERSION,
  "/static/icons/icon-512.png?v=" + VERSION,
  "/static/icons/apple-touch-icon.png?v=" + VERSION,
  "/manifest.webmanifest",
];

/* Paths that must never be cached — auth/billing/webhooks and the PWA
 * control files. These always go straight to the network. */
const NO_CACHE_PREFIXES = [
  "/login", "/logout", "/signup", "/forgot",
  "/verify/", "/reset/", "/auth", "/billing", "/webhooks/",
  "/sw.js", "/manifest.webmanifest", "/offline-manifest.json",
];

function isNoCache(url) {
  return NO_CACHE_PREFIXES.some((p) => url.pathname === p || url.pathname.startsWith(p));
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // Cache assets individually so one missing file can't fail the whole
      // install.
      Promise.allSettled(SHELL.map((url) => cache.add(url)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k.startsWith("nacpay-") && k !== CACHE).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never intercept edits/uploads
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // cross-origin: leave alone
  if (isNoCache(url)) return; // auth/billing/control files: straight to network

  const isNavigation =
    req.mode === "navigate" ||
    (req.headers.get("accept") || "").includes("text/html");

  if (isNavigation) {
    event.respondWith(networkFirst(req));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(req));
  }
});

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const resp = await fetch(req);
    if (resp && resp.status === 200 && (resp.headers.get("content-type") || "").includes("text/html")) {
      cache.put(req, resp.clone());
    }
    return resp;
  } catch (err) {
    const cached = await cache.match(req);
    if (cached) return cached;
    const offline = await cache.match(OFFLINE_URL);
    return offline || new Response("Offline", { status: 503, statusText: "Offline" });
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  if (cached) return cached;
  try {
    const resp = await fetch(req);
    if (resp && resp.status === 200) cache.put(req, resp.clone());
    return resp;
  } catch (err) {
    return cached || new Response("", { status: 504 });
  }
}

/* PREWARM: the page hands us the per-user URL list from /offline-manifest.json
 * and we fetch each into the cache (throttled) so every month/view is
 * available offline. We post START/PROGRESS/DONE back to the page so it can
 * show an "Available offline" indicator. */
let prewarming = false;

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "PREWARM" && Array.isArray(data.urls)) {
    event.waitUntil(prewarm(data.urls));
  }
});

async function notifyClients(msg) {
  const clients = await self.clients.matchAll({ includeUncontrolled: true, type: "window" });
  for (const client of clients) client.postMessage(msg);
}

async function prewarm(urls) {
  if (prewarming) return; // don't double-run when load + online fire together
  prewarming = true;
  try {
    const cache = await caches.open(CACHE);
    const total = urls.length;
    await notifyClients({ type: "PREWARM_START", total: total });
    const CONCURRENCY = 4;
    let i = 0, done = 0, cached = 0;
    async function worker() {
      while (i < urls.length) {
        const url = urls[i++];
        try {
          const resp = await fetch(url, { credentials: "same-origin" });
          if (resp.ok && (resp.headers.get("content-type") || "").includes("text/html")) {
            await cache.put(url, resp.clone());
            cached++;
          }
        } catch (err) {
          /* offline or failed — skip; next online sync will retry */
        }
        done++;
        if (done % 5 === 0 || done === total) {
          await notifyClients({ type: "PREWARM_PROGRESS", done: done, total: total });
        }
      }
    }
    await Promise.all(Array.from({ length: CONCURRENCY }, worker));
    await notifyClients({ type: "PREWARM_DONE", cached: cached, total: total });
  } finally {
    prewarming = false;
  }
}
