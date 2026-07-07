/* NAC Pay Tracker — PWA bootstrap (loaded on every page via base.html).
 *
 *   1. Register the root-scoped service worker.
 *   2. While online, fetch the per-user pre-warm list and hand it to the SW
 *      so every month/view is cached for offline.
 *   3. Toggle an "offline — showing last synced data" banner.
 *   4. Warn (don't silently break) when an edit/upload is attempted offline.
 */
(function () {
  "use strict";
  if (!("serviceWorker" in navigator)) return;

  function prewarm() {
    if (!navigator.onLine || !navigator.serviceWorker.controller) return;
    fetch("/offline-manifest.json", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !Array.isArray(data.urls)) return;
        navigator.serviceWorker.controller.postMessage({
          type: "PREWARM",
          urls: data.urls,
        });
      })
      .catch(function () { /* offline or gated — skip */ });
  }

  // ---- "Available offline" indicator ---------------------------------
  // The service worker posts START/PROGRESS/DONE as it pre-warms the cache.
  var statusEl = document.getElementById("offline-status");
  var statusTimer;
  function showStatus(text, ready) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.hidden = false;
    statusEl.classList.toggle("offline-status--ready", !!ready);
    clearTimeout(statusTimer);
    if (ready) {
      statusTimer = setTimeout(function () { statusEl.hidden = true; }, 4000);
    }
  }
  navigator.serviceWorker.addEventListener("message", function (e) {
    var d = e.data || {};
    if (d.type === "PREWARM_START") showStatus("Preparing offline…", false);
    else if (d.type === "PREWARM_PROGRESS") showStatus("Preparing offline… " + d.done + "/" + d.total, false);
    else if (d.type === "PREWARM_DONE") showStatus("✓ Available offline", true);
  });

  window.addEventListener("load", function () {
    navigator.serviceWorker
      .register("/sw.js")
      .then(function () {
        // Give the controller a moment to take over on first load.
        if (navigator.serviceWorker.controller) prewarm();
        navigator.serviceWorker.addEventListener("controllerchange", prewarm);
      })
      .catch(function () { /* registration failed — app still works online */ });
  });

  // ---- offline banner -------------------------------------------------
  var banner = document.getElementById("offline-banner");
  function updateOnline() {
    if (!banner) return;
    banner.hidden = navigator.onLine;
  }
  window.addEventListener("online", function () { updateOnline(); prewarm(); });
  window.addEventListener("offline", updateOnline);
  updateOnline();

  // ---- guard edits/uploads while offline ------------------------------
  // Read-only offline: block form POSTs (reassign/drop/upload/settings) with
  // a clear message instead of a broken navigation.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.tagName !== "FORM") return;
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method === "post" && !navigator.onLine) {
      e.preventDefault();
      alert("You're offline. Changes and uploads need a connection — this view is read-only offline.");
    }
  }, true);
})();
