"""Tests for ``sources.mpt.MPTClient.submit_video`` callback support (S2 of
A-design, 2026-05-16).

These tests intentionally avoid the higher-level ``jobs.mpt_runner`` flow so
that the transport contract — exactly what JSON body the engine POSTs to MPT
— can be asserted directly. The MPT-side counterpart (S4 in the design)
relies on these field names being stable.
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest


def _fresh_mpt(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reload sources.mpt with MPT_API_BASE set to a fake value so submit
    doesn't short-circuit on config check."""
    monkeypatch.setenv("MPT_API_BASE", "http://mpt.fake:8090")
    if "sources.mpt" in sys.modules:
        del sys.modules["sources.mpt"]
    return importlib.import_module("sources.mpt")


def _patch_post(monkeypatch: pytest.MonkeyPatch, mpt_module: Any, task_id: str = "task-test-abc") -> list[dict[str, Any]]:
    """Patch the MPTClient._post method to capture the body and return a
    valid {data:{task_id:...}} envelope. Returns the captured-body list."""
    captured: list[dict[str, Any]] = []

    def _fake_post(self: Any, path: str, body: dict[str, Any]) -> Any:
        captured.append({"path": path, "body": body})
        return {"data": {"task_id": task_id}}

    monkeypatch.setattr(mpt_module.MPTClient, "_post", _fake_post, raising=True)
    return captured


# --------------------------------------------------------------------------- #
# Default (no callback) — body must NOT carry callback_* keys
# --------------------------------------------------------------------------- #


def test_submit_video_default_omits_callback_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    mpt = _fresh_mpt(monkeypatch)
    captured = _patch_post(monkeypatch, mpt)

    client = mpt.MPTClient()
    task_id = client.submit_video("测试旁白", voice="zh-CN-YunxiNeural-Male")

    assert task_id == "task-test-abc"
    assert len(captured) == 1
    body = captured[0]["body"]
    assert "callback_url" not in body
    assert "callback_secret" not in body
    # Core fields still set
    assert body["video_script"] == "测试旁白"
    assert body["voice_name"] == "zh-CN-YunxiNeural-Male"
    assert body["video_aspect"] == "9:16"


# --------------------------------------------------------------------------- #
# Callback mode — both fields land in body
# --------------------------------------------------------------------------- #


def test_submit_video_callback_mode_includes_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    mpt = _fresh_mpt(monkeypatch)
    captured = _patch_post(monkeypatch, mpt)

    client = mpt.MPTClient()
    client.submit_video(
        "测试旁白",
        callback_url="http://taskon-ingestion:5051/api/mpt-callback",
        callback_secret="s3cr3t-32-bytes-xxxxxxxxxxxxxxxx",
    )

    body = captured[0]["body"]
    assert body["callback_url"] == "http://taskon-ingestion:5051/api/mpt-callback"
    assert body["callback_secret"] == "s3cr3t-32-bytes-xxxxxxxxxxxxxxxx"


def test_submit_video_callback_mode_with_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """https:// URLs accepted (production deployment behind cloudflared)."""
    mpt = _fresh_mpt(monkeypatch)
    captured = _patch_post(monkeypatch, mpt)
    client = mpt.MPTClient()
    client.submit_video(
        "x",
        callback_url="https://engine.taskon.xyz/api/mpt-callback",
        callback_secret="ok",
    )
    assert captured[0]["body"]["callback_url"].startswith("https://")


# --------------------------------------------------------------------------- #
# XOR validation — both or neither
# --------------------------------------------------------------------------- #


def test_submit_video_callback_url_only_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mpt = _fresh_mpt(monkeypatch)
    _patch_post(monkeypatch, mpt)
    client = mpt.MPTClient()
    with pytest.raises(mpt.MPTError, match="XOR rejected"):
        client.submit_video("x", callback_url="http://engine/api/mpt-callback")


def test_submit_video_callback_secret_only_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mpt = _fresh_mpt(monkeypatch)
    _patch_post(monkeypatch, mpt)
    client = mpt.MPTClient()
    with pytest.raises(mpt.MPTError, match="XOR rejected"):
        client.submit_video("x", callback_secret="orphan-secret")


# --------------------------------------------------------------------------- #
# URL scheme validation
# --------------------------------------------------------------------------- #


def test_submit_video_callback_url_bad_scheme_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mpt = _fresh_mpt(monkeypatch)
    _patch_post(monkeypatch, mpt)
    client = mpt.MPTClient()
    with pytest.raises(mpt.MPTError, match="must start with http"):
        client.submit_video(
            "x",
            callback_url="ftp://wat.example.com/cb",
            callback_secret="s",
        )


def test_submit_video_callback_url_missing_scheme_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mpt = _fresh_mpt(monkeypatch)
    _patch_post(monkeypatch, mpt)
    client = mpt.MPTClient()
    with pytest.raises(mpt.MPTError, match="must start with http"):
        client.submit_video(
            "x",
            callback_url="engine.taskon.xyz/api/mpt-callback",
            callback_secret="s",
        )


# --------------------------------------------------------------------------- #
# Secret must NEVER appear in logs
# --------------------------------------------------------------------------- #


def test_submit_video_callback_secret_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mpt = _fresh_mpt(monkeypatch)
    _patch_post(monkeypatch, mpt)
    client = mpt.MPTClient()

    secret = "VERY-SECRET-VALUE-do-not-leak-32bytes"
    with caplog.at_level(logging.DEBUG, logger="sources.mpt"):
        client.submit_video(
            "x",
            callback_url="http://taskon-ingestion:5051/api/mpt-callback",
            callback_secret=secret,
        )

    # Secret must not appear anywhere in captured log records.
    full_text = caplog.text
    assert secret not in full_text, f"secret leaked into logs:\n{full_text}"

    # But the host part should be logged for ops.
    assert "callback_host=http://taskon-ingestion:5051" in full_text


def test_submit_video_query_string_in_callback_url_not_fully_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If caller accidentally puts a token in the URL query string, log only host."""
    mpt = _fresh_mpt(monkeypatch)
    _patch_post(monkeypatch, mpt)
    client = mpt.MPTClient()

    with caplog.at_level(logging.DEBUG, logger="sources.mpt"):
        client.submit_video(
            "x",
            callback_url="http://engine:5051/api/mpt-callback?secret-token=LEAKY",
            callback_secret="ok",
        )

    assert "LEAKY" not in caplog.text
