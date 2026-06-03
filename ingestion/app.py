"""TaskOn Marketing Engine · ingestion HTTP service.

Single Flask app exposing the public webhook + landing-signup surface:

* ``POST /api/landing-signup``  — landing page form submissions
* ``POST /api/listmonk-webhook`` — Listmonk Open/Click/Bounce events
* ``POST /api/ses-bounce``       — AWS SES SNS Bounce/Complaint envelopes
* ``GET  /health``               — liveness / SQLite reachability probe
* ``GET  /metrics``              — Prometheus exposition (row counts + heartbeat)

Deployment
----------
Local dev: ``python -m ingestion.app`` (werkzeug, single thread).
Production: ``gunicorn ingestion.wsgi:app`` inside Docker; HTTPS terminated
upstream by Cloudflare Tunnel (no TLS cert juggling needed in the container).

Hard rules (Prompt_AI系统化编程_v1.md §7)
---------------------------------------
* No silent failures — every handler logs + returns a structured error JSON.
* No hardcoded secrets — all knobs read from environment.
* No bare ``print()`` — JSON logging to stdout.
* No direct ``sqlite3.connect()`` — only ``from lib.db import db``.
* No ``requests.*`` without ``timeout=``.
* Full type hints (Python 3.12).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

# ``lib`` is on PYTHONPATH (engine root) in both dev and the container image.
from lib.db import db
from lib.lark import alert as lark_alert
from lib.utm import parse_utm

# Admin API Blueprint — Bearer-protected admin endpoints for Cowork remote control.
# Mounted at /admin/*. Disabled unless ADMIN_API_TOKEN env var is set (safe default).
from ingestion.admin_routes import admin_bp

# MPT callback Blueprint — receives MoneyPrinterTurbo's async webhook on render
# completion (A-design 2026-05-16, S3). Mounted at /api/mpt-callback. Disabled
# unless MPT_CALLBACK_SECRET env var is set (safe default).
from ingestion.mpt_callback import mpt_cb_bp

# Media-serve Blueprint — public signed-URL endpoint at /api/media/<piece>/<file>.
# Postiz YouTube provider fetches mp4 from this route via the cloudflared tunnel.
# HMAC-signed URLs only; unsigned access returns 401. (2026-05-16 piece-02 fix.)
from ingestion.media_routes import media_bp


# --------------------------------------------------------------------------- #
# Logging — single-line JSON to stdout so docker/CF log aggregation works.
# --------------------------------------------------------------------------- #


class _JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter (no external dep)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in getattr(record, "extra_fields", {}).items():
            payload[k] = v
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging() -> None:
    root = logging.getLogger()
    if any(getattr(h, "_taskon_json", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler._taskon_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


_configure_logging()
logger = logging.getLogger("ingestion")


# --------------------------------------------------------------------------- #
# Configuration / constants
# --------------------------------------------------------------------------- #

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

INGESTION_HMAC_SECRET_ENV = "INGESTION_HMAC_SECRET"
LISTMONK_HMAC_SECRET_ENV = "LISTMONK_WEBHOOK_SECRET"


# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 << 20  # 16 MiB（配图上传 phase2；文本端点远小于此）

# ----------------------------------------------------------------------- #
# CORS · landing page JS (https://taskon.xyz) POSTs to ingest.taskon.xyz.
# Allow origins are env-configurable for staging / preview deployments
# (CF Pages preview URLs etc). Default = production taskon.xyz only —
# never use "*" wildcard which would let any page forge submissions.
# ----------------------------------------------------------------------- #
_default_origins = "https://taskon.xyz"
_cors_origins = [
    o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", _default_origins).split(",")
    if o.strip()
]
CORS(
    app,
    resources={
        r"/api/landing-signup": {
            "origins": _cors_origins,
            "methods": ["POST", "OPTIONS"],
            "allow_headers": ["Content-Type", "X-Requested-With", "X-Signature"],
            "expose_headers": [],
            # ★ landing_form_submit.js uses fetch({credentials:'include'}) so
            # CORS must echo Access-Control-Allow-Credentials: true. This is
            # safe because _cors_origins is an explicit allow-list (no '*').
            "supports_credentials": True,
            "max_age": 3600,
        },
    },
)

# Register admin Blueprint (Bearer-protected, /admin/* prefix).
# All admin routes are no-op (401) unless ADMIN_API_TOKEN env var is configured.
app.register_blueprint(admin_bp)

# Register MPT callback Blueprint (/api/mpt-callback). Returns 503 if
# MPT_CALLBACK_SECRET env is not set (safe default — endpoint disabled).
app.register_blueprint(mpt_cb_bp)

# Register signed-URL media Blueprint (/api/media/<piece_id>/<filename>).
# Unsigned / stale requests rejected with 401.
app.register_blueprint(media_bp)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _is_valid_email(email: str | None) -> bool:
    if not email or not isinstance(email, str):
        return False
    return bool(EMAIL_RE.match(email.strip()))


def _verify_hmac(secret_env: str, body: bytes, header_value: str | None) -> bool:
    """Constant-time HMAC verification.

    Returns ``True`` if no secret is configured (HMAC optional) OR the
    signature matches. Header value may be raw hex or ``sha256=<hex>``.
    """
    secret = os.environ.get(secret_env, "").strip()
    if not secret:
        return True  # HMAC not enforced
    if not header_value:
        return False
    received = header_value.strip()
    if received.lower().startswith("sha256="):
        received = received.split("=", 1)[1].strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(received, expected)
    except Exception:
        logger.exception("hmac compare failed unexpectedly")
        return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(status: int, code: str, message: str, **extra: Any) -> tuple[Response, int]:
    body: dict[str, Any] = {"status": "error", "code": code, "message": message}
    body.update(extra)
    return jsonify(body), status


# --------------------------------------------------------------------------- #
# /api/landing-signup
# --------------------------------------------------------------------------- #


@app.post("/api/landing-signup")
def landing_signup() -> tuple[Response, int]:
    """Accept landing-page form submissions AND anonymous impression beacons.

    Two modes (selected by the ``impression_only`` flag in the JSON body):

    1. **Form submission** (``impression_only`` absent/false) — the historical
       behaviour. Requires a valid ``email`` field. Writes one ``leads`` row
       (UPSERT by email) plus one ``user_journey`` row with ``action='signup'``.
       Also upserts a ``cookie_email_map`` row when ``cookie_id`` is present so
       the attribution engine can stitch anonymous impressions later.

    2. **Impression beacon** (``impression_only=True``) — fired by the landing
       page on ``window.load`` with a persistent ``cookie_id`` only. Requires
       ``cookie_id`` (returns 400 otherwise). Writes a single ``user_journey``
       row with ``action='impression'`` and ``user_id=cookie_id``. **Never**
       touches the ``leads`` table.

    Both modes parse UTM from ``url`` and persist the full payload as
    ``raw_data`` JSON for forensics.
    """
    raw_body = request.get_data() or b""
    if not _verify_hmac(INGESTION_HMAC_SECRET_ENV, raw_body, request.headers.get("X-Signature")):
        logger.warning("landing_signup: HMAC mismatch from %s", request.remote_addr)
        return _err(401, "bad_signature", "X-Signature mismatch")

    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        logger.exception("landing_signup: invalid JSON")
        return _err(400, "bad_json", "request body is not valid JSON")

    impression_only = bool(payload.get("impression_only", False))
    email = (payload.get("email") or "").strip()
    page_path = payload.get("page_path") or ""
    url = payload.get("url") or ""
    referrer = payload.get("referrer") or ""
    cookie_id = (payload.get("cookie_id") or "").strip()

    parsed = parse_utm(url) if url else None
    utm_source = parsed.source if parsed else None
    utm_medium = parsed.medium if parsed else None
    utm_campaign = parsed.campaign if parsed else None
    utm_content = parsed.content if parsed else None
    utm_term = parsed.term if parsed else None

    now_iso = _utc_now_iso()

    # ---- Branch A: impression-only (anonymous beacon) ------------------- #
    if impression_only:
        # cookie_id is mandatory for impression beacons — otherwise we cannot
        # stitch this view to a future signup.
        if not cookie_id:
            return _err(400, "bad_cookie_id", "impression_only requires cookie_id")
        try:
            db.user_journey.insert(
                user_id=cookie_id,
                action="impression",
                utm_source=utm_source,
                utm_medium=utm_medium,
                utm_campaign=utm_campaign,
                utm_content=utm_content,
                utm_term=utm_term,
                referrer=referrer,
                page_path=page_path,
                timestamp=now_iso,
                raw_data=json.dumps(payload, ensure_ascii=False),
            )
        except Exception as exc:
            logger.exception("landing_signup impression: db failure cookie=%s", cookie_id[:12])
            return _err(500, "db_error", f"{type(exc).__name__}: {exc}")
        logger.info(
            "landing_signup impression ok cookie=%s page=%s utm_campaign=%s",
            cookie_id[:12], page_path, utm_campaign,
        )
        return jsonify({"status": "ok", "mode": "impression"}), 201

    # ---- Branch B: full form submission (legacy path) ------------------- #
    if not _is_valid_email(email):
        return _err(400, "bad_email", "missing or malformed email")

    e_hash = _email_hash(email)

    try:
        with db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO leads (
                    email, email_hash, first_seen_at, last_seen_at,
                    first_landing_page, first_utm_campaign,
                    first_utm_content, first_utm_term
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    email_hash   = COALESCE(leads.email_hash, excluded.email_hash)
                """,
                (
                    email, e_hash, now_iso, now_iso,
                    page_path, utm_campaign, utm_content, utm_term,
                ),
            )
            # rowcount==1 means INSERT; on UPSERT-update sqlite3 still returns 1.
            # We disambiguate via SELECT after the fact.
            row = conn.execute(
                "SELECT id, first_seen_at FROM leads WHERE email = ?", (email,)
            ).fetchone()
            lead_id = int(row["id"])
            is_new = (row["first_seen_at"] == now_iso)
            del cur  # noqa: F841

            user_id = cookie_id or e_hash
            conn.execute(
                """
                INSERT INTO user_journey (
                    user_id, touchpoint_seq, action,
                    utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                    referrer, page_path, timestamp, raw_data
                )
                VALUES (?, NULL, 'signup', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                    referrer, page_path, now_iso,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

            # Stitch cookie ↔ email_hash so attribution_engine can retro-join
            # this lead's anonymous-impression journey rows (which are keyed
            # by cookie_id, action='impression'). Migration 007 created the
            # table; we ON CONFLICT UPDATE so re-submissions just bump
            # last_seen without duplicating rows.
            if cookie_id:
                now_ts = int(datetime.now(timezone.utc).timestamp())
                conn.execute(
                    """
                    INSERT INTO cookie_email_map (
                        cookie_id, email_hash, first_seen, last_seen
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cookie_id, email_hash) DO UPDATE SET
                        last_seen = excluded.last_seen
                    """,
                    (cookie_id, e_hash, now_ts, now_ts),
                )
    except Exception as exc:
        logger.exception("landing_signup: db failure email_hash=%s", e_hash)
        return _err(500, "db_error", f"{type(exc).__name__}: {exc}")

    logger.info(
        "landing_signup ok lead_id=%s is_new=%s utm_campaign=%s",
        lead_id, is_new, utm_campaign,
    )

    # ---- Lark notification · only fire on NEW lead (avoid spam on resubmit) -- #
    # form_data carries V1 free-diagnostic fields (telegram_handle, project_url)
    # so BD can act on the lead without opening the CRM. Failure here must NOT
    # break the response — lark_alert() is already side-effect-only by contract.
    if is_new:
        try:
            form_data = payload.get("form_data") or {}
            telegram_handle = form_data.get("telegram_handle") or "(not provided)"
            project_url = form_data.get("project_url") or "(not provided)"

            lark_alert(
                "P1",
                f"[New Lead] {email} from {project_url}",
                {
                    "lead_id": lead_id,
                    "email": email,
                    "telegram": telegram_handle,
                    "project_url": project_url,
                    "landing_page": page_path,
                    "utm_source": utm_source or "(direct)",
                    "utm_medium": utm_medium or "(none)",
                    "utm_campaign": utm_campaign or "(none)",
                    "utm_content": utm_content or "(none)",
                    "utm_term": utm_term or "(none)",
                    "referrer": referrer or "(none)",
                },
            )
        except Exception:
            # Never let alerting break the user-facing response.
            logger.exception("landing_signup: lark_alert failed (non-fatal)")

    return jsonify({"status": "ok", "lead_id": lead_id, "is_new": is_new}), (201 if is_new else 200)


# --------------------------------------------------------------------------- #
# /api/listmonk-webhook
# --------------------------------------------------------------------------- #


@app.post("/api/listmonk-webhook")
def listmonk_webhook() -> tuple[Response, int]:
    raw_body = request.get_data() or b""
    header_sig = request.headers.get("X-Listmonk-Signature") or request.headers.get("X-Signature")
    if not _verify_hmac(LISTMONK_HMAC_SECRET_ENV, raw_body, header_sig):
        logger.warning("listmonk_webhook: HMAC mismatch from %s", request.remote_addr)
        return _err(401, "bad_signature", "X-Listmonk-Signature mismatch")

    payload = request.get_json(silent=True) or {}
    event = (payload.get("event") or "").strip()
    email = (payload.get("subscriber_email") or "").strip()
    campaign_id = payload.get("campaign_id")
    ts = payload.get("timestamp") or _utc_now_iso()
    url = payload.get("url") or ""

    if not event:
        return _err(400, "missing_event", "event field required")

    user_id = _email_hash(email) if email else ""

    try:
        if event == "campaign.open":
            db.user_journey.insert(
                user_id=user_id,
                action="open",
                utm_source="listmonk",
                utm_medium="edm",
                utm_campaign=str(campaign_id) if campaign_id is not None else None,
                page_path=url,
                timestamp=ts,
                raw_data=json.dumps(payload, ensure_ascii=False),
            )
        elif event == "campaign.click":
            parsed = parse_utm(url) if url else None
            page_path = urlparse(url).path if url else ""
            db.user_journey.insert(
                user_id=user_id,
                action="click",
                utm_source=parsed.source if parsed else "listmonk",
                utm_medium=parsed.medium if parsed else "edm",
                utm_campaign=parsed.campaign if parsed else (str(campaign_id) if campaign_id is not None else None),
                utm_content=parsed.content if parsed else None,
                utm_term=parsed.term if parsed else None,
                page_path=page_path or url,
                timestamp=ts,
                raw_data=json.dumps(payload, ensure_ascii=False),
            )
        elif event == "campaign.bounce":
            if email:
                db.execute(
                    "UPDATE leads SET bounced = 1 WHERE email = ?",
                    (email,),
                )
            else:
                logger.warning("listmonk_webhook bounce missing email")
        else:
            logger.warning("listmonk_webhook unknown event=%s", event)
            return _err(400, "unknown_event", f"unsupported event: {event}")
    except Exception as exc:
        logger.exception("listmonk_webhook: db failure event=%s", event)
        return _err(500, "db_error", f"{type(exc).__name__}: {exc}")

    logger.info("listmonk_webhook ok event=%s email_present=%s", event, bool(email))
    return jsonify({"status": "ok", "event": event}), 200


# --------------------------------------------------------------------------- #
# /api/ses-bounce  (AWS SNS envelope)
# --------------------------------------------------------------------------- #


def _confirm_sns_subscription(subscribe_url: str) -> None:
    """SNS handshake: HTTP GET the SubscribeURL to confirm the subscription."""
    if not subscribe_url:
        return
    try:
        resp = requests.get(subscribe_url, timeout=10)
        logger.info("sns subscription confirm status=%s url=%s", resp.status_code, subscribe_url)
    except Exception:
        logger.exception("sns subscription confirm failed url=%s", subscribe_url)


@app.post("/api/ses-bounce")
def ses_bounce() -> tuple[Response, int]:
    # SNS sends application/json with a non-JSON Content-Type sometimes
    # (text/plain). force=True handles both.
    payload = request.get_json(silent=True, force=True) or {}
    sns_type = payload.get("Type") or payload.get("notificationType")

    # 1) Subscription handshake
    if sns_type == "SubscriptionConfirmation":
        _confirm_sns_subscription(payload.get("SubscribeURL", ""))
        return jsonify({"status": "ok", "action": "subscription_confirmed"}), 200

    # 2) Notification — message body may be a JSON-encoded string in payload['Message']
    inner: dict[str, Any] = payload
    if sns_type == "Notification":
        msg = payload.get("Message", "")
        try:
            inner = json.loads(msg) if isinstance(msg, str) else (msg or {})
        except json.JSONDecodeError:
            logger.exception("ses_bounce: failed to parse SNS Message JSON")
            return _err(400, "bad_message", "SNS Message is not valid JSON")

    notif_type = inner.get("notificationType") or inner.get("eventType") or ""

    try:
        if notif_type == "Bounce":
            bounce = inner.get("bounce") or {}
            if bounce.get("bounceType") == "Permanent":
                recipients = bounce.get("bouncedRecipients") or []
                affected = 0
                for r in recipients:
                    email = (r.get("emailAddress") or "").strip()
                    if not email:
                        continue
                    db.execute(
                        "UPDATE leads SET bounced = 1 WHERE email = ?",
                        (email,),
                    )
                    affected += 1
                logger.info("ses_bounce permanent affected=%d", affected)
            else:
                logger.info("ses_bounce non-permanent type=%s ignored", bounce.get("bounceType"))
        elif notif_type == "Complaint":
            complaint = inner.get("complaint") or {}
            recipients = complaint.get("complainedRecipients") or []
            emails = [r.get("emailAddress", "") for r in recipients if r.get("emailAddress")]
            logger.error("ses_bounce COMPLAINT P0 emails=%s", emails)
            db.publish_failures.insert(
                severity="P0",
                source="ses",
                failure_type="complaint",
                failure_detail=json.dumps({"emails": emails, "raw": inner}, ensure_ascii=False),
            )
        else:
            logger.warning("ses_bounce unknown notificationType=%s", notif_type)
            return _err(400, "unknown_notification", f"unsupported notificationType: {notif_type}")
    except Exception as exc:
        logger.exception("ses_bounce: db failure type=%s", notif_type)
        return _err(500, "db_error", f"{type(exc).__name__}: {exc}")

    return jsonify({"status": "ok", "notificationType": notif_type}), 200


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #


@app.get("/health")
def health() -> tuple[Response, int]:
    try:
        row = db.fetchone("SELECT 1 AS ok")
        if row and row["ok"] == 1:
            return jsonify({"status": "ok", "service": "taskon-ingestion"}), 200
        return jsonify({"status": "degraded", "error": "unexpected response"}), 503
    except Exception as exc:
        logger.exception("health: db unreachable")
        return jsonify({"status": "degraded", "error": f"{type(exc).__name__}: {exc}"}), 503


# --------------------------------------------------------------------------- #
# /metrics  (Prometheus exposition)
# --------------------------------------------------------------------------- #


_PROM_TABLE_GAUGES: tuple[tuple[str, str], ...] = (
    ("taskon_leads_total", "leads"),
    ("taskon_pieces_total", "pieces"),
    ("taskon_publishings_total", "publishings"),
    ("taskon_user_journey_total", "user_journey"),
    ("taskon_metrics_daily_rows", "metrics_daily"),
    ("taskon_newsletter_campaigns_total", "newsletter_campaigns"),
    ("taskon_publish_failures_total", "publish_failures"),
)


@app.get("/metrics")
def metrics() -> Response:
    lines: list[str] = []
    for metric_name, table in _PROM_TABLE_GAUGES:
        try:
            row = db.fetchone(f"SELECT COUNT(*) AS c FROM {table}")
            count = int(row["c"]) if row else 0
        except Exception:
            logger.exception("metrics: count failed for %s", table)
            count = -1
        lines.append(f"# TYPE {metric_name} gauge")
        lines.append(f"{metric_name} {count}")

    # Heartbeat freshness per job: seconds since last_run_at
    try:
        rows = db.fetchall(
            """
            SELECT job_name,
                   CAST((julianday('now') - julianday(MAX(last_run_at))) * 86400 AS INTEGER) AS age_s
            FROM heartbeat
            GROUP BY job_name
            """
        )
        lines.append("# TYPE taskon_heartbeat_last_seconds gauge")
        for r in rows:
            job = (r["job_name"] or "unknown").replace('"', '\\"')
            age = r["age_s"] if r["age_s"] is not None else -1
            lines.append(f'taskon_heartbeat_last_seconds{{job="{job}"}} {age}')
    except Exception:
        logger.exception("metrics: heartbeat aggregation failed")

    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")


# --------------------------------------------------------------------------- #
# Error handlers (so even bad routes return JSON, not HTML)
# --------------------------------------------------------------------------- #


@app.errorhandler(404)
def _not_found(_e: Any) -> tuple[Response, int]:
    return _err(404, "not_found", "no such route")


@app.errorhandler(405)
def _method_not_allowed(_e: Any) -> tuple[Response, int]:
    return _err(405, "method_not_allowed", "method not allowed for this route")


@app.errorhandler(413)
def _too_large(_e: Any) -> tuple[Response, int]:
    return _err(413, "payload_too_large", "request body exceeds 1 MiB cap")


# --------------------------------------------------------------------------- #
# Dev entry point — production uses gunicorn via ingestion.wsgi:app
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    port = int(os.environ.get("INGESTION_PORT", "5051"))
    host = os.environ.get("INGESTION_BIND", "127.0.0.1")
    logger.info("starting ingestion dev server on %s:%d", host, port)
    app.run(host=host, port=port, debug=False)
