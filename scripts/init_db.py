"""Bootstrap script · one-shot initialization of state.db.

Usage:
    python -m scripts.init_db                 # idempotent, safe to re-run
    python -m scripts.init_db --reset         # drop and rebuild (DESTRUCTIVE)
    python -m scripts.init_db --verify-only   # exit non-zero if schema differs

The script simply triggers ``lib.db.Database`` import, which auto-creates the
runtime directory + 14-table schema. ``--reset`` deletes the file first.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow execution either as `python -m scripts.init_db` or `python scripts/init_db.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import DDL_STATEMENTS, db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("init_db")


EXPECTED_TABLES = {
    # Core (DDL_STATEMENTS in lib/db.py)
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
    # V3 Q3 add-ons (migrations 004 / 005 / 006)
    "cohort_metrics",
    "ab_results",
    "markov_credits",
}


def verify() -> bool:
    rows = db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    actual = {r["name"] for r in rows if not r["name"].startswith("sqlite_")}
    missing = EXPECTED_TABLES - actual
    extra = actual - EXPECTED_TABLES
    if missing:
        logger.error("missing tables: %s", sorted(missing))
    if extra:
        logger.warning("extra tables (not in DDL set): %s", sorted(extra))
    return not missing


def reset() -> None:
    db_path = db.path
    if db_path.exists():
        # Close any per-thread connection first.
        db.close_thread_connection()
        logger.warning("DELETING %s (--reset)", db_path)
        db_path.unlink()
        # Also nuke WAL/SHM sidecars.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_suffix(db_path.suffix + suffix)
            if sidecar.exists():
                sidecar.unlink()
    # Re-import Database to rebuild the schema.
    from importlib import reload

    import lib.db as db_module

    reload(db_module)
    logger.info("schema rebuilt at %s", db_module.db.path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Initialize TaskOn marketing engine SQLite state.db")
    p.add_argument("--reset", action="store_true", help="drop and rebuild (DESTRUCTIVE)")
    p.add_argument("--verify-only", action="store_true", help="exit non-zero if schema mismatches")
    args = p.parse_args(argv)

    if args.verify_only:
        ok = verify()
        return 0 if ok else 2

    if args.reset:
        reset()
    else:
        # Importing db has already triggered DDL bootstrap. Nothing else needed.
        logger.info("schema ready at %s", db.path)
        logger.info("applied %d DDL statements", len(DDL_STATEMENTS))

    if not verify():
        logger.error("verification failed")
        return 2
    logger.info("OK: %d tables present", len(EXPECTED_TABLES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
