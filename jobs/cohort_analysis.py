"""Cohort Analysis · monthly (1st Sunday 19:30) cron (PRD §2.7 V3 + §7 Q3).

Tracks lead cohorts by ISO signup week and computes the D7 / D14 / D30
conversion progression through ``qualified`` -> ``sql_verified``.

A "cohort" here = all leads whose ``first_seen_at`` falls inside one ISO
week. For each cohort we ask:

* How many leads have we acquired in this week?
* Of those, how many reached the ``qualified=1`` state within 7 / 14 / 30
  days of their first_seen_at?
* Of those, how many reached ``sql_status='verified'`` within the same
  windows? (We use ``sql_verified_at`` as the milestone timestamp.)

Output:
* Idempotent UPSERT into ``cohort_metrics`` (PK on cohort_week).
* Markdown report at ``runtime/cohort_report_<week>.md`` containing a
  matrix table with rows=signup_weeks and cols=D7/D14/D30.

CLI::

    python -m jobs.cohort_analysis
    python -m jobs.cohort_analysis --weeks 12
    python -m jobs.cohort_analysis --output runtime/custom.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402

logger = logging.getLogger(__name__)

JOB_NAME = "cohort_analysis"

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR") or (_ENGINE_ROOT / "runtime"))

DEFAULT_COHORT_COUNT = 8

_WEEK_RE = re.compile(r"^(\d{4})W(\d{1,2})$")


# --------------------------------------------------------------------------- #
# Monthly self-gate (cron / Task Scheduler fire weekly; we self-gate inside)
# --------------------------------------------------------------------------- #


def is_first_sunday_of_month(today: dt.date | None = None) -> bool:
    """True iff ``today`` is the 1st Sunday of its calendar month.

    Used as a self-gate when the scheduler can only express "Weekly Sunday"
    (Windows Task Scheduler, vixie-cron with strict OR semantics on dom+dow).
    """
    today = today or dt.date.today()
    if today.isoweekday() != 7:
        return False
    return today.day <= 7


# --------------------------------------------------------------------------- #
# ISO week helpers (mirrors attribution_engine._iso_week_range pattern)
# --------------------------------------------------------------------------- #


def _iso_week_range(week: str) -> tuple[str, str]:
    """Map ``2026W18`` -> (``'2026-05-04 00:00:00'``, ``'2026-05-10 23:59:59'``).

    Returns SPACE-separated boundaries (not ISO 'T') because SQLite stores
    DATETIME values in space-separated form by default; SQLite's BETWEEN does
    raw string comparison and "2026-05-04 09:00:00" < "2026-05-04T00:00:00"
    when 'T' is used as the upper bound prefix (space 0x20 < 'T' 0x54).
    """
    m = _WEEK_RE.match(week)
    if not m:
        raise ValueError(f"invalid ISO week token: {week!r} (expected e.g. '2026W18')")
    year, w = int(m.group(1)), int(m.group(2))
    monday = dt.date.fromisocalendar(year, w, 1)
    sunday = monday + dt.timedelta(days=6)
    return (f"{monday.isoformat()} 00:00:00", f"{sunday.isoformat()} 23:59:59")


def _iso_week_label(d: dt.date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}W{week:02d}"


def _previous_iso_week(label: str) -> str:
    m = _WEEK_RE.match(label)
    if not m:
        raise ValueError(f"invalid ISO week token: {label!r}")
    year, w = int(m.group(1)), int(m.group(2))
    # Walk back 7 days from this week's Monday.
    monday = dt.date.fromisocalendar(year, w, 1)
    prev = monday - dt.timedelta(days=7)
    return _iso_week_label(prev)


def _recent_cohort_weeks(count: int, today: dt.date | None = None) -> list[str]:
    """Return ``count`` most recent ISO weeks (oldest first), ending at the
    week BEFORE today's week (so we always have at least a full week of data).

    Output is sorted ascending so the report matrix reads top-to-bottom in
    chronological order.
    """
    today = today or dt.date.today()
    # Start one week before "this" week so partial-week leakage doesn't pollute
    # the most recent row.
    cursor = _previous_iso_week(_iso_week_label(today))
    weeks: list[str] = [cursor]
    for _ in range(count - 1):
        cursor = _previous_iso_week(cursor)
        weeks.append(cursor)
    return list(reversed(weeks))


# --------------------------------------------------------------------------- #
# Cohort computation
# --------------------------------------------------------------------------- #


def _safe_div(num: int, den: int) -> float:
    return float(num) / float(den) if den > 0 else 0.0


def _parse_dt(value: Any) -> dt.datetime | None:
    """Best-effort ISO/SQLite timestamp -> datetime. Returns None if unparseable."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    # SQLite typically stores "YYYY-MM-DD HH:MM:SS"; ISO uses "T".
    # ``fromisoformat`` accepts both since Python 3.11.
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # Final fallback: date-only.
        try:
            return dt.datetime.fromisoformat(s[:10])
        except ValueError:
            logger.warning("cohort_analysis: unparseable timestamp %r", value)
            return None


def compute_cohort(week: str) -> dict[str, Any]:
    """Compute one cohort row for ``week``. Returns a dict matching the
    ``cohort_metrics`` schema."""
    start_iso, end_iso = _iso_week_range(week)
    rows = db.fetchall(
        """
        SELECT id, first_seen_at, qualified, sql_status, sql_verified_at
        FROM leads
        WHERE first_seen_at IS NOT NULL
          AND first_seen_at BETWEEN ? AND ?
        """,
        (start_iso, end_iso),
    )
    cohort_size = len(rows)
    d7_q = d14_q = d30_q = 0
    d7_s = d14_s = d30_s = 0

    for r in rows:
        first_seen = _parse_dt(r["first_seen_at"])
        if first_seen is None:
            continue
        qualified = bool(r["qualified"])
        sql_ts = _parse_dt(r["sql_verified_at"]) if r["sql_status"] == "verified" else None

        # Qualified milestone: we don't have a dedicated qualified_at column,
        # so as the closest proxy we treat ``sql_verified_at`` as the moment
        # the lead became fully qualified+verified (qualified is a prereq).
        # When qualified=1 but no sql_verified_at exists, attribute it at
        # ``last_seen_at`` if newer than first_seen_at, otherwise count at D7.
        # This keeps numerics honest without inventing data.
        qualified_ts: dt.datetime | None = None
        if qualified:
            qualified_ts = sql_ts or _parse_dt(rows_get(r, "last_seen_at"))
            if qualified_ts is None:
                # No timestamp anywhere — bucket it into D7 conservatively.
                qualified_ts = first_seen + dt.timedelta(days=7)

        if qualified_ts is not None:
            delta_days = (qualified_ts - first_seen).total_seconds() / 86400.0
            if delta_days <= 7:
                d7_q += 1
            if delta_days <= 14:
                d14_q += 1
            if delta_days <= 30:
                d30_q += 1

        if sql_ts is not None:
            delta_days = (sql_ts - first_seen).total_seconds() / 86400.0
            if delta_days <= 7:
                d7_s += 1
            if delta_days <= 14:
                d14_s += 1
            if delta_days <= 30:
                d30_s += 1

    return {
        "cohort_week": week,
        "cohort_size": cohort_size,
        "d7_qualified": d7_q,
        "d14_qualified": d14_q,
        "d30_qualified": d30_q,
        "d7_sql": d7_s,
        "d14_sql": d14_s,
        "d30_sql": d30_s,
        "d7_conv_rate": _safe_div(d7_s, cohort_size),
        "d30_conv_rate": _safe_div(d30_s, cohort_size),
    }


def rows_get(row: Any, key: str) -> Any:
    """Safely read ``key`` from a ``sqlite3.Row`` — Row has no ``.get``."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def upsert_cohort(metrics: dict[str, Any]) -> None:
    """Idempotent insert-or-replace keyed on cohort_week."""
    db.execute(
        """
        INSERT INTO cohort_metrics (
            cohort_week, cohort_size,
            d7_qualified, d14_qualified, d30_qualified,
            d7_sql, d14_sql, d30_sql,
            d7_conv_rate, d30_conv_rate,
            computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(cohort_week) DO UPDATE SET
            cohort_size    = excluded.cohort_size,
            d7_qualified   = excluded.d7_qualified,
            d14_qualified  = excluded.d14_qualified,
            d30_qualified  = excluded.d30_qualified,
            d7_sql         = excluded.d7_sql,
            d14_sql        = excluded.d14_sql,
            d30_sql        = excluded.d30_sql,
            d7_conv_rate   = excluded.d7_conv_rate,
            d30_conv_rate  = excluded.d30_conv_rate,
            computed_at    = CURRENT_TIMESTAMP
        """,
        (
            metrics["cohort_week"],
            metrics["cohort_size"],
            metrics["d7_qualified"],
            metrics["d14_qualified"],
            metrics["d30_qualified"],
            metrics["d7_sql"],
            metrics["d14_sql"],
            metrics["d30_sql"],
            metrics["d7_conv_rate"],
            metrics["d30_conv_rate"],
        ),
    )


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #


def _format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_report(cohorts: list[dict[str, Any]]) -> str:
    """Render a matrix-style markdown table. Rows = cohorts, cols = D7/D14/D30."""
    if not cohorts:
        return "# Cohort Report\n\n_No cohorts in scope._\n"

    head = (
        "# Cohort Report\n\n"
        f"_Generated at {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}_\n\n"
        f"Cohorts analyzed: **{len(cohorts)}** "
        f"({cohorts[0]['cohort_week']} → {cohorts[-1]['cohort_week']})\n\n"
    )
    table = [
        "| Cohort | Size | D7 Q | D14 Q | D30 Q | D7 SQL | D14 SQL | D30 SQL | D7 Conv | D30 Conv |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in cohorts:
        table.append(
            "| {week} | {size} | {d7q} | {d14q} | {d30q} | {d7s} | {d14s} | {d30s} | {d7r} | {d30r} |".format(
                week=c["cohort_week"],
                size=c["cohort_size"],
                d7q=c["d7_qualified"],
                d14q=c["d14_qualified"],
                d30q=c["d30_qualified"],
                d7s=c["d7_sql"],
                d14s=c["d14_sql"],
                d30s=c["d30_sql"],
                d7r=_format_pct(c["d7_conv_rate"]),
                d30r=_format_pct(c["d30_conv_rate"]),
            )
        )
    body = "\n".join(table)
    return head + body + "\n"


def write_report(content: str, output_path: Path | None, latest_week: str) -> Path:
    target = output_path or (_RUNTIME_DIR / f"cohort_report_{latest_week}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("cohort_analysis: wrote report to %s (%d bytes)", target, len(content))
    return target


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(weeks: int = DEFAULT_COHORT_COUNT, output: Path | None = None) -> dict[str, Any]:
    """Compute the last ``weeks`` cohorts, UPSERT, and write the markdown matrix."""
    started_at = time.monotonic()
    cohort_weeks = _recent_cohort_weeks(weeks)
    logger.info("cohort_analysis: analyzing %d cohorts: %s", len(cohort_weeks), cohort_weeks)

    cohorts: list[dict[str, Any]] = []
    failures = 0
    for w in cohort_weeks:
        try:
            metrics = compute_cohort(w)
            upsert_cohort(metrics)
            cohorts.append(metrics)
        except Exception as exc:  # noqa: BLE001 - record + continue
            failures += 1
            logger.exception("cohort_analysis: cohort %s failed: %s", w, exc)
            db.publish_failures.insert(
                severity="P2",
                source=JOB_NAME,
                failure_type="cohort_compute_failed",
                failure_detail=f"week={w} err={exc}",
            )

    latest = cohort_weeks[-1] if cohort_weeks else _iso_week_label(dt.date.today())
    report = render_report(cohorts)
    try:
        target = write_report(report, output, latest)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        logger.exception("cohort_analysis: failed to write report: %s", exc)
        alert("P2", f"cohort_analysis report write failed: {exc}")
        target = None

    duration = int(time.monotonic() - started_at)
    status = "ok" if failures == 0 else ("warning" if cohorts else "failed")
    db.heartbeat.record(
        JOB_NAME,
        status,
        duration,
        rows_written=len(cohorts),
        error_message=f"{failures} failures" if failures else None,
    )
    logger.info(
        "cohort_analysis done: status=%s cohorts=%d failures=%d duration=%ds",
        status,
        len(cohorts),
        failures,
        duration,
    )
    return {
        "cohorts": len(cohorts),
        "failures": failures,
        "status": status,
        "duration_seconds": duration,
        "report_path": str(target) if target else None,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jobs.cohort_analysis",
        description="Compute D7/D14/D30 lead-cohort conversion metrics.",
    )
    p.add_argument(
        "--weeks",
        type=int,
        default=DEFAULT_COHORT_COUNT,
        help=f"Number of cohorts to analyze (default {DEFAULT_COHORT_COUNT}).",
    )
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Override path for the markdown report (default runtime/cohort_report_<week>.md).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Bypass the 1st-Sunday-of-month self-gate.",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    args = _build_arg_parser().parse_args()
    if not args.force and not is_first_sunday_of_month():
        logger.info("cohort_analysis: not the 1st Sunday of month; exiting (use --force to override)")
        return 0
    output_path = Path(args.output) if args.output else None
    try:
        summary = run(weeks=args.weeks, output=output_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cohort_analysis top-level failure: %s", exc)
        alert("P1", f"cohort_analysis crashed: {exc}")
        return 2
    logger.info("cohort_analysis summary: %s", summary)
    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
