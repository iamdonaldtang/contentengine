"""Tests for ``lib.db`` — DDL bootstrap, state-machine adapter, concurrency, backup."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

EXPECTED_TABLES: set[str] = {
    "schema_migrations",
    "pieces",
    "state_events",
    "publishings",
    "metrics_daily",
    "landing_metrics",
    "leads",
    "newsletter_campaigns",
    "newsletter_link_clicks",
    "btouch_daily",
    "user_journey",
    "weekly_aggregates",
    "heartbeat",
    "publish_failures",
    "kol_watchlist",
    "candidates",
}


# --------------------------------------------------------------------------- #
# Test 1 — DDL bootstrap creates all 16 expected tables
# --------------------------------------------------------------------------- #


def test_ddl_bootstrap_creates_all_tables(tmp_db: Any) -> None:
    rows = tmp_db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    actual = {r["name"] for r in rows if not r["name"].startswith("sqlite_")}
    missing = EXPECTED_TABLES - actual
    assert not missing, f"missing tables after bootstrap: {sorted(missing)}"
    # Allow extras (future tables added by migrations) but they should be a
    # superset of EXPECTED_TABLES.
    assert EXPECTED_TABLES.issubset(actual)


# --------------------------------------------------------------------------- #
# Test 2 — pieces.create + update_state logs state_event
# --------------------------------------------------------------------------- #


def test_pieces_create_and_update_state_logs_event(tmp_db: Any) -> None:
    piece_id = "test-piece-002"
    tmp_db.pieces.create(piece_id, "piece_id: test-piece-002\n", actor="test")

    p = tmp_db.pieces.get(piece_id)
    assert p is not None
    assert p.state == "candidate"

    tmp_db.pieces.update_state(piece_id, "drafted", actor="claude", notes="round 1")

    p = tmp_db.pieces.get(piece_id)
    assert p is not None
    assert p.state == "drafted"

    events = tmp_db.fetchall(
        "SELECT * FROM state_events WHERE piece_id = ? ORDER BY id",
        (piece_id,),
    )
    # 1 row for create (None -> candidate), 1 for update (candidate -> drafted)
    assert len(events) == 2
    assert events[0]["from_state"] is None
    assert events[0]["to_state"] == "candidate"
    assert events[1]["from_state"] == "candidate"
    assert events[1]["to_state"] == "drafted"
    assert events[1]["actor"] == "claude"
    assert events[1]["notes"] == "round 1"


# --------------------------------------------------------------------------- #
# Test 3 — concurrent writes from 5 threads → no SQLITE_BUSY error
# --------------------------------------------------------------------------- #


def test_concurrent_writes_no_busy(tmp_db: Any) -> None:
    errors: list[BaseException] = []
    writes_per_thread = 10
    n_threads = 5

    def worker(idx: int) -> None:
        try:
            for i in range(writes_per_thread):
                tmp_db.heartbeat.record(
                    job_name=f"concurrent_test_{idx}",
                    status="ok",
                    duration_seconds=i,
                    rows_written=1,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"unexpected errors during concurrent writes: {errors!r}"

    # Confirm every write made it in.
    rows = tmp_db.fetchall(
        "SELECT COUNT(*) AS c FROM heartbeat WHERE job_name LIKE 'concurrent_test_%'"
    )
    assert rows[0]["c"] == n_threads * writes_per_thread


# --------------------------------------------------------------------------- #
# Test 4 — db.transaction() rollback on exception
# --------------------------------------------------------------------------- #


def test_transaction_rollback_on_exception(tmp_db: Any) -> None:
    """``db.transaction()`` must roll back on exception.

    Note: the context manager yields the raw connection; in-transaction
    statements must use that connection (not ``db.execute()``, which would
    deadlock on the same non-reentrant write lock).
    """
    piece_id = "txn-rollback-001"
    tmp_db.pieces.create(piece_id, "piece_id: txn-rollback-001\n", actor="test")

    with pytest.raises(RuntimeError, match="intentional"):
        with tmp_db.transaction() as conn:
            conn.execute(
                "UPDATE pieces SET notes = ? WHERE id = ?",
                ("should be rolled back", piece_id),
            )
            # Sanity: change is visible to this same connection mid-txn.
            row = conn.execute(
                "SELECT notes FROM pieces WHERE id = ?", (piece_id,)
            ).fetchone()
            assert row["notes"] == "should be rolled back"
            raise RuntimeError("intentional")

    # After the rollback, the notes column must be NULL again.
    row = tmp_db.fetchone("SELECT notes FROM pieces WHERE id = ?", (piece_id,))
    assert row is not None
    assert row["notes"] is None


# --------------------------------------------------------------------------- #
# Test 5 — backup_to() creates dest file with same row count
# --------------------------------------------------------------------------- #


def test_backup_to_preserves_rows(tmp_db: Any, tmp_path: Path) -> None:
    # Seed a handful of rows.
    for i in range(7):
        tmp_db.heartbeat.record(
            job_name=f"backup_test_{i}",
            status="ok",
            duration_seconds=i,
            rows_written=1,
        )

    dest = tmp_path / "backup.db"
    tmp_db.backup_to(dest)

    assert dest.exists() and dest.stat().st_size > 0

    # Open the backup independently and count rows. The whole point of
    # backup_to() is that the dest is a fully self-contained snapshot —
    # so a direct sqlite3.connect() here is exactly the right tool (and the
    # `no direct sqlite3.connect()` rule is for production code, not tests
    # verifying the backup file).
    src_count = tmp_db.fetchone(
        "SELECT COUNT(*) AS c FROM heartbeat WHERE job_name LIKE 'backup_test_%'"
    )["c"]

    conn = sqlite3.connect(str(dest))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM heartbeat WHERE job_name LIKE 'backup_test_%'"
        ).fetchone()
        dest_count = row[0]
    finally:
        conn.close()

    assert dest_count == src_count == 7
