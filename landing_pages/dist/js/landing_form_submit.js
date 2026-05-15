/*!
 * landing_form_submit.js — POST landing-page form submissions to TaskOn ingest.
 *
 * Drop this AFTER `taskon_uid.js` on each of the 3 landing pages:
 *   /benchmark-report
 *   /free-diagnostic
 *   /growth-playbook
 *
 * It binds to the first form that has [data-taskon-signup] OR (fallback)
 * the first <form> on the page, captures email + cookie_id + page metadata,
 * POSTs to `${TASKON_INGEST_BASE}/api/landing-signup`, then either redirects
 * to the configured thank-you page or replaces the form with a success
 * message.
 *
 * Configuration via global object set BEFORE this script loads:
 *
 *   window.TASKON_INGEST_BASE = 'https://ingest.taskon.xyz';   // required
 *   window.TASKON_THANK_YOU_URL = '/thank-you';                // optional
 *   window.TASKON_FORM_SELECTOR = '[data-taskon-signup]';      // optional override
 *
 * Server contract: `lib.utm.parse_utm` on the engine requires ALL 5 UTM
 * segments (source / medium / campaign / content / term) to attribute the
 * journey row. Make sure the landing-page URL carries the full set in the
 * inbound link.
 */
(function (global) {
  'use strict';

  function getCookieId() {
    if (global.TaskOnUID && typeof global.TaskOnUID.value === 'function') {
      return global.TaskOnUID.value();
    }
    return '';
  }

  function showError(form, message) {
    var slot = form.querySelector('[data-taskon-error]');
    if (slot) {
      slot.textContent = message;
      slot.style.display = '';
    } else {
      console.warn('[TaskOn] form error:', message);
    }
  }

  function showSuccess(form) {
    var slot = form.querySelector('[data-taskon-success]');
    if (slot) {
      form.style.display = 'none';
      slot.style.display = '';
      return;
    }
    form.innerHTML = '<p>已收到。我们 24h 内联系你。</p>';
  }

  function isValidEmail(s) {
    return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(s || '').trim());
  }

  async function submit(form, ev) {
    ev.preventDefault();

    var emailInput = form.querySelector('input[type="email"], input[name="email"]');
    var email = emailInput ? emailInput.value : '';
    if (!isValidEmail(email)) {
      showError(form, '请输入有效的邮箱。');
      return;
    }

    var ingestBase = global.TASKON_INGEST_BASE;
    if (!ingestBase) {
      showError(form, '配置缺失：TASKON_INGEST_BASE');
      return;
    }

    var payload = {
      email: email.trim(),
      cookie_id: getCookieId(),
      page_path: location.pathname,
      url: location.href,
      referrer: document.referrer || ''
    };

    var submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;

    try {
      var resp = await fetch(ingestBase.replace(/\/+$/, '') + '/api/landing-signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        credentials: 'omit',
        keepalive: true
      });
      if (!resp.ok) {
        var body = await resp.text().catch(function () { return ''; });
        console.error('[TaskOn] signup HTTP', resp.status, body);
        showError(form, '提交失败，请稍后再试。');
        if (submitBtn) submitBtn.disabled = false;
        return;
      }
      // Optional: dispatch a custom event so analytics can listen.
      try {
        document.dispatchEvent(new CustomEvent('taskon:signup', { detail: { email: payload.email } }));
      } catch (e) { /* IE11-era browsers; ignore */ }

      if (global.TASKON_THANK_YOU_URL) {
        location.href = global.TASKON_THANK_YOU_URL;
      } else {
        showSuccess(form);
      }
    } catch (e) {
      console.error('[TaskOn] signup network error', e);
      showError(form, '网络错误，请稍后重试。');
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  function bind() {
    var selector = global.TASKON_FORM_SELECTOR || '[data-taskon-signup]';
    var form = document.querySelector(selector) || document.querySelector('form');
    if (!form) {
      console.warn('[TaskOn] no form found for selector', selector);
      return;
    }
    form.addEventListener('submit', function (ev) { return submit(form, ev); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})(typeof window !== 'undefined' ? window : globalThis);
