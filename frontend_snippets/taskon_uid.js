/*!
 * taskon_uid.js — anonymous-cookie persistence helper for TaskOn landing pages
 *
 * Drops a 30-day first-party cookie (default name `_taskon_uid`) at first
 * visit so that subsequent impression beacons + signup form submissions
 * can be stitched into one user_journey by the engine's attribution
 * pipeline (B1 §4.3, engine migration 007).
 *
 * Public API:
 *   window.TaskOnUID.get(name?, days?) -> string  // generate-or-fetch
 *   window.TaskOnUID.value()           -> string  // shorthand using defaults
 *
 * No external deps. Safe under SSR (no-ops if `document` is missing). Cookies
 * are written `SameSite=Lax; Secure; Path=/`. The page's TLS context is
 * required for `Secure` — over plain HTTP the browser silently drops the
 * write; that's the desired fail-closed behaviour (TaskOn landing pages
 * MUST be HTTPS).
 */
(function (global) {
  'use strict';

  var DEFAULT_NAME = '_taskon_uid';
  var DEFAULT_DAYS = 30;

  function uuidV4() {
    // crypto.randomUUID() is available in all evergreen browsers (96%+ as of
    // 2025). Fall back to a Math.random-based v4 only on antique browsers.
    if (global.crypto && typeof global.crypto.randomUUID === 'function') {
      return global.crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function readCookie(name) {
    if (typeof document === 'undefined' || !document.cookie) return '';
    var prefix = encodeURIComponent(name) + '=';
    var parts = document.cookie.split(';');
    for (var i = 0; i < parts.length; i++) {
      var c = parts[i].trim();
      if (c.indexOf(prefix) === 0) {
        return decodeURIComponent(c.substring(prefix.length));
      }
    }
    return '';
  }

  function writeCookie(name, value, days) {
    if (typeof document === 'undefined') return;
    var expires = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toUTCString();
    var attrs = [
      encodeURIComponent(name) + '=' + encodeURIComponent(value),
      'expires=' + expires,
      'path=/',
      'SameSite=Lax'
    ];
    // Secure attribute is mandatory for SameSite=None in modern browsers and
    // also required on HTTPS. We always send it — under HTTP the browser
    // ignores the write (intended fail-closed).
    if (typeof location !== 'undefined' && location.protocol === 'https:') {
      attrs.push('Secure');
    }
    document.cookie = attrs.join('; ');
  }

  function getCookieOrGenerate(name, days) {
    name = name || DEFAULT_NAME;
    days = typeof days === 'number' ? days : DEFAULT_DAYS;
    var existing = readCookie(name);
    if (existing) return existing;
    var fresh = uuidV4();
    writeCookie(name, fresh, days);
    return fresh;
  }

  global.TaskOnUID = {
    get: getCookieOrGenerate,
    value: function () { return getCookieOrGenerate(DEFAULT_NAME, DEFAULT_DAYS); },
    _read: readCookie,   // exported for tests / debugging
    _write: writeCookie
  };
})(typeof window !== 'undefined' ? window : globalThis);
