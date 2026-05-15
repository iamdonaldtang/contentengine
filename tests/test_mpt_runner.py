"""Tests for ``sources.mpt`` + ``jobs.mpt_runner`` (T4 · B1 §2.6/2.7).

Coverage:
1. **Submit happy-path** — mock MPT API returns task_id; runner downloads
   mp4 and writes publishings rows (yt_shorts + tiktok) with media_path.
2. **Poll timeout → P1 alert** — patch poll_task to raise MPTTimeoutError;
   verify the alert fires and run() re-raises.
3. **Download fallback** — submit + poll succeed but download fails; runner
   persists publishings WITHOUT media_path and emits P2 publish_failures.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------- #
# Reload helpers — same pattern as test_schedule_planner / test_attribution
# --------------------------------------------------------------------------- #


def _reload_runner() -> Any:
    for mod_name in ("jobs.mpt_runner", "sources.mpt"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("jobs.mpt_runner")


def _seed_script(drafts_root: Path, piece_id: str) -> Path:
    piece_dir = drafts_root / piece_id
    piece_dir.mkdir(parents=True, exist_ok=True)
    script = piece_dir / "shorts_60s.md"
    script.write_text(
        "0:00 反共识钩子：47% Quest 预算被 Bot 吃\n0:10 数据展开\n0:50 CTA",
        encoding="utf-8",
    )
    return piece_dir


# --------------------------------------------------------------------------- #
# Test 1 — happy path: submit + poll OK + download OK
# --------------------------------------------------------------------------- #


def test_mpt_runner_happy_path_writes_publishings(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit OK → poll terminal → download OK → 2 publishings rows + media_path stamped."""
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    runner = _reload_runner()
    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    piece_dir = _seed_script(drafts, "mptpiece-001")

    # Seed a piece row so state_events FK is satisfied.
    tmp_db.pieces.create("mptpiece-001", '{"piece_id":"mptpiece-001"}', actor="test")
    tmp_db.pieces.update_state("mptpiece-001", "reviewed", actor="test")

    # Patch MPT client.
    monkeypatch.setattr(runner.mpt, "submit_video", lambda script, **kw: "task-abc-123")
    monkeypatch.setattr(
        runner.mpt, "poll_task",
        lambda task_id, **kw: {"state": 1, "progress": 100, "videos": ["final-1.mp4"]},
    )

    expected_path = piece_dir / "shorts_60s.mp4"

    def _fake_download(task_id: str, dest_path: Path) -> Path:
        dest_path = Path(dest_path)
        dest_path.write_bytes(b"\x00" * 200_000)  # > 100 KB per spec
        return dest_path

    monkeypatch.setattr(runner.mpt, "download_video", _fake_download)

    summary = runner.run("mptpiece-001")

    assert summary["status"] == "ok", summary
    assert summary["task_id"] == "task-abc-123"
    assert summary["media_path"] == str(expected_path)
    assert summary["file_size_bytes"] >= 100_000
    assert expected_path.is_file()

    pubs = tmp_db.fetchall(
        "SELECT platform, media_path FROM publishings WHERE piece_id = ? ORDER BY platform",
        ("mptpiece-001",),
    )
    platforms = {r["platform"] for r in pubs}
    assert platforms == {"tiktok", "yt_shorts"}
    for r in pubs:
        assert r["media_path"] == str(expected_path)


# --------------------------------------------------------------------------- #
# Test 2 — poll timeout → P1 alert + run re-raises MPTTimeoutError
# --------------------------------------------------------------------------- #


def test_mpt_runner_poll_timeout_emits_p1_and_raises(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    runner = _reload_runner()

    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    _seed_script(drafts, "mptpiece-002")
    tmp_db.pieces.create("mptpiece-002", '{"piece_id":"mptpiece-002"}', actor="test")

    from sources.mpt import MPTTimeoutError

    monkeypatch.setattr(runner.mpt, "submit_video", lambda script, **kw: "task-timeout-x")

    def _boom(task_id: str, **kw: Any) -> dict[str, Any]:
        raise MPTTimeoutError(f"task {task_id} did not finish within 5s")

    monkeypatch.setattr(runner.mpt, "poll_task", _boom)

    # Capture alert calls.
    fired: list[tuple[str, str, dict[str, Any]]] = []

    def _capture_alert(severity: str, message: str, details: dict[str, Any] | None = None) -> bool:
        fired.append((severity, message, details or {}))
        return True

    monkeypatch.setattr(runner, "alert", _capture_alert)

    with pytest.raises(MPTTimeoutError):
        runner.run("mptpiece-002")

    p1_alerts = [a for a in fired if a[0] == "P1"]
    assert p1_alerts, f"expected at least one P1 alert; got fired={fired}"
    assert any("TIMEOUT" in a[1] or "timeout" in a[1].lower() for a in p1_alerts)

    # Heartbeat row should mark this as 'failed'.
    hb_rows = tmp_db.fetchall(
        "SELECT status, error_message FROM heartbeat WHERE job_name = ? ORDER BY id DESC LIMIT 1",
        ("mpt_runner",),
    )
    assert hb_rows, "expected a heartbeat row"
    assert hb_rows[0]["status"] == "failed"


# --------------------------------------------------------------------------- #
# Test 3 — download fail → publishings row inserted WITHOUT media_path + P2
# --------------------------------------------------------------------------- #


def test_mpt_runner_download_fallback_persists_without_media(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    runner = _reload_runner()
    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    _seed_script(drafts, "mptpiece-003")
    tmp_db.pieces.create("mptpiece-003", '{"piece_id":"mptpiece-003"}', actor="test")

    from sources.mpt import MPTError

    monkeypatch.setattr(runner.mpt, "submit_video", lambda script, **kw: "task-dl-fail")
    monkeypatch.setattr(
        runner.mpt, "poll_task",
        lambda task_id, **kw: {"state": 1, "progress": 100},
    )

    def _dl_boom(task_id: str, dest_path: Path) -> Path:
        raise MPTError("no working URL for task_id=task-dl-fail; last_err=HTTP 404")

    monkeypatch.setattr(runner.mpt, "download_video", _dl_boom)

    summary = runner.run("mptpiece-003")
    # Status should be 'warning' (not 'failed') — fallback path.
    assert summary["status"] == "warning", summary
    assert summary["media_path"] is None
    assert summary["download_error"]

    # Both publishings rows exist but media_path is NULL.
    pubs = tmp_db.fetchall(
        "SELECT platform, media_path FROM publishings WHERE piece_id = ?",
        ("mptpiece-003",),
    )
    assert len(pubs) == 2
    for r in pubs:
        assert r["media_path"] is None

    # publish_failures row recorded the fallback.
    pf = tmp_db.fetchall(
        "SELECT severity, failure_type FROM publish_failures WHERE source = ? ORDER BY id DESC LIMIT 1",
        ("mpt_runner",),
    )
    assert pf, "expected a publish_failures row"
    assert pf[0]["severity"] == "P2"
    assert pf[0]["failure_type"] == "mpt_download_failed"
