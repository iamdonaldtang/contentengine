"""Tests for ``ingestion.mpt_callback`` — MPT webhook receiver (S3 of A-design).

Covers the security envelope (HMAC + timestamp anti-replay), state machine
transitions (idempotent, atomic), and the off-the-happy-path branches
(unknown task_id, malformed body, missing fields, race-lost to reconciler).

Download spawning is mocked — we only assert that ``_spawn_download`` was
called with the right arguments. The actual mp4 fetch is tested in S7 (E2E).
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
import time
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest


_SECRET = "test-secret-32-bytes-xxxxxxxxxxxxxxxxx"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_db: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Test client with MPT_CALLBACK_SECRET configured and download mocked."""
    monkeypatch.setenv("MPT_CALLBACK_SECRET", _SECRET)
    # Reload so Blueprint picks up the test DB.
    for mod_name in (
        "ingestion.app",
        "ingestion.wsgi",
        "ingestion.mpt_callback",
        "ingestion",
    ):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    cb_module = importlib.import_module("ingestion.mpt_callback")
    # Mock the download spawner so unit tests don't try to hit MPT.
    cb_module._spawn_download = MagicMock(name="_spawn_download")
    app_module = importlib.import_module("ingestion.app")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c._cb_module = cb_module  # type: ignore[attr-defined]
        yield c


@pytest.fixture()
def client_disabled(tmp_db: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Test client with NO MPT_CALLBACK_SECRET (endpoint disabled)."""
    monkeypatch.delenv("MPT_CALLBACK_SECRET", raising=False)
    for mod_name in ("ingestion.app", "ingestion.wsgi", "ingestion.mpt_callback", "ingestion"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    app_module = importlib.import_module("ingestion.app")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sign(body: bytes, secret: str = _SECRET, ts: int | None = None) -> dict[str, str]:
    """Build valid HMAC headers for the given body."""
    if ts is None:
        ts = int(time.time())
    sig = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-MPT-Signature": f"sha256={sig}",
        "X-MPT-Timestamp": str(ts),
        "Content-Type": "application/json",
    }


def _setup_submitted_task(tmp_db: Any, *, piece_id: str = "test-cb-piece", task_id: str = "mpt-task-cb") -> tuple[int, str]:
    """Seed pieces row + mpt_tasks row in 'submitted' state. Returns (row_id, task_id)."""
    tmp_db.pieces.create(piece_id, f"piece_id: {piece_id}\n", actor="test")
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, task_id)
    return row_id, task_id


def _post_signed(
    client: Any,
    payload: dict[str, Any],
    *,
    ts: int | None = None,
    secret: str = _SECRET,
) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _sign(body, secret=secret, ts=ts)
    return client.post("/api/mpt-callback", data=body, headers=headers)


# --------------------------------------------------------------------------- #
# Disabled state
# --------------------------------------------------------------------------- #


def test_callback_disabled_when_no_secret(client_disabled: Any) -> None:
    """Endpoint must return 503 + safe error when MPT_CALLBACK_SECRET is unset."""
    resp = client_disabled.post(
        "/api/mpt-callback",
        data=b"{}",
        headers={
            "X-MPT-Signature": "sha256=deadbeef",
            "X-MPT-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["code"] == "callback_disabled"


# --------------------------------------------------------------------------- #
# Signature / timestamp
# --------------------------------------------------------------------------- #


def test_callback_rejects_missing_signature_header(client: Any) -> None:
    resp = client.post(
        "/api/mpt-callback",
        data=b"{}",
        headers={
            "X-MPT-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_callback_rejects_bad_signature_prefix(client: Any) -> None:
    resp = client.post(
        "/api/mpt-callback",
        data=b"{}",
        headers={
            "X-MPT-Signature": "md5=deadbeef",  # not sha256
            "X-MPT-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "bad_sig_prefix" in resp.get_json()["message"]


def test_callback_rejects_invalid_signature(client: Any, tmp_db: Any) -> None:
    _setup_submitted_task(tmp_db)
    body = json.dumps({"task_id": "mpt-task-cb", "status": "completed"}).encode("utf-8")
    ts = int(time.time())
    resp = client.post(
        "/api/mpt-callback",
        data=body,
        headers={
            "X-MPT-Signature": "sha256=" + ("0" * 64),
            "X-MPT-Timestamp": str(ts),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "sig_mismatch" in resp.get_json()["message"]


def test_callback_rejects_old_timestamp(client: Any) -> None:
    body = json.dumps({"task_id": "x", "status": "completed"}).encode("utf-8")
    old_ts = int(time.time()) - 600  # 10 min ago
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body, ts=old_ts))
    assert resp.status_code == 401
    assert "ts_skew" in resp.get_json()["message"]


def test_callback_rejects_future_timestamp(client: Any) -> None:
    body = json.dumps({"task_id": "x", "status": "completed"}).encode("utf-8")
    future_ts = int(time.time()) + 600
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body, ts=future_ts))
    assert resp.status_code == 401


def test_callback_rejects_non_integer_timestamp(client: Any) -> None:
    body = b"{}"
    valid_sig = hmac.new(_SECRET.encode(), b"abc.{}", hashlib.sha256).hexdigest()
    resp = client.post(
        "/api/mpt-callback",
        data=body,
        headers={
            "X-MPT-Signature": f"sha256={valid_sig}",
            "X-MPT-Timestamp": "not-a-number",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "bad_ts_format" in resp.get_json()["message"]


# --------------------------------------------------------------------------- #
# Body validation (post-auth)
# --------------------------------------------------------------------------- #


def test_callback_bad_json_body_returns_400(client: Any) -> None:
    body = b"this is not json"
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body))
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "bad_body"


def test_callback_array_body_rejected(client: Any) -> None:
    body = b"[1,2,3]"
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body))
    assert resp.status_code == 400


def test_callback_missing_task_id_returns_400(client: Any) -> None:
    body = json.dumps({"status": "completed"}).encode("utf-8")
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body))
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "missing_task_id"


def test_callback_bad_status_returns_400(client: Any) -> None:
    body = json.dumps({"task_id": "x", "status": "weird_state"}).encode("utf-8")
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body))
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "bad_status"


def test_callback_completed_without_mp4_url_returns_400(client: Any, tmp_db: Any) -> None:
    _setup_submitted_task(tmp_db)
    body = json.dumps({"task_id": "mpt-task-cb", "status": "completed"}).encode("utf-8")
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body))
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "missing_mp4_url"


# --------------------------------------------------------------------------- #
# Unknown task_id graceful
# --------------------------------------------------------------------------- #


def test_callback_unknown_task_id_returns_200_ignored(client: Any, tmp_db: Any) -> None:
    """Replay from a different engine / stale MPT must not 4xx — MPT would DLQ it.
    We return 200 ignored + audit log."""
    body = json.dumps({
        "task_id": "totally-unknown-uuid",
        "status": "completed",
        "mp4_url": "http://mpt:8090/x.mp4",
    }).encode("utf-8")
    resp = client.post("/api/mpt-callback", data=body, headers=_sign(body))
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ignored"


# --------------------------------------------------------------------------- #
# Happy path — completed
# --------------------------------------------------------------------------- #


def test_callback_completed_happy_path(client: Any, tmp_db: Any) -> None:
    _, task_id = _setup_submitted_task(tmp_db)
    body = json.dumps({
        "task_id": task_id,
        "status": "completed",
        "mp4_url": "http://mpt:8090/tasks/abc/final-1.mp4",
        "state": 1,
        "progress": 100,
    }).encode("utf-8")
    resp = _post_signed(client, json.loads(body))
    assert resp.status_code == 202, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j["status"] == "accepted"
    assert j["transition"] == "submitted->completed"

    # DB transitioned
    row = tmp_db.mpt_tasks.get_by_task_id(task_id)
    assert row["status"] == "completed"
    assert row["mp4_url"] == "http://mpt:8090/tasks/abc/final-1.mp4"
    assert row["terminal_source"] == "callback"
    assert row["callback_received_at"] is not None

    # Download spawned with correct args
    cb_module = client._cb_module
    cb_module._spawn_download.assert_called_once_with(task_id, "test-cb-piece")


def test_callback_duplicate_completed_is_idempotent(client: Any, tmp_db: Any) -> None:
    _, task_id = _setup_submitted_task(tmp_db)
    payload = {
        "task_id": task_id,
        "status": "completed",
        "mp4_url": "http://mpt:8090/x.mp4",
    }
    first = _post_signed(client, payload)
    assert first.status_code == 202

    second = _post_signed(client, payload)
    assert second.status_code == 200
    assert second.get_json()["status"] == "already_processed"

    # Download must have been spawned exactly once.
    assert client._cb_module._spawn_download.call_count == 1


# --------------------------------------------------------------------------- #
# Failed status path
# --------------------------------------------------------------------------- #


def test_callback_failed_status_transitions_and_no_download(client: Any, tmp_db: Any) -> None:
    _, task_id = _setup_submitted_task(tmp_db)
    body = {
        "task_id": task_id,
        "status": "failed",
        "error": "Whisper subtitle module crashed",
        "state": -1,
    }
    resp = _post_signed(client, body)
    assert resp.status_code == 202
    assert resp.get_json()["transition"] == "submitted->failed"

    row = tmp_db.mpt_tasks.get_by_task_id(task_id)
    assert row["status"] == "failed"
    assert "Whisper subtitle module crashed" in (row["error"] or "")
    assert row["terminal_source"] == "callback"

    # No download for failed callback
    client._cb_module._spawn_download.assert_not_called()


def test_callback_cancelled_treated_as_failed(client: Any, tmp_db: Any) -> None:
    _, task_id = _setup_submitted_task(tmp_db)
    resp = _post_signed(client, {"task_id": task_id, "status": "cancelled"})
    assert resp.status_code == 202
    row = tmp_db.mpt_tasks.get_by_task_id(task_id)
    assert row["status"] == "failed"


# --------------------------------------------------------------------------- #
# Race-lost (reconciler already processed)
# --------------------------------------------------------------------------- #


def test_callback_race_lost_after_reconciler_wins(client: Any, tmp_db: Any) -> None:
    """Reconciler simulated the callback before the real one arrived."""
    _, task_id = _setup_submitted_task(tmp_db)
    # Simulate reconciler winning
    tmp_db.mpt_tasks.mark_completed(task_id, "url-from-reconciler", source="reconciler")

    body = {
        "task_id": task_id,
        "status": "completed",
        "mp4_url": "url-from-callback",
    }
    resp = _post_signed(client, body)
    # Pre-loop check sees terminal status → already_processed (this is OK; the
    # transition function would also return False, but we short-circuit before)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "already_processed"

    # Reconciler's writes must still be intact — no overwrite.
    row = tmp_db.mpt_tasks.get_by_task_id(task_id)
    assert row["mp4_url"] == "url-from-reconciler"
    assert row["terminal_source"] == "reconciler"

    # No download spawned (callback short-circuited).
    client._cb_module._spawn_download.assert_not_called()
