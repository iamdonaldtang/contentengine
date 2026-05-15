"""Tests for ``ingestion.app`` — landing-signup + webhook handlers.

Exercises the public HTTP contract via Flask's ``test_client``:

* /api/landing-signup happy path (201, lead + journey rows)
* /api/landing-signup missing email (400)
* /api/landing-signup duplicate (200, is_new=false)
* /api/landing-signup HMAC mismatch when secret set (401)
* /api/listmonk-webhook campaign.click insert
* /api/ses-bounce permanent → leads.bounced=1
* /health → 200
* /metrics → prom-style text
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
from typing import Any, Iterator

import pytest


# --------------------------------------------------------------------------- #
# client fixture — reloads ingestion.app AFTER tmp_db has set SQLITE_PATH so
# the Flask app binds to the test database, not the developer's local one.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_db: Any) -> Iterator[Any]:
    # Drop any cached import so `from lib.db import db` inside the module
    # picks up the freshly-rebuilt singleton from the tmp_db fixture.
    for mod_name in ("ingestion.app", "ingestion.wsgi", "ingestion"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    app_module = importlib.import_module("ingestion.app")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# --------------------------------------------------------------------------- #
# /api/landing-signup
# --------------------------------------------------------------------------- #


def test_landing_signup_happy_path(client: Any, tmp_db: Any) -> None:
    payload = {
        "email": "founder@somedex.xyz",
        "page_path": "/benchmark-report",
        "url": (
            "https://taskon.xyz/benchmark-report?"
            "utm_source=twitter&utm_medium=thread&utm_campaign=2026w19_thread01"
            "&utm_content=donald_en&utm_term=47pct_bot"
        ),
        "referrer": "https://twitter.com/i/web/status/123",
        "cookie_id": "anon_cookie_abc123",
    }
    resp = client.post("/api/landing-signup", json=payload)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["is_new"] is True
    lead_id = body["lead_id"]
    assert isinstance(lead_id, int) and lead_id > 0

    lead = tmp_db.fetchone("SELECT * FROM leads WHERE id = ?", (lead_id,))
    assert lead is not None
    assert lead["email"] == "founder@somedex.xyz"
    assert lead["first_utm_campaign"] == "2026w19_thread01"
    assert lead["first_landing_page"] == "/benchmark-report"

    journey_rows = tmp_db.fetchall(
        "SELECT * FROM user_journey WHERE action = 'signup'"
    )
    assert len(journey_rows) == 1
    j = journey_rows[0]
    assert j["user_id"] == "anon_cookie_abc123"
    assert j["utm_source"] == "twitter"
    assert j["utm_campaign"] == "2026w19_thread01"
    assert j["touchpoint_seq"] is None


def test_landing_signup_missing_email(client: Any) -> None:
    resp = client.post("/api/landing-signup", json={"page_path": "/x"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["status"] == "error"
    assert body["code"] == "bad_email"


def test_landing_signup_malformed_email(client: Any) -> None:
    resp = client.post("/api/landing-signup", json={"email": "not-an-email"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "bad_email"


def test_landing_signup_duplicate_upsert(client: Any, tmp_db: Any) -> None:
    payload = {
        "email": "alice@example.com",
        "page_path": "/p",
        "url": "https://taskon.xyz/p",
    }
    r1 = client.post("/api/landing-signup", json=payload)
    assert r1.status_code == 201
    assert r1.get_json()["is_new"] is True

    r2 = client.post("/api/landing-signup", json=payload)
    assert r2.status_code == 200, r2.get_data(as_text=True)
    body = r2.get_json()
    assert body["is_new"] is False
    assert body["lead_id"] == r1.get_json()["lead_id"]

    # Exactly one lead row, two journey rows.
    leads = tmp_db.fetchall("SELECT * FROM leads")
    assert len(leads) == 1
    journeys = tmp_db.fetchall("SELECT * FROM user_journey WHERE action='signup'")
    assert len(journeys) == 2


# --------------------------------------------------------------------------- #
# /api/landing-signup · impression_only branch (T1)
# --------------------------------------------------------------------------- #


def test_landing_impression_only_happy_path(client: Any, tmp_db: Any) -> None:
    """impression_only=True with cookie_id → 201 + user_journey row, no leads.

    The URL carries a full 5-segment UTM (TaskOn discipline) so parse_utm
    resolves all fields. Missing-segment cases are covered by lib.utm tests.
    """
    payload = {
        "impression_only": True,
        "page_path": "/benchmark-report",
        "url": (
            "https://taskon.xyz/benchmark-report?"
            "utm_source=twitter&utm_medium=thread&utm_campaign=2026w19_thread01"
            "&utm_content=donald_en&utm_term=47pct_bot"
        ),
        "referrer": "https://t.co/abc",
        "cookie_id": "anon_abc123",
    }
    resp = client.post("/api/landing-signup", json=payload)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["mode"] == "impression"

    # No leads row created.
    leads = tmp_db.fetchall("SELECT * FROM leads")
    assert len(leads) == 0

    # Exactly one user_journey row with action='impression' and cookie as user_id.
    journeys = tmp_db.fetchall("SELECT * FROM user_journey WHERE action='impression'")
    assert len(journeys) == 1
    j = journeys[0]
    assert j["user_id"] == "anon_abc123"
    assert j["utm_source"] == "twitter"
    assert j["utm_campaign"] == "2026w19_thread01"
    assert j["page_path"] == "/benchmark-report"


def test_landing_impression_only_ignores_email(client: Any, tmp_db: Any) -> None:
    """impression_only=True wins even if email is present — never writes leads."""
    payload = {
        "impression_only": True,
        "email": "should-be-ignored@example.com",
        "page_path": "/free-diagnostic",
        "url": "https://taskon.xyz/free-diagnostic?utm_source=linkedin&utm_medium=post",
        "cookie_id": "cookie_xyz",
    }
    resp = client.post("/api/landing-signup", json=payload)
    assert resp.status_code == 201
    assert resp.get_json()["mode"] == "impression"

    # Critical invariant: impression beacons NEVER create lead rows even when
    # email is accidentally included by a frontend bug.
    leads = tmp_db.fetchall("SELECT * FROM leads")
    assert len(leads) == 0

    journeys = tmp_db.fetchall("SELECT * FROM user_journey WHERE action='impression'")
    assert len(journeys) == 1
    assert journeys[0]["user_id"] == "cookie_xyz"


def test_landing_impression_only_missing_cookie_rejects(client: Any, tmp_db: Any) -> None:
    """impression_only without cookie_id → 400 (cannot stitch later)."""
    payload = {
        "impression_only": True,
        "page_path": "/growth-playbook",
        "url": "https://taskon.xyz/growth-playbook",
    }
    resp = client.post("/api/landing-signup", json=payload)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "bad_cookie_id"

    # No rows written on validation failure.
    assert len(tmp_db.fetchall("SELECT * FROM leads")) == 0
    assert len(tmp_db.fetchall("SELECT * FROM user_journey")) == 0


def test_landing_signup_bad_hmac(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGESTION_HMAC_SECRET", "super_secret_value")
    body_bytes = json.dumps({"email": "a@b.co"}).encode("utf-8")
    resp = client.post(
        "/api/landing-signup",
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Signature": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "bad_signature"


def test_landing_signup_good_hmac(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "super_secret_value"
    monkeypatch.setenv("INGESTION_HMAC_SECRET", secret)
    body_bytes = json.dumps(
        {"email": "ok@example.com", "page_path": "/", "url": "https://taskon.xyz/"}
    ).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    resp = client.post(
        "/api/landing-signup",
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Signature": f"sha256={sig}",
        },
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# /api/listmonk-webhook
# --------------------------------------------------------------------------- #


def test_listmonk_campaign_click(client: Any, tmp_db: Any) -> None:
    payload = {
        "event": "campaign.click",
        "subscriber_email": "lead@example.com",
        "campaign_id": 5,
        "timestamp": "2026-05-13T10:00:00Z",
        "url": (
            "https://taskon.xyz/landing?utm_source=listmonk&utm_medium=edm"
            "&utm_campaign=2026w20_newsletter&utm_content=donald_en&utm_term=cta"
        ),
    }
    resp = client.post("/api/listmonk-webhook", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    rows = tmp_db.fetchall("SELECT * FROM user_journey WHERE action='click'")
    assert len(rows) == 1
    r = rows[0]
    assert r["utm_campaign"] == "2026w20_newsletter"
    assert r["utm_source"] == "listmonk"


def test_listmonk_campaign_open(client: Any, tmp_db: Any) -> None:
    payload = {
        "event": "campaign.open",
        "subscriber_email": "lead@example.com",
        "campaign_id": 7,
        "timestamp": "2026-05-13T10:00:00Z",
        "url": "https://newsletter.taskon.xyz/c/7",
    }
    resp = client.post("/api/listmonk-webhook", json=payload)
    assert resp.status_code == 200

    rows = tmp_db.fetchall("SELECT * FROM user_journey WHERE action='open'")
    assert len(rows) == 1
    assert rows[0]["utm_campaign"] == "7"


def test_listmonk_campaign_bounce(client: Any, tmp_db: Any) -> None:
    # Seed a lead first
    tmp_db.execute(
        "INSERT INTO leads (email, email_hash) VALUES (?, ?)",
        ("bounce@example.com", "h"),
    )
    payload = {
        "event": "campaign.bounce",
        "subscriber_email": "bounce@example.com",
        "campaign_id": 9,
        "timestamp": "2026-05-13T10:00:00Z",
    }
    resp = client.post("/api/listmonk-webhook", json=payload)
    assert resp.status_code == 200
    row = tmp_db.fetchone(
        "SELECT bounced FROM leads WHERE email = ?", ("bounce@example.com",)
    )
    assert row["bounced"] == 1


# --------------------------------------------------------------------------- #
# /api/ses-bounce
# --------------------------------------------------------------------------- #


def test_ses_bounce_permanent(client: Any, tmp_db: Any) -> None:
    # Seed a lead so the bounce update is observable.
    tmp_db.execute(
        "INSERT INTO leads (email, email_hash) VALUES (?, ?)",
        ("invalid@example.com", "h"),
    )

    inner = {
        "notificationType": "Bounce",
        "bounce": {
            "bounceType": "Permanent",
            "bouncedRecipients": [{"emailAddress": "invalid@example.com"}],
        },
    }
    sns_envelope = {
        "Type": "Notification",
        "Message": json.dumps(inner),
    }
    resp = client.post("/api/ses-bounce", json=sns_envelope)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    row = tmp_db.fetchone(
        "SELECT bounced FROM leads WHERE email = ?", ("invalid@example.com",)
    )
    assert row["bounced"] == 1


def test_ses_complaint_logs_failure(client: Any, tmp_db: Any) -> None:
    inner = {
        "notificationType": "Complaint",
        "complaint": {
            "complainedRecipients": [{"emailAddress": "angry@example.com"}],
        },
    }
    resp = client.post(
        "/api/ses-bounce", json={"Type": "Notification", "Message": json.dumps(inner)}
    )
    assert resp.status_code == 200
    rows = tmp_db.fetchall(
        "SELECT * FROM publish_failures WHERE source='ses' AND failure_type='complaint'"
    )
    assert len(rows) == 1
    assert rows[0]["severity"] == "P0"


# --------------------------------------------------------------------------- #
# /health + /metrics
# --------------------------------------------------------------------------- #


def test_health_ok(client: Any) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"


def test_metrics_prom_format(client: Any, tmp_db: Any) -> None:
    # Seed minimal rows so the counts are non-zero.
    tmp_db.execute(
        "INSERT INTO leads (email, email_hash) VALUES (?, ?)", ("m@x.co", "h")
    )
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("Content-Type", "")
    text = resp.get_data(as_text=True)
    assert "taskon_leads_total" in text
    assert "taskon_pieces_total" in text
    assert "taskon_metrics_daily_rows" in text
    assert "taskon_heartbeat_last_seconds" in text
    # Each gauge has a TYPE line + a value line.
    assert "# TYPE taskon_leads_total gauge" in text
