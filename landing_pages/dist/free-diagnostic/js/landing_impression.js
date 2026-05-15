/*!
 * landing_impression.js — anonymous-impression beacon for TaskOn landing pages.
 *
 * Fires once on every landing-page load (load event, idempotent via a
 * sessionStorage marker so SPA navigations / browser back/forward don't
 * double-count). Sends an `impression_only=true` POST to
 * `${TASKON_INGEST_BASE}/api/landing-signup` with the persistent cookie_id
 * + UTM-bearing URL so the engine can later stitch this view to a future
 * signup (B1 §4.3, engine T1 impression branch).
 *
 * Drop AFTER `taskon_uid.js`. Configuration mirrors `landing_form_submit.js`:
 *
 *   window.TASKON_INGEST_BASE = 'https://ingest.taskon.xyz';  // required
 *
 * Uses `navigator.sendBeacon` when available so the request survives
 * navigation (user clicks an outbound link 1 ms after onload). Falls back
 * to `fetch(..., keepalive: true)` on browsers without sendBeacon.
 *
 * Privacy: this snippet sends NO PII — only the cookie_id (a random UUID),
 * the URL path, the URL query string (UTM), and the referrer. Email never
 * touches this endpoint until the form submits via landing_form_submit.js.
 */
(function (global) {
  'use strict';

  var STORAGE_KEY = '_taskon_impression_fired';

  function getCookieId() {
    if (global.TaskOnUID && typeof global.TaskOnUID.value === 'function') {
      return global.TaskOnUID.value();
    }
    return '';
  }

  function alreadyFiredThisSession() {
    try {
      // Use sessionStorage so refresh inside the same tab is deduped, but a
      // new tab / new session still counts as a fresh impression.
      return sessionStorage.getItem(STORAGE_KEY) === '1';
    } catch (e) {
      return false;
    }
  }

  function markFired() {
    try {
      sessionStorage.setItem(STORAGE_KEY, '1');
    } catch (e) { /* private mode / storage disabled — best-effort only */ }
  }

  function sendImpression() {
    if (alreadyFiredThisSession()) return;
    var ingestBase = global.TASKON_INGEST_BASE;
    if (!ingestBase) {
      console.warn('[TaskOn] TASKON_INGEST_BASE not set; skipping impression beacon');
      return;
    }
    var cookie_id = getCookieId();
    if (!cookie_id) {
      console.warn('[TaskOn] cookie_id unavailable; skipping impression');
      return;
    }

    var url = ingestBase.replace(/\/+$/, '') + '/api/landing-signup';
    var payload = JSON.stringify({
      impression_only: true,
      cookie_id: cookie_id,
      page_path: location.pathname,
      url: location.href,
      referrer: document.referrer || ''
    });

    // sendBeacon is fire-and-forget but bounded to ~64 KB. Our payload is
    // <1 KB so this is the right primitive — the request survives the user
    // immediately clicking an outbound link.
    var sent = false;
    try {
      if (navigator && typeof navigator.sendBeacon === 'function') {
        var blob = new Blob([payload], { type: 'application/json' });
        sent = navigator.sendBeacon(url, blob);
      }
    } catch (e) { /* fall through to fetch */ }

    if (!sent) {
      try {
        fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: payload,
          credentials: 'omit',
          keepalive: true,
          mode: 'no-cors'
        }).catch(function () { /* swallow — best-effort */ });
      } catch (e) { /* nothing to do */ }
    }
    markFired();
  }

  // Fire as soon as DOM is ready so the beacon goes out even on fast
  // bounces. Wrap in try/catch so any error here NEVER blocks the page.
  function start() {
    try { sendImpression(); } catch (e) { console.warn('[TaskOn] impression', e); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})(typeof window !== 'undefined' ? window : globalThis);
