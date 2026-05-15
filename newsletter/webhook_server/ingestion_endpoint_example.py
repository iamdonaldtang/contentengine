"""Donald 桌面运行的 Ingestion Endpoint  (LEGACY — back-reference only)
========================================================================

⚠️ SUPERSEDED 2026-05-13: production ingestion now lives in
``engine/ingestion/app.py`` and runs in Docker behind Cloudflare Tunnel.
That service exposes ``/api/landing-signup``, ``/api/listmonk-webhook``,
``/api/ses-bounce``, ``/health`` and ``/metrics`` with HMAC verification,
JSON logging and Prometheus metrics. THIS FILE IS KEPT FOR HISTORICAL
REFERENCE / ONE-OFF MANUAL TESTING ONLY — do not deploy.

Differences vs the new service:
  * single ``/newsletter_event`` endpoint that switched on ``event_type``
  * direct ``sqlite3.connect()`` (now banned — Prompt §7)
  * hardcoded D-drive path (now banned — Prompt §7)

If you need to run this for local testing it now uses ``lib.db.db`` and
honours ``SQLITE_PATH`` env (default: engine/runtime/state.db relative to
this file).

Original deployment notes (preserved verbatim):
  1. 此脚本运行在 Donald 桌面（Python 3.12+）
  2. 用 Cloudflare Tunnel 把本机 5051 端口暴露为公网 URL
       cloudflared tunnel --url http://localhost:5051
  3. 把得到的 URL 填到 VPS .env 的 DONALD_DESKTOP_INGESTION_URL
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

# Ensure the engine root is on sys.path so `from lib.db import db` works
# whether this script is launched directly or imported.
_ENGINE_ROOT = Path(__file__).resolve().parents[2]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from lib.db import db  # noqa: E402  (path tweak above)


# ============================================================
# Configuration — all via env, no hardcoded paths.
# ============================================================
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")
PORT = int(os.environ.get("PORT", "5051"))

if not WEBHOOK_SHARED_SECRET:
    raise RuntimeError(
        "WEBHOOK_SHARED_SECRET env not set — refusing to start unauthenticated"
    )

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def verify(req) -> bool:
    """HMAC-SHA256 签名校验（与 VPS webhook_server 共享 secret）。"""
    received = req.headers.get("X-TaskOn-Signature", "")
    if not received:
        return False
    body = req.get_data()
    expected = hmac.new(
        WEBHOOK_SHARED_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(received, expected)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.route("/newsletter_event", methods=["POST"])
def newsletter_event():
    """主接收 endpoint — switches on event_type."""
    if not verify(request):
        logger.warning("invalid signature from %s", request.remote_addr)
        return jsonify({"error": "invalid signature"}), 401

    event = request.get_json(silent=True) or {}
    etype = event.get("event_type", "")
    email = event.get("subscriber_email", "") or ""

    try:
        if etype == "campaign.send":
            db.execute(
                """
                INSERT INTO newsletter_campaigns
                    (campaign_id, send_time, subject_a, emails_sent, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    emails_sent = excluded.emails_sent,
                    fetched_at  = excluded.fetched_at
                """,
                (
                    str(event.get("campaign_id")),
                    event.get("timestamp"),
                    event.get("subject"),
                    event.get("sent_count"),
                    _utc_now(),
                ),
            )

        elif etype in ("campaign.open", "campaign.click"):
            user_id = (
                hashlib.sha256(email.lower().encode("utf-8")).hexdigest() if email else ""
            )
            action = etype.split(".", 1)[1]
            db.user_journey.insert(
                user_id=user_id,
                action=action,
                utm_source="listmonk",
                utm_medium="edm",
                utm_campaign=str(event.get("campaign_id", "")),
                page_path=event.get("url", ""),
                timestamp=event.get("timestamp"),
                raw_data=json.dumps(event, ensure_ascii=False),
            )

        elif etype in ("subscriber.bounced", "ses.bounce"):
            db.execute(
                """
                UPDATE leads
                   SET bounced    = 1,
                       sql_status = 'rejected',
                       notes      = COALESCE(notes, '') || ' | bounced: ' || ?
                 WHERE email = ?
                """,
                (event.get("bounce_type", "unknown"), email),
            )

        elif etype == "ses.complaint":
            db.execute(
                """
                UPDATE leads
                   SET bounced    = 1,
                       sql_status = 'rejected',
                       notes      = COALESCE(notes, '') || ' | COMPLAINT (P0)'
                 WHERE email = ?
                """,
                (email,),
            )
            db.publish_failures.insert(
                severity="P0",
                source="ses",
                failure_type="complaint",
                failure_detail=json.dumps(event, ensure_ascii=False),
            )

        else:
            logger.warning("unknown event_type: %s", etype)

        logger.info("processed %s email=%s", etype, email)
        return jsonify({"status": "ok"}), 200

    except Exception as exc:
        logger.exception("ingestion failed event=%s", etype)
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/health", methods=["GET"])
def health():
    """健康检查 / VPS webhook_server 会调用此 endpoint。"""
    try:
        db.fetchone("SELECT 1")
        sqlite_ok = True
    except Exception:
        logger.exception("health check sqlite failed")
        sqlite_ok = False
    return (
        jsonify(
            {
                "status": "ok" if sqlite_ok else "degraded",
                "service": "taskon-newsletter-ingestion-legacy",
                "sqlite_path": str(db.path),
                "sqlite_ok": sqlite_ok,
            }
        ),
        200 if sqlite_ok else 503,
    )


if __name__ == "__main__":
    logger.info("starting LEGACY ingestion endpoint on port %d", PORT)
    logger.info("sqlite path: %s", db.path)
    # 仅监听 127.0.0.1 — 走 Cloudflare Tunnel 暴露公网
    app.run(host="127.0.0.1", port=PORT, debug=False)
