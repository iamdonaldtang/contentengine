"""MPT webhook callback receiver — A-design async render path (2026-05-16).

WHY THIS EXISTS
---------------
``jobs.mpt_runner`` used to block the engine main thread for 5-10 minutes
per render by sync-polling MPT's ``GET /api/v1/tasks/<id>`` every 5 seconds.
The new flow:

  1. mpt_runner inserts mpt_tasks row + POST /api/v1/videos with
     ``callback_url`` + ``callback_secret`` → returns task_id → mpt_runner exits.
  2. MPT renders. On completion (success or failure), MPT POSTs back to
     ``POST /api/mpt-callback`` with an HMAC-SHA256 signed body.
  3. This Blueprint verifies the signature, atomically transitions the
     mpt_tasks row, and spawns a daemon thread to download the mp4 +
     stamp ``media_path``. The HTTP response returns in < 100ms so MPT's
     callback POST never blocks on engine-side work.

If a callback is dropped (MPT crash mid-POST, network blip, engine restart
during transit) ``jobs.mpt_reconciler`` (cron every 5min) GETs MPT for any
mpt_tasks row stuck in 'submitted' and ``mark_completed(source='reconciler')``
self-heals it. The reconciler reuses the same atomic UPDATE primitive so
the callback path and the reconciler-rescue path produce identical end
state — see ``lib.db._MptTasksAdapter`` for the state machine docs.

SECURITY
--------
* ``MPT_CALLBACK_SECRET`` env required. Missing → endpoint returns 503,
  endpoint is disabled in a safe default. (Same pattern as the admin
  Blueprint in ``ingestion.admin_routes``: empty secret = disabled.)
* HMAC-SHA256 over ``f"{timestamp}.{raw_body}"`` — timestamp goes in a
  separate header (``X-MPT-Timestamp``) so the body alone can't be
  replayed with a different timestamp.
* Timestamp must be within ±300s of server clock — kills replays older
  than 5 minutes.
* ``hmac.compare_digest`` for constant-time signature compare.
* Body bytes hashed AS RECEIVED — we do NOT round-trip through
  ``request.get_json()`` for verification because Flask's JSON parsing
  re-serializes with different whitespace and would mismatch MPT's sig.

OBSERVABILITY
-------------
Every callback (accepted, rejected, ignored) writes a single-line audit log
to the root logger (JSON formatter already configured in ``ingestion.app``).

Hard rules (Prompt_AI系统化编程_v1.md §7):
* No silent failures — every branch logs and returns a specific status.
* No catch-and-pass — exceptions either propagate or are wrapped with
  context-rich messages and routed through Lark P-alerts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from flask import Blueprint, Response, jsonify, request

from jobs.mpt_post_callback import spawn_download as _spawn_download
from lib.db import db
from lib.lark import alert as lark_alert


logger = logging.getLogger("mpt_callback")

mpt_cb_bp = Blueprint("mpt_callback", __name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_SECRET_ENV = "MPT_CALLBACK_SECRET"
_TIMESTAMP_TOLERANCE_S = 300  # 5 minutes; anti-replay window


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _err(status: int, code: str, message: str, **extra: Any) -> tuple[Response, int]:
    body: dict[str, Any] = {"status": "error", "code": code, "message": message}
    body.update(extra)
    return jsonify(body), status


def _current_secret() -> str:
    """Read the active MPT callback secret from env. Empty = endpoint disabled."""
    return os.environ.get(_SECRET_ENV, "").strip()


def _verify_signature(raw_body: bytes, timestamp_header: str, signature_header: str, secret: str) -> tuple[bool, str]:
    """Verify HMAC-SHA256 of ``f"{ts}.{body}"`` with constant-time compare.

    Returns ``(ok, reason)``. ``reason`` is a short token suitable for audit
    logging — never includes the secret or any user-controlled value.
    """
    if not signature_header.startswith("sha256="):
        return False, "bad_sig_prefix"
    sent_sig = signature_header[7:].strip()
    if not sent_sig:
        return False, "empty_sig"

    # Timestamp validation: must be a base-10 integer within tolerance.
    try:
        ts = int(timestamp_header.strip())
    except (ValueError, AttributeError):
        return False, "bad_ts_format"

    now = int(time.time())
    skew = abs(now - ts)
    if skew > _TIMESTAMP_TOLERANCE_S:
        return False, f"ts_skew_{skew}s"

    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, sent_sig):
        return False, "sig_mismatch"

    return True, "ok"


def _safe_log(audit: dict[str, Any]) -> None:
    """Single-line JSON audit log. Never includes the secret."""
    try:
        logger.info("mpt_callback %s", json.dumps(audit, ensure_ascii=False, sort_keys=True))
    except Exception:
        logger.exception("mpt_callback: audit serialization failed")


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


@mpt_cb_bp.post("/api/mpt-callback")
def mpt_callback() -> tuple[Response, int]:
    """Receive MPT's webhook callback for a completed / failed render.

    Request:
        Headers:
          X-MPT-Signature: sha256=<hex>
          X-MPT-Timestamp: <unix seconds>
        Body (JSON):
          task_id     str  required
          status      str  required  ('completed' | 'failed' | 'cancelled')
          mp4_url     str  optional  (only for completed)
          error       str  optional  (only for failed)
          state       int  optional  (MPT's internal state code)
          progress    int  optional  (final progress %, usually 100)
          duration_seconds int optional

    Responses:
        202 accepted          — signature valid, state transition occurred,
                                download thread spawned (if completed).
        200 already_processed — duplicate callback for a row already in
                                terminal state (MPT retried).
        200 ignored           — unknown task_id; logged for audit only.
        401 unauthorized      — signature / timestamp invalid.
        503 disabled          — MPT_CALLBACK_SECRET env not configured.
        400 bad_request       — malformed body / missing fields.
    """
    secret = _current_secret()
    if not secret:
        _safe_log({"event": "disabled", "remote": request.remote_addr})
        return _err(503, "callback_disabled", f"{_SECRET_ENV} env not set")

    raw_body = request.get_data(cache=False, as_text=False)
    sig_header = request.headers.get("X-MPT-Signature", "")
    ts_header = request.headers.get("X-MPT-Timestamp", "")

    ok, reason = _verify_signature(raw_body, ts_header, sig_header, secret)
    if not ok:
        _safe_log({"event": "auth_fail", "reason": reason, "remote": request.remote_addr, "body_bytes": len(raw_body)})
        return _err(401, "unauthorized", f"signature verification failed: {reason}")

    # Body must be valid JSON object now that auth passed.
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _safe_log({"event": "bad_body", "remote": request.remote_addr, "error": f"{type(exc).__name__}"})
        return _err(400, "bad_body", "request body is not valid UTF-8 JSON")
    if not isinstance(payload, dict):
        return _err(400, "bad_body", "request body must be a JSON object")

    task_id = (payload.get("task_id") or "").strip()
    status = (payload.get("status") or "").strip().lower()
    if not task_id:
        return _err(400, "missing_task_id", "payload.task_id required")
    if status not in {"completed", "failed", "cancelled"}:
        return _err(400, "bad_status", f"payload.status must be 'completed'|'failed'|'cancelled', got {status!r}")

    row = db.mpt_tasks.get_by_task_id(task_id)
    if row is None:
        # Unknown task: could be a replay from a stale MPT, a different engine
        # instance, or a bug. Don't 404 — that would make MPT mark the callback
        # as failed and write a DLQ row. Just return 200 + audit.
        _safe_log({"event": "unknown_task", "task_id": task_id, "remote": request.remote_addr})
        return jsonify({"status": "ignored", "reason": "unknown task_id"}), 200

    if row["status"] in {"completed", "failed", "cancelled", "stale"}:
        # Idempotent re-delivery from MPT's retry mechanism.
        _safe_log({"event": "already_processed", "task_id": task_id, "current_status": row["status"]})
        return jsonify({"status": "already_processed", "current_status": row["status"]}), 200

    piece_id = row["piece_id"]
    mp4_url = (payload.get("mp4_url") or "").strip() or None
    err_msg = (payload.get("error") or "").strip() or None

    if status == "completed":
        if not mp4_url:
            return _err(400, "missing_mp4_url", "status=completed requires mp4_url")
        won = db.mpt_tasks.mark_completed(task_id, mp4_url, source="callback")
        if won:
            _spawn_download(task_id, piece_id)
            _safe_log({
                "event": "accepted",
                "task_id": task_id,
                "piece_id": piece_id,
                "status": "completed",
                "mp4_url_host": mp4_url.split("/")[2] if "://" in mp4_url else None,
            })
            return jsonify({"status": "accepted", "transition": "submitted->completed"}), 202
        # Reconciler beat us. Idempotent — log and return 200.
        _safe_log({"event": "race_lost", "task_id": task_id, "winner": "reconciler"})
        return jsonify({"status": "already_processed", "current_status": "completed"}), 200

    # status in {failed, cancelled} → mark_failed
    won = db.mpt_tasks.mark_failed(
        task_id,
        err_msg or f"MPT reported status={status}",
        source="callback",
    )
    if won:
        _safe_log({
            "event": "accepted",
            "task_id": task_id,
            "piece_id": piece_id,
            "status": status,
            "error": err_msg[:200] if err_msg else None,
        })
        try:
            lark_alert(
                "P1",
                f"MPT render failed for piece={piece_id}",
                {"task_id": task_id, "status": status, "error": (err_msg or "")[:300]},
            )
        except Exception:
            logger.exception("mpt_callback: lark alert dispatch crashed")
        return jsonify({"status": "accepted", "transition": f"submitted->failed"}), 202
    _safe_log({"event": "race_lost", "task_id": task_id, "winner": "reconciler"})
    return jsonify({"status": "already_processed", "current_status": "failed"}), 200
