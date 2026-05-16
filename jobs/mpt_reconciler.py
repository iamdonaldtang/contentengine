"""jobs/mpt_reconciler.py · Self-heal for dropped MPT callbacks (A-design S5).

WHY THIS EXISTS
---------------
The A-design async render path depends on MoneyPrinterTurbo POSTing a
webhook to ``/api/mpt-callback`` when each task terminates. Callbacks
can be dropped in several ways the engine cannot prevent:

  * MPT crashed *after* finishing the render but before its
    callback POST completed.
  * Network blip / Docker DNS hiccup between MPT and engine.
  * Engine was restarting at the exact second MPT POSTed.
  * MPT's local DLQ filled up (3 retries exhausted in send_callback).

Without a backstop, those tasks would sit in ``mpt_tasks.status='submitted'``
forever, blocking publish_immediate for that piece. This module is the
**reliability cornerstone of the A-design**.

WHAT IT DOES
------------
Every 5 min the cron tick:

  1. Selects all ``mpt_tasks`` rows in ``pending_submit`` or ``submitted``
     state that haven't been touched in > 5 min (configurable via the
     ``RECONCILE_AGE_THRESHOLD_S`` env).

  2. For each row:

       * ``pending_submit`` (no task_id assigned) → mpt_runner crashed
         between the INSERT and the post-submit UPDATE. Mark failed +
         P1 alert. (Recovery: re-run mpt_runner manually after fixing
         whatever crashed it; that creates a fresh mpt_tasks row.)

       * ``submitted`` → GET MPT task state (single call, never blocks):

           - ``state=1`` (success): simulate the callback by calling
             ``db.mpt_tasks.mark_completed(source='reconciler')`` and
             spawning the same download as the real callback handler.
             The atomic ``UPDATE ... WHERE status='submitted'`` makes
             this idempotent with a concurrent real callback — whoever
             wins runs the download, the loser no-ops.

           - ``state=-1`` (terminal failure): simulate failed callback.
             P1 alert (we never learned from a real callback, so the
             operator may not have noticed).

           - any other state (still rendering): no-op. Try next tick.

           - if ``state`` non-terminal but row aged > 6 h: mark stale +
             P1 alert. Indicates either MPT is permanently hung or our
             task_id is gone from MPT's task table. Operator intervention.

       * MPT GET fails: log and continue. Will retry next tick. Don't
         transition the row — we don't know its true state.

  3. Records a single heartbeat with counts.

This module imports the same download spawner as the webhook (via
``jobs.mpt_post_callback.spawn_download``) so the post-completion flow
is identical regardless of which path won.

Test surface: see ``tests/test_mpt_reconciler.py``. Hard-mocks MPT GET
+ uses tmp_db fixture so no network and no real cron.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from typing import Any

from jobs.mpt_post_callback import spawn_download
from lib.db import db
from lib.lark import alert as lark_alert
from sources.mpt import MPTConfigError, MPTError, mpt


logger = logging.getLogger("mpt_reconciler")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# How old a row must be (since submitted_at, or created_at if pending_submit)
# before we even consider GETting MPT. Default 5 min — matches the cron tick
# rate so freshly submitted rows aren't reconciled before MPT has had any
# realistic chance to finish.
DEFAULT_AGE_THRESHOLD_S = int(os.environ.get("RECONCILE_AGE_THRESHOLD_S", "300"))

# Beyond this age, a row still in non-terminal state is declared stale.
# Default 6 h — generous enough to cover the worst MPT renders (Whisper
# subtitle on slow CPU), short enough that "stuck forever" isn't a thing.
DEFAULT_STALE_THRESHOLD_S = int(os.environ.get("RECONCILE_STALE_THRESHOLD_S", str(6 * 3600)))

# Max rows touched per tick — safety valve against runaway state.
DEFAULT_BATCH_LIMIT = int(os.environ.get("RECONCILE_BATCH_LIMIT", "50"))

# MPT internal task state semantics (from sources/mpt._MPT_TERMINAL_STATES).
_MPT_STATE_SUCCESS = 1
_MPT_STATE_FAILED = -1


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_sqlite_ts(value: str | None) -> dt.datetime | None:
    """SQLite CURRENT_TIMESTAMP returns ``YYYY-MM-DD HH:MM:SS`` UTC, no TZ.
    Coerce to a timezone-aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace(" ", "T"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        logger.warning("could not parse sqlite ts %r", value)
        return None


def _age_seconds(value: str | None) -> float:
    """Seconds elapsed since the given UTC SQLite timestamp string. ``+inf``
    if the value is missing / unparseable (treats unknown as ancient — safer
    for stale detection)."""
    parsed = _parse_sqlite_ts(value)
    if parsed is None:
        return float("inf")
    return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()


def _candidate_mp4_url(task_id: str) -> str:
    """Default MPT mp4 URL convention — used when reconciler wins the race
    and there's no real callback payload to pull mp4_url from. The download
    helper tries multiple URL conventions so this is informational only."""
    base = (mpt.base_url or "http://moneyprinterturbo-api:8090").rstrip("/")
    return f"{base}/tasks/{task_id}/final-1.mp4"


# --------------------------------------------------------------------------- #
# Per-row handlers
# --------------------------------------------------------------------------- #


def _handle_pending_submit(row: Any, counts: dict[str, int]) -> None:
    """``pending_submit`` row aged > threshold — mpt_runner died after INSERT
    but before mark_submitted. Mark failed + alert."""
    age = _age_seconds(row["created_at"])
    err = f"no task_id after {age:.0f}s — mpt_runner crashed between INSERT and mark_submitted?"
    changed = db.mpt_tasks.mark_submit_failed(row["id"], err)
    if changed:
        counts["submit_failed"] += 1
        logger.warning("reconcile: pending_submit failed piece=%s row=%d age=%.0fs", row["piece_id"], row["id"], age)
        try:
            lark_alert(
                "P1",
                f"mpt_reconciler: pending_submit stuck for piece={row['piece_id']}",
                {"row_id": row["id"], "age_seconds": int(age)},
            )
        except Exception:
            logger.exception("reconcile: lark dispatch crashed")


def _handle_submitted(row: Any, *, stale_threshold_s: int, counts: dict[str, int]) -> None:
    """``submitted`` row — GET MPT for state, simulate callback if terminal."""
    task_id = row["task_id"]
    piece_id = row["piece_id"]

    try:
        task = mpt.get_task(task_id)
    except (MPTError, MPTConfigError) as exc:
        # Transient — try again next tick. Don't transition the row.
        logger.warning("reconcile: MPT GET failed task=%s err=%s", task_id, exc)
        counts["mpt_errors"] += 1
        return

    state = task.get("state")
    progress = task.get("progress")

    if state == _MPT_STATE_SUCCESS:
        mp4_url = task.get("video_url") or _candidate_mp4_url(task_id)
        won = db.mpt_tasks.mark_completed(task_id, mp4_url, source="reconciler")
        if won:
            spawn_download(task_id, piece_id)
            counts["rescued_completed"] += 1
            logger.info(
                "reconcile: WON race task=%s piece=%s state=1 — spawned download",
                task_id, piece_id,
            )
        else:
            counts["race_lost"] += 1
            logger.info(
                "reconcile: LOST race task=%s — real callback arrived first",
                task_id,
            )
        return

    if state == _MPT_STATE_FAILED:
        error_msg = task.get("error") or "MPT reported state=-1 (no callback received)"
        won = db.mpt_tasks.mark_failed(task_id, error_msg, source="reconciler")
        if won:
            counts["rescued_failed"] += 1
            logger.warning(
                "reconcile: WON failure race task=%s piece=%s err=%s",
                task_id, piece_id, error_msg[:200],
            )
            try:
                lark_alert(
                    "P1",
                    f"mpt_reconciler: render failed (no callback) piece={piece_id}",
                    {"task_id": task_id, "error": error_msg[:300]},
                )
            except Exception:
                logger.exception("reconcile: lark dispatch crashed")
        else:
            counts["race_lost"] += 1
        return

    # Non-terminal state — still rendering or stuck.
    submitted_age = _age_seconds(row["submitted_at"])
    if submitted_age > stale_threshold_s:
        err = f"stuck at state={state} progress={progress} for {submitted_age/3600:.1f}h (> stale threshold)"
        changed = db.mpt_tasks.mark_stale(task_id, err)
        if changed:
            counts["stale"] += 1
            logger.error(
                "reconcile: STALE task=%s piece=%s state=%s progress=%s age=%.0fs",
                task_id, piece_id, state, progress, submitted_age,
            )
            try:
                lark_alert(
                    "P1",
                    f"mpt_reconciler: STALE task for piece={piece_id}",
                    {"task_id": task_id, "state": state, "progress": progress,
                     "age_hours": round(submitted_age / 3600, 1)},
                )
            except Exception:
                logger.exception("reconcile: lark dispatch crashed")
    else:
        counts["still_running"] += 1
        logger.debug(
            "reconcile: still running task=%s state=%s progress=%s age=%.0fs",
            task_id, state, progress, submitted_age,
        )


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #


def run(
    *,
    age_threshold_s: int = DEFAULT_AGE_THRESHOLD_S,
    stale_threshold_s: int = DEFAULT_STALE_THRESHOLD_S,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
) -> dict[str, int]:
    """Single reconciliation tick. Returns counts dict for the heartbeat."""
    started_at = time.monotonic()
    counts: dict[str, int] = {
        "checked": 0,
        "submit_failed": 0,
        "rescued_completed": 0,
        "rescued_failed": 0,
        "stale": 0,
        "still_running": 0,
        "race_lost": 0,
        "mpt_errors": 0,
    }

    rows = db.mpt_tasks.get_pending_for_reconcile(
        older_than_seconds=age_threshold_s,
        limit=batch_limit,
    )
    counts["checked"] = len(rows)
    if not rows:
        _record_heartbeat("ok", started_at, counts)
        return counts

    for row in rows:
        try:
            if row["status"] == "pending_submit":
                _handle_pending_submit(row, counts)
            elif row["status"] == "submitted":
                _handle_submitted(row, stale_threshold_s=stale_threshold_s, counts=counts)
            else:
                # Defensive: status filter in get_pending_for_reconcile should
                # have screened these out; if not it's a bug.
                logger.error("reconcile: unexpected status row_id=%d status=%s", row["id"], row["status"])
        except Exception:
            counts["mpt_errors"] += 1
            logger.exception(
                "reconcile: per-row handler crashed row_id=%s task=%s",
                row["id"], row.get("task_id") if hasattr(row, "get") else row["task_id"],
            )
            # Continue with the rest of the batch — one bad row shouldn't block all.

    _record_heartbeat("ok", started_at, counts)
    logger.info("reconcile done %s", counts)
    return counts


def _record_heartbeat(status: str, started_at: float, counts: dict[str, int]) -> None:
    rows_written = (
        counts["submit_failed"]
        + counts["rescued_completed"]
        + counts["rescued_failed"]
        + counts["stale"]
    )
    try:
        db.heartbeat.record(
            job_name="mpt_reconciler",
            status=status,
            duration_seconds=int(time.monotonic() - started_at),
            rows_written=rows_written,
            error_message=None,
        )
    except Exception:
        logger.exception("reconcile: heartbeat record failed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MPT task reconciler — self-heal for dropped webhooks.")
    parser.add_argument("--age-threshold-s", type=int, default=DEFAULT_AGE_THRESHOLD_S,
                        help="Min row age (default %(default)s) before reconciliation kicks in.")
    parser.add_argument("--stale-threshold-s", type=int, default=DEFAULT_STALE_THRESHOLD_S,
                        help="Beyond this age, non-terminal rows are declared stale (default %(default)s).")
    parser.add_argument("--batch-limit", type=int, default=DEFAULT_BATCH_LIMIT,
                        help="Max rows to process per tick (default %(default)s).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    counts = run(
        age_threshold_s=args.age_threshold_s,
        stale_threshold_s=args.stale_threshold_s,
        batch_limit=args.batch_limit,
    )
    return 0 if counts["mpt_errors"] == 0 else 0  # MPT errors are warnings, not run failures


if __name__ == "__main__":
    sys.exit(main())
