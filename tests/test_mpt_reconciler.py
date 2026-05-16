"""Tests for ``jobs.mpt_reconciler`` — the A-design reliability backstop.

Mocks MPT's GET and the download spawner so tests are deterministic and
network-free. Exercises every branch of the per-row handler + the batch
loop's defensive crash handling.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest


def _reload_reconciler() -> Any:
    for mod_name in ("jobs.mpt_reconciler", "sources.mpt", "jobs.mpt_post_callback"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("jobs.mpt_reconciler")


def _seed_pending_submit(tmp_db: Any, piece_id: str, *, backdate_s: int = 0) -> int:
    tmp_db.pieces.create(piece_id, f"piece_id: {piece_id}\n", actor="test")
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    if backdate_s:
        tmp_db.execute(
            "UPDATE mpt_tasks SET created_at=datetime('now', ?) WHERE id=?",
            (f"-{backdate_s} seconds", row_id),
        )
    return row_id


def _seed_submitted(tmp_db: Any, piece_id: str, task_id: str, *, backdate_s: int = 0) -> int:
    tmp_db.pieces.create(piece_id, f"piece_id: {piece_id}\n", actor="test")
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, task_id)
    if backdate_s:
        tmp_db.execute(
            "UPDATE mpt_tasks SET submitted_at=datetime('now', ?), created_at=datetime('now', ?) WHERE id=?",
            (f"-{backdate_s} seconds", f"-{backdate_s} seconds", row_id),
        )
    return row_id


# --------------------------------------------------------------------------- #
# Empty batch
# --------------------------------------------------------------------------- #


def test_reconciler_no_pending_rows_returns_empty_counts(tmp_db: Any) -> None:
    reconciler = _reload_reconciler()
    counts = reconciler.run(age_threshold_s=0)
    assert counts["checked"] == 0
    # Heartbeat row should still be recorded for observability.
    hb = tmp_db.heartbeat.last("mpt_reconciler")
    assert hb is not None
    assert hb["status"] == "ok"


# --------------------------------------------------------------------------- #
# pending_submit branch
# --------------------------------------------------------------------------- #


def test_reconciler_pending_submit_too_old_marks_failed(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _reload_reconciler()
    row_id = _seed_pending_submit(tmp_db, "p-pending-old", backdate_s=600)

    counts = reconciler.run(age_threshold_s=300)
    assert counts["submit_failed"] == 1

    row = tmp_db.mpt_tasks.get_by_id(row_id)
    assert row["status"] == "failed"
    assert "no task_id" in (row["error"] or "")


def test_reconciler_pending_submit_too_young_not_touched(tmp_db: Any) -> None:
    reconciler = _reload_reconciler()
    row_id = _seed_pending_submit(tmp_db, "p-pending-young", backdate_s=60)

    # threshold > age → row excluded from get_pending_for_reconcile
    counts = reconciler.run(age_threshold_s=300)
    assert counts["checked"] == 0

    row = tmp_db.mpt_tasks.get_by_id(row_id)
    assert row["status"] == "pending_submit"


# --------------------------------------------------------------------------- #
# submitted + state=1 (success rescue)
# --------------------------------------------------------------------------- #


def test_reconciler_submitted_state_1_marks_completed_and_spawns_download(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-rescue-ok", "task-rescue-1", backdate_s=600)

    # Mock MPT GET → terminal success.
    monkeypatch.setattr(
        reconciler.mpt, "get_task",
        lambda task_id: {"state": 1, "progress": 100, "video_url": "http://mpt/x.mp4"},
    )
    spawn_mock = MagicMock(name="spawn_download")
    monkeypatch.setattr(reconciler, "spawn_download", spawn_mock)

    counts = reconciler.run(age_threshold_s=300)
    assert counts["rescued_completed"] == 1
    assert counts["race_lost"] == 0

    row = tmp_db.mpt_tasks.get_by_task_id("task-rescue-1")
    assert row["status"] == "completed"
    assert row["terminal_source"] == "reconciler"
    assert row["mp4_url"] == "http://mpt/x.mp4"
    assert row["callback_received_at"] is None  # reconciler-won

    spawn_mock.assert_called_once_with("task-rescue-1", "p-rescue-ok")


def test_reconciler_state_1_no_video_url_uses_candidate(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If MPT doesn't include video_url, fall back to the canonical URL convention."""
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-rescue-canon", "task-no-url", backdate_s=600)

    monkeypatch.setattr(
        reconciler.mpt, "get_task",
        lambda task_id: {"state": 1, "progress": 100},  # no video_url key
    )
    monkeypatch.setattr(reconciler, "spawn_download", MagicMock())

    reconciler.run(age_threshold_s=300)
    row = tmp_db.mpt_tasks.get_by_task_id("task-no-url")
    assert row["mp4_url"]
    assert "/tasks/task-no-url/" in row["mp4_url"]


# --------------------------------------------------------------------------- #
# submitted + state=-1 (failure rescue)
# --------------------------------------------------------------------------- #


def test_reconciler_submitted_state_minus_1_marks_failed(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-rescue-fail", "task-fail-x", backdate_s=600)

    monkeypatch.setattr(
        reconciler.mpt, "get_task",
        lambda task_id: {"state": -1, "progress": 50, "error": "ffmpeg crash"},
    )
    spawn_mock = MagicMock()
    monkeypatch.setattr(reconciler, "spawn_download", spawn_mock)

    counts = reconciler.run(age_threshold_s=300)
    assert counts["rescued_failed"] == 1

    row = tmp_db.mpt_tasks.get_by_task_id("task-fail-x")
    assert row["status"] == "failed"
    assert "ffmpeg crash" in (row["error"] or "")
    assert row["terminal_source"] == "reconciler"
    spawn_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# submitted + state non-terminal (still running)
# --------------------------------------------------------------------------- #


def test_reconciler_submitted_still_running_no_op(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-running", "task-still", backdate_s=600)

    monkeypatch.setattr(
        reconciler.mpt, "get_task",
        lambda task_id: {"state": 4, "progress": 60},  # MPT internal "running"
    )
    spawn_mock = MagicMock()
    monkeypatch.setattr(reconciler, "spawn_download", spawn_mock)

    counts = reconciler.run(age_threshold_s=300, stale_threshold_s=99_999)
    assert counts["still_running"] == 1
    row = tmp_db.mpt_tasks.get_by_task_id("task-still")
    assert row["status"] == "submitted"
    spawn_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# stale (non-terminal for > stale_threshold_s)
# --------------------------------------------------------------------------- #


def test_reconciler_stale_after_threshold(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _reload_reconciler()
    # 7h old, still state=4
    _seed_submitted(tmp_db, "p-stale", "task-stale", backdate_s=7 * 3600)

    monkeypatch.setattr(
        reconciler.mpt, "get_task",
        lambda task_id: {"state": 4, "progress": 40},
    )
    monkeypatch.setattr(reconciler, "spawn_download", MagicMock())

    counts = reconciler.run(age_threshold_s=300, stale_threshold_s=6 * 3600)
    assert counts["stale"] == 1

    row = tmp_db.mpt_tasks.get_by_task_id("task-stale")
    assert row["status"] == "stale"
    assert row["terminal_source"] == "reconciler"


# --------------------------------------------------------------------------- #
# Race-lost (real callback won first)
# --------------------------------------------------------------------------- #


def test_reconciler_race_lost_no_spawn(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates: real callback processed the row → status='completed' →
    reconciler runs after → MPT still says state=1 but mark_completed
    returns False because row is already terminal."""
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-race", "task-race", backdate_s=600)

    # Real callback already marked completed.
    tmp_db.mpt_tasks.mark_completed("task-race", "http://cb-url.mp4", source="callback")

    # But the row's status is now 'completed', so get_pending_for_reconcile
    # filters it out before we even GET MPT. Reconciler does no work.
    spawn_mock = MagicMock()
    get_mock = MagicMock()
    monkeypatch.setattr(reconciler, "spawn_download", spawn_mock)
    monkeypatch.setattr(reconciler.mpt, "get_task", get_mock)

    counts = reconciler.run(age_threshold_s=300)
    assert counts["checked"] == 0
    get_mock.assert_not_called()
    spawn_mock.assert_not_called()


def test_reconciler_race_lost_during_simulate(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tighter race: between get_pending_for_reconcile and mark_completed,
    a real callback flips the row. The atomic UPDATE's WHERE='submitted'
    clause means mark_completed returns False → race_lost increments,
    no spawn."""
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-tight-race", "task-tight", backdate_s=600)

    flipped = {"done": False}

    def _flip_then_get(task_id: str) -> dict[str, Any]:
        # Simulate the callback arriving between reading the batch and
        # us calling mark_completed.
        if not flipped["done"]:
            tmp_db.mpt_tasks.mark_completed("task-tight", "callback-url", source="callback")
            flipped["done"] = True
        return {"state": 1, "video_url": "reconciler-url"}

    monkeypatch.setattr(reconciler.mpt, "get_task", _flip_then_get)
    spawn_mock = MagicMock()
    monkeypatch.setattr(reconciler, "spawn_download", spawn_mock)

    counts = reconciler.run(age_threshold_s=300)
    assert counts["race_lost"] == 1
    assert counts["rescued_completed"] == 0
    spawn_mock.assert_not_called()

    # Callback's writes survive (mp4_url + terminal_source).
    row = tmp_db.mpt_tasks.get_by_task_id("task-tight")
    assert row["mp4_url"] == "callback-url"
    assert row["terminal_source"] == "callback"


# --------------------------------------------------------------------------- #
# MPT GET failure — graceful skip
# --------------------------------------------------------------------------- #


def test_reconciler_mpt_get_failure_continues_batch(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-err-1", "task-err-1", backdate_s=600)
    _seed_submitted(tmp_db, "p-err-2", "task-good-2", backdate_s=600)

    def _flaky_get(task_id: str) -> dict[str, Any]:
        if task_id == "task-err-1":
            raise reconciler.MPTError("connection reset")
        return {"state": 1, "video_url": "ok.mp4"}

    monkeypatch.setattr(reconciler.mpt, "get_task", _flaky_get)
    spawn_mock = MagicMock()
    monkeypatch.setattr(reconciler, "spawn_download", spawn_mock)

    counts = reconciler.run(age_threshold_s=300)
    assert counts["mpt_errors"] == 1
    assert counts["rescued_completed"] == 1
    spawn_mock.assert_called_once_with("task-good-2", "p-err-2")

    # First row's status unchanged — try again next tick.
    row1 = tmp_db.mpt_tasks.get_by_task_id("task-err-1")
    assert row1["status"] == "submitted"


# --------------------------------------------------------------------------- #
# Defensive per-row crash isolation
# --------------------------------------------------------------------------- #


def test_reconciler_per_row_crash_does_not_abort_batch(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception inside a handler must not kill the whole tick.

    This protects production from "one corrupted row blocks all rescue."
    """
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-boom", "task-boom", backdate_s=600)
    _seed_submitted(tmp_db, "p-after", "task-after", backdate_s=600)

    def _kaboom(task_id: str) -> dict[str, Any]:
        if task_id == "task-boom":
            raise RuntimeError("unexpected non-MPTError")
        return {"state": 1}

    monkeypatch.setattr(reconciler.mpt, "get_task", _kaboom)
    monkeypatch.setattr(reconciler, "spawn_download", MagicMock())

    counts = reconciler.run(age_threshold_s=300)
    assert counts["mpt_errors"] >= 1
    # Second row still rescued.
    row_after = tmp_db.mpt_tasks.get_by_task_id("task-after")
    assert row_after["status"] == "completed"


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #


def test_reconciler_records_heartbeat(tmp_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    reconciler = _reload_reconciler()
    _seed_submitted(tmp_db, "p-hb", "task-hb", backdate_s=600)
    monkeypatch.setattr(
        reconciler.mpt, "get_task",
        lambda task_id: {"state": 1, "video_url": "x.mp4"},
    )
    monkeypatch.setattr(reconciler, "spawn_download", MagicMock())

    reconciler.run(age_threshold_s=300)

    hb = tmp_db.heartbeat.last("mpt_reconciler")
    assert hb is not None
    assert hb["status"] == "ok"
    assert hb["rows_written"] >= 1  # at least the rescued row
