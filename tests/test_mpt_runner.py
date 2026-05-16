"""Tests for ``jobs.mpt_runner`` after the A-design refactor (S6).

The runner now ONLY submits — no poll, no download, no publishings rows.
Coverage:

  * Happy path: narration extract + mpt_tasks row created + submitted.
  * Idempotency: in-flight row → already_in_flight (no resubmit).
  * --force: bypass the idempotency guard.
  * Missing / empty script / no narration → skipped + P1 alert (heartbeat warning).
  * Submit POST raises MPTError → row marked failed, summary status='failed'.
  * Submit POST raises unexpected exception → also captured as failed (no leak).
  * MPT_CALLBACK_SECRET missing → P0 alert, status='failed', no MPT POST attempted.
  * Dry-run: validates env but never inserts mpt_tasks rows or calls MPT.
  * Heartbeat row recorded per branch.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


def _reload_runner() -> Any:
    for mod_name in ("jobs.mpt_runner", "sources.mpt"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("jobs.mpt_runner")


def _seed_piece_and_script(tmp_db: Any, tmp_path: Path, piece_id: str, script_text: str = "narration here") -> Path:
    """Create a piece row + the shorts_60s.md file."""
    tmp_db.pieces.create(piece_id, f"piece_id: {piece_id}\n", actor="test")
    drafts = tmp_path / "drafts"
    piece_dir = drafts / piece_id
    piece_dir.mkdir(parents=True, exist_ok=True)
    (piece_dir / "shorts_60s.md").write_text(script_text, encoding="utf-8")
    return piece_dir


# --------------------------------------------------------------------------- #
# Happy path — submit-and-exit
# --------------------------------------------------------------------------- #


def test_runner_happy_path_submits_and_exits(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "test-secret")
    monkeypatch.setenv("MPT_CALLBACK_URL", "http://taskon-engine:5051/api/mpt-callback")

    runner = _reload_runner()
    _seed_piece_and_script(
        tmp_db, tmp_path, "p-happy",
        "## 纯旁白稿\n这是测试旁白文本",
    )

    captured: dict[str, Any] = {}

    def _fake_submit(narration: str, **kwargs: Any) -> str:
        captured["narration"] = narration
        captured["kwargs"] = kwargs
        return "task-happy-abc"

    monkeypatch.setattr(runner.mpt, "submit_video", _fake_submit)

    summary = runner.run("p-happy")
    assert summary["status"] == "submitted"
    assert summary["task_id"] == "task-happy-abc"
    assert summary["narration_chars"] > 0

    # mpt_tasks row created + transitioned to 'submitted'
    row = tmp_db.mpt_tasks.get_by_task_id("task-happy-abc")
    assert row is not None
    assert row["status"] == "submitted"
    assert row["piece_id"] == "p-happy"

    # Callback fields forwarded to MPT
    assert captured["kwargs"]["callback_url"] == "http://taskon-engine:5051/api/mpt-callback"
    assert captured["kwargs"]["callback_secret"] == "test-secret"

    # Narration was extracted (header stripped)
    assert "## 纯旁白稿" not in captured["narration"]
    assert "这是测试旁白文本" in captured["narration"]

    # Heartbeat recorded as 'ok'
    hb = tmp_db.heartbeat.last("mpt_runner")
    assert hb["status"] == "ok"


def test_runner_uses_default_callback_url_when_unset(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    monkeypatch.delenv("MPT_CALLBACK_URL", raising=False)

    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-dft", "any text")

    captured = {}
    monkeypatch.setattr(
        runner.mpt, "submit_video",
        lambda script, **kw: captured.update(kw) or "task-dft",
    )

    runner.run("p-dft")
    assert captured["callback_url"] == runner.DEFAULT_CALLBACK_URL


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_runner_skips_when_in_flight_row_exists(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-flight", "x")

    # Seed an in-flight row.
    row_id = tmp_db.mpt_tasks.create_pending("p-flight")
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-prev")

    submit_mock = MagicMock(name="submit_video")
    monkeypatch.setattr(runner.mpt, "submit_video", submit_mock)

    summary = runner.run("p-flight")
    assert summary["status"] == "already_in_flight"
    assert summary["task_id"] == "task-prev"
    submit_mock.assert_not_called()


def test_runner_force_bypasses_in_flight_guard(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-force", "x")
    row_id = tmp_db.mpt_tasks.create_pending("p-force")
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-prev")

    monkeypatch.setattr(runner.mpt, "submit_video", lambda s, **kw: "task-new")
    summary = runner.run("p-force", force=True)
    assert summary["status"] == "submitted"
    assert summary["task_id"] == "task-new"


# --------------------------------------------------------------------------- #
# Skipped paths — input missing / empty / no narration
# --------------------------------------------------------------------------- #


def test_runner_missing_script_returns_skipped_no_db_write(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    tmp_db.pieces.create("p-noscript", "piece_id: p-noscript\n", actor="test")
    # NO shorts_60s.md created

    summary = runner.run("p-noscript")
    assert summary["status"] == "skipped"
    assert summary["reason"] == "no_script"
    assert tmp_db.mpt_tasks.get_in_flight_for_piece("p-noscript") is None
    hb = tmp_db.heartbeat.last("mpt_runner")
    assert hb["status"] == "warning"


def test_runner_empty_script_returns_skipped(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-empty", "   \n  \n")

    summary = runner.run("p-empty")
    assert summary["status"] == "skipped"
    assert summary["reason"] == "empty_script"


def test_runner_no_narration_after_extraction_returns_skipped(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    # Header exists, but body is only HTML comments → stripped → empty
    _seed_piece_and_script(
        tmp_db, tmp_path, "p-nonarr",
        "## 纯旁白稿\n<!-- only comments here -->",
    )

    summary = runner.run("p-nonarr")
    assert summary["status"] == "skipped"
    assert summary["reason"] == "no_narration"


# --------------------------------------------------------------------------- #
# Submit POST failure
# --------------------------------------------------------------------------- #


def test_runner_submit_mpt_error_marks_failed(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-fail-submit", "x")

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise runner.MPTError("HTTP 500 from MPT")

    monkeypatch.setattr(runner.mpt, "submit_video", _boom)

    summary = runner.run("p-fail-submit")
    assert summary["status"] == "failed"
    assert summary["reason"] == "submit_error"
    assert "HTTP 500 from MPT" in summary["error"]

    row = tmp_db.mpt_tasks.get_by_id(summary["mpt_task_row_id"])
    assert row["status"] == "failed"
    assert "HTTP 500 from MPT" in (row["error"] or "")

    hb = tmp_db.heartbeat.last("mpt_runner")
    assert hb["status"] == "failed"


def test_runner_submit_unexpected_exception_captured(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-fail-bug", "x")

    def _crash(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("unexpected non-MPTError")

    monkeypatch.setattr(runner.mpt, "submit_video", _crash)

    summary = runner.run("p-fail-bug")
    assert summary["status"] == "failed"
    assert summary["reason"] == "crash"
    row = tmp_db.mpt_tasks.get_by_id(summary["mpt_task_row_id"])
    assert row["status"] == "failed"
    assert "RuntimeError" in (row["error"] or "")


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #


def test_runner_missing_callback_secret_fails_before_mpt_post(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.delenv("MPT_CALLBACK_SECRET", raising=False)
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-nosecret", "x")

    submit_mock = MagicMock()
    monkeypatch.setattr(runner.mpt, "submit_video", submit_mock)

    summary = runner.run("p-nosecret")
    assert summary["status"] == "failed"
    assert summary["reason"] == "config_invalid"
    submit_mock.assert_not_called()
    # No mpt_tasks row should be inserted when config is invalid.
    assert tmp_db.mpt_tasks.get_in_flight_for_piece("p-nosecret") is None


def test_runner_invalid_callback_url_scheme_fails(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    monkeypatch.setenv("MPT_CALLBACK_URL", "ftp://not-http.example.com/cb")
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-badurl", "x")

    summary = runner.run("p-badurl")
    assert summary["status"] == "failed"
    assert summary["reason"] == "config_invalid"


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #


def test_runner_dry_run_no_db_write_no_mpt_post(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("MPT_CALLBACK_SECRET", "s")
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-dry", "x")

    submit_mock = MagicMock()
    monkeypatch.setattr(runner.mpt, "submit_video", submit_mock)

    summary = runner.run("p-dry", dry_run=True)
    assert summary["status"] == "dry_run"
    submit_mock.assert_not_called()
    assert tmp_db.mpt_tasks.get_in_flight_for_piece("p-dry") is None


def test_runner_dry_run_still_validates_config(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run must catch config errors so operators can preview safely."""
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.delenv("MPT_CALLBACK_SECRET", raising=False)
    runner = _reload_runner()
    _seed_piece_and_script(tmp_db, tmp_path, "p-dry-bad", "x")

    summary = runner.run("p-dry-bad", dry_run=True)
    assert summary["status"] == "failed"
    assert summary["reason"] == "config_invalid"
