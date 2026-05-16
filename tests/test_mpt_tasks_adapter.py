"""Tests for ``lib.db._MptTasksAdapter`` — async MPT task state machine.

Coverage:
  * Happy path: create_pending → mark_submitted → mark_completed.
  * Idempotent terminal transitions (callback retry, callback/reconciler race).
  * State machine guards (mark_completed only from 'submitted', etc.).
  * Source validation (only 'callback' / 'reconciler' allowed).
  * Reconciler reads (age filter, status filter).
  * Argument validation on the reads.
"""
from __future__ import annotations

import time
from typing import Any

import pytest


# ----- helpers --------------------------------------------------------------- #


def _make_piece(db: Any, piece_id: str = "test-piece-mpt-001") -> str:
    """Insert a minimal piece so FK constraint passes."""
    db.pieces.create(piece_id, f"piece_id: {piece_id}\nhook_type: 47pct_bot\n", actor="test")
    return piece_id


# ----- create_pending -------------------------------------------------------- #


def test_create_pending_returns_int_rowid(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    assert isinstance(row_id, int)
    assert row_id > 0


def test_create_pending_initial_state(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    row = tmp_db.mpt_tasks.get_by_id(row_id)
    assert row is not None
    assert row["status"] == "pending_submit"
    assert row["task_id"] is None
    assert row["submitted_at"] is None
    assert row["completed_at"] is None
    assert row["mp4_url"] is None
    assert row["media_path"] is None
    assert row["error"] is None
    assert row["submit_attempt"] == 0
    assert row["terminal_source"] is None
    assert row["piece_id"] == piece_id


def test_create_pending_two_rows_both_null_task_id_ok(tmp_db: Any) -> None:
    """Partial unique index must allow multiple in-flight pending submits."""
    piece_id = _make_piece(tmp_db)
    a = tmp_db.mpt_tasks.create_pending(piece_id)
    b = tmp_db.mpt_tasks.create_pending(piece_id)
    assert a != b


# ----- mark_submitted -------------------------------------------------------- #


def test_mark_submitted_sets_task_id_and_status(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    ok = tmp_db.mpt_tasks.mark_submitted(row_id, "mpt-task-abc")
    assert ok is True
    row = tmp_db.mpt_tasks.get_by_id(row_id)
    assert row["status"] == "submitted"
    assert row["task_id"] == "mpt-task-abc"
    assert row["submitted_at"] is not None
    assert row["submit_attempt"] == 1


def test_mark_submitted_blocked_after_terminal(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submit_failed(row_id, "boom")
    ok = tmp_db.mpt_tasks.mark_submitted(row_id, "should-not-stick")
    assert ok is False
    row = tmp_db.mpt_tasks.get_by_id(row_id)
    assert row["status"] == "failed"
    assert row["task_id"] is None


def test_mark_submitted_task_id_must_be_unique_across_non_null(tmp_db: Any) -> None:
    """Partial unique index protects against duplicate task_id once assigned."""
    import sqlite3

    piece_id = _make_piece(tmp_db)
    a = tmp_db.mpt_tasks.create_pending(piece_id)
    b = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(a, "dup-task-id")
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.mpt_tasks.mark_submitted(b, "dup-task-id")


# ----- mark_submit_failed ---------------------------------------------------- #


def test_mark_submit_failed_transitions_to_failed_with_error(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    ok = tmp_db.mpt_tasks.mark_submit_failed(row_id, "HTTP 500 from MPT")
    assert ok is True
    row = tmp_db.mpt_tasks.get_by_id(row_id)
    assert row["status"] == "failed"
    assert row["error"] == "HTTP 500 from MPT"
    assert row["completed_at"] is not None
    assert row["task_id"] is None  # never got one
    assert row["submit_attempt"] == 1


# ----- mark_completed (callback path) ---------------------------------------- #


def test_mark_completed_callback_wins_first_call_returns_true(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-x")

    won = tmp_db.mpt_tasks.mark_completed("task-x", "http://mpt:8090/tasks/task-x/final-1.mp4", source="callback")
    assert won is True

    row = tmp_db.mpt_tasks.get_by_task_id("task-x")
    assert row["status"] == "completed"
    assert row["mp4_url"] == "http://mpt:8090/tasks/task-x/final-1.mp4"
    assert row["terminal_source"] == "callback"
    assert row["callback_received_at"] is not None
    assert row["completed_at"] is not None


def test_mark_completed_second_call_returns_false_no_double_write(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-x")

    first = tmp_db.mpt_tasks.mark_completed("task-x", "url-1", source="callback")
    snapshot = dict(tmp_db.mpt_tasks.get_by_task_id("task-x"))
    second = tmp_db.mpt_tasks.mark_completed("task-x", "url-2", source="callback")

    assert first is True
    assert second is False
    after = dict(tmp_db.mpt_tasks.get_by_task_id("task-x"))
    # mp4_url, completed_at, callback_received_at all must be unchanged
    assert after["mp4_url"] == snapshot["mp4_url"]
    assert after["completed_at"] == snapshot["completed_at"]
    assert after["callback_received_at"] == snapshot["callback_received_at"]


def test_mark_completed_reconciler_does_not_set_callback_received_at(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-y")

    won = tmp_db.mpt_tasks.mark_completed("task-y", "url-y", source="reconciler")
    assert won is True
    row = tmp_db.mpt_tasks.get_by_task_id("task-y")
    assert row["status"] == "completed"
    assert row["terminal_source"] == "reconciler"
    assert row["callback_received_at"] is None  # reconciler-won: no real callback
    assert row["completed_at"] is not None


def test_mark_completed_invalid_source_raises(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-z")
    with pytest.raises(ValueError, match="source must be one of"):
        tmp_db.mpt_tasks.mark_completed("task-z", "url-z", source="bogus")


def test_mark_completed_blocked_if_never_submitted(tmp_db: Any) -> None:
    """If callback arrives for an unknown task_id, mark_completed returns False."""
    won = tmp_db.mpt_tasks.mark_completed("never-submitted-id", "url", source="callback")
    assert won is False


def test_mark_completed_blocked_from_failed(tmp_db: Any) -> None:
    """If row already failed, callback that says 'completed' is ignored."""
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-q")
    tmp_db.mpt_tasks.mark_failed("task-q", "MPT said -1", source="callback")
    won = tmp_db.mpt_tasks.mark_completed("task-q", "url-late", source="callback")
    assert won is False
    row = tmp_db.mpt_tasks.get_by_task_id("task-q")
    assert row["status"] == "failed"


# ----- mark_failed ----------------------------------------------------------- #


def test_mark_failed_callback_path(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-fail")

    won = tmp_db.mpt_tasks.mark_failed("task-fail", "render error", source="callback")
    assert won is True
    row = tmp_db.mpt_tasks.get_by_task_id("task-fail")
    assert row["status"] == "failed"
    assert row["error"] == "render error"
    assert row["terminal_source"] == "callback"
    assert row["callback_received_at"] is not None


def test_mark_failed_idempotent(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-fail2")
    first = tmp_db.mpt_tasks.mark_failed("task-fail2", "boom1", source="callback")
    second = tmp_db.mpt_tasks.mark_failed("task-fail2", "boom2", source="callback")
    assert first is True
    assert second is False
    row = tmp_db.mpt_tasks.get_by_task_id("task-fail2")
    assert row["error"] == "boom1"  # first wins


def test_mark_failed_invalid_source_raises(tmp_db: Any) -> None:
    with pytest.raises(ValueError, match="source must be one of"):
        tmp_db.mpt_tasks.mark_failed("anything", "x", source="evil")


# ----- mark_stale (reconciler-only) ----------------------------------------- #


def test_mark_stale_only_from_submitted(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    # No mark_submitted call — still pending_submit.
    won = tmp_db.mpt_tasks.mark_stale("any", error="should not")
    assert won is False

    tmp_db.mpt_tasks.mark_submitted(row_id, "task-stale")
    won = tmp_db.mpt_tasks.mark_stale("task-stale")
    assert won is True
    row = tmp_db.mpt_tasks.get_by_task_id("task-stale")
    assert row["status"] == "stale"
    assert row["terminal_source"] == "reconciler"
    assert row["error"] == "stuck > 6h, no terminal state"


# ----- callback vs reconciler race ------------------------------------------- #


def test_callback_and_reconciler_race_only_one_wins(tmp_db: Any) -> None:
    """Simulates: reconciler GETs MPT, sees state=1, calls mark_completed
    just as the real callback arrives and also calls mark_completed.
    The atomic UPDATE serialises them; exactly one returns True."""
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-race")

    cb_won = tmp_db.mpt_tasks.mark_completed("task-race", "url-cb", source="callback")
    rec_won = tmp_db.mpt_tasks.mark_completed("task-race", "url-rec", source="reconciler")

    assert cb_won is True
    assert rec_won is False
    row = tmp_db.mpt_tasks.get_by_task_id("task-race")
    assert row["terminal_source"] == "callback"
    assert row["mp4_url"] == "url-cb"


# ----- set_media_path -------------------------------------------------------- #


def test_set_media_path_unconditional(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-mp")
    tmp_db.mpt_tasks.mark_completed("task-mp", "url", source="callback")

    tmp_db.mpt_tasks.set_media_path("task-mp", "/app/runtime/drafts/p/shorts_60s.mp4")
    row = tmp_db.mpt_tasks.get_by_task_id("task-mp")
    assert row["media_path"] == "/app/runtime/drafts/p/shorts_60s.mp4"


# ----- get_pending_for_reconcile -------------------------------------------- #


def test_get_pending_for_reconcile_excludes_terminal_states(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    completed_row = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(completed_row, "task-done")
    tmp_db.mpt_tasks.mark_completed("task-done", "u", source="callback")

    failed_row = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submit_failed(failed_row, "boom")

    # 0-age threshold — anything submitted before *now* qualifies. Terminal
    # states must still be excluded.
    rows = tmp_db.mpt_tasks.get_pending_for_reconcile(older_than_seconds=0)
    task_ids = {r["task_id"] for r in rows}
    assert "task-done" not in task_ids
    # failed row has task_id=None, so it can't be in there by task_id; check
    # explicitly that no row with status='failed' returned:
    assert all(r["status"] in ("submitted", "pending_submit") for r in rows)


def test_get_pending_for_reconcile_age_filter(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    tmp_db.mpt_tasks.mark_submitted(row_id, "task-fresh")

    # Just-submitted row is younger than 300s threshold — must not appear.
    fresh = tmp_db.mpt_tasks.get_pending_for_reconcile(older_than_seconds=300)
    assert all(r["task_id"] != "task-fresh" for r in fresh)

    # Backdate submitted_at by 10 min via raw SQL to simulate a stuck row.
    tmp_db.execute(
        "UPDATE mpt_tasks SET submitted_at=datetime('now','-600 seconds') WHERE id=?",
        (row_id,),
    )
    stale_candidates = tmp_db.mpt_tasks.get_pending_for_reconcile(older_than_seconds=300)
    assert any(r["task_id"] == "task-fresh" for r in stale_candidates)


def test_get_pending_for_reconcile_picks_up_pending_submit_via_created_at(tmp_db: Any) -> None:
    """pending_submit rows have submitted_at=NULL — age must fall back to created_at."""
    piece_id = _make_piece(tmp_db)
    row_id = tmp_db.mpt_tasks.create_pending(piece_id)
    # Backdate created_at by 10min.
    tmp_db.execute(
        "UPDATE mpt_tasks SET created_at=datetime('now','-600 seconds') WHERE id=?",
        (row_id,),
    )
    rows = tmp_db.mpt_tasks.get_pending_for_reconcile(older_than_seconds=300)
    assert any(r["id"] == row_id and r["status"] == "pending_submit" for r in rows)


def test_get_pending_for_reconcile_bad_args(tmp_db: Any) -> None:
    with pytest.raises(ValueError):
        tmp_db.mpt_tasks.get_pending_for_reconcile(older_than_seconds=-1)
    with pytest.raises(ValueError):
        tmp_db.mpt_tasks.get_pending_for_reconcile(limit=0)


def test_get_pending_for_reconcile_respects_limit(tmp_db: Any) -> None:
    piece_id = _make_piece(tmp_db)
    ids = []
    for i in range(5):
        r = tmp_db.mpt_tasks.create_pending(piece_id)
        tmp_db.mpt_tasks.mark_submitted(r, f"task-{i}")
        ids.append(r)
    tmp_db.execute(
        f"UPDATE mpt_tasks SET submitted_at=datetime('now','-600 seconds') WHERE id IN ({','.join(['?']*len(ids))})",
        tuple(ids),
    )
    rows = tmp_db.mpt_tasks.get_pending_for_reconcile(older_than_seconds=300, limit=3)
    assert len(rows) == 3


# ----- get_by_task_id -------------------------------------------------------- #


def test_get_by_task_id_returns_none_for_missing(tmp_db: Any) -> None:
    assert tmp_db.mpt_tasks.get_by_task_id("nonexistent") is None
