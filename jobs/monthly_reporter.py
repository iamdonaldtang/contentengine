"""Monthly Reporter Wrapper · PRD §2.8 (last Sunday of month).

End-of-month complement to ``jobs.weekly_reporter``. Pulls the last 4
``weekly_aggregates`` rows plus monthly raw rollups, calls the LLM with the
``monthly_reporter.txt`` system prompt, writes ``runtime/monthly_report_<YYYY-MM>.md``.

Cron pattern (cf. ``config.yaml``)::

    0 19 28-31 * *    python -m jobs.monthly_reporter

The cron fires on days 28-31; the script self-gates on ``is_last_sunday_of_month()``
and exits 0 (INFO) on other days unless ``--force`` is supplied.

CLI::

    python -m jobs.monthly_reporter
    python -m jobs.monthly_reporter --month 2026-05
    python -m jobs.monthly_reporter --month 2026-05 --output runtime/m.md
    python -m jobs.monthly_reporter --force
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from lib.llm_client import LLMClientError, llm  # noqa: E402

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR") or (_ENGINE_ROOT / "runtime"))
_PROMPT_PATH = _ENGINE_ROOT / "config" / "prompts" / "monthly_reporter.txt"

_JOB_NAME = "monthly_reporter"


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #


def is_last_sunday_of_month(today: dt.date | None = None) -> bool:
    """True if ``today`` is the last Sunday of its calendar month.

    ISO weekday for Sunday is 7. The "last Sunday" is the latest Sunday ≤
    last day of month — equivalently, today is Sunday AND today + 7 days
    crosses into the next month.
    """
    today = today or dt.date.today()
    if today.isoweekday() != 7:
        return False
    return (today + dt.timedelta(days=7)).month != today.month


def _current_month(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _parse_month(month: str) -> tuple[int, int]:
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"month must be YYYY-MM (e.g. 2026-05); got {month!r}")
    try:
        return int(month[:4]), int(month[5:])
    except ValueError as exc:
        raise ValueError(f"month parse failed: {month!r}") from exc


def _month_date_range(month: str) -> tuple[str, str]:
    year, mon = _parse_month(month)
    first = dt.date(year, mon, 1)
    last = dt.date(year, mon, calendar.monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat()


def _weeks_in_month(month: str) -> list[str]:
    """Return up to 5 ISO-week labels intersecting the calendar month.

    A week is counted if any day of it falls inside the month.
    """
    start_s, end_s = _month_date_range(month)
    start = dt.date.fromisoformat(start_s)
    end = dt.date.fromisoformat(end_s)
    weeks: list[str] = []
    seen: set[str] = set()
    cur = start
    while cur <= end:
        year, week, _ = cur.isocalendar()
        label = f"{year}W{week:02d}"
        if label not in seen:
            seen.add(label)
            weeks.append(label)
        cur += dt.timedelta(days=1)
    return weeks


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #


def _strip_raw_json(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for k in ("raw_json", "raw_data"):
        if k in out:
            out.pop(k, None)
    return out


def collect_monthly_data(month: str) -> dict[str, Any]:
    """Pull last-4-weeks weekly_aggregates + monthly raw rollups.

    Returns a JSON-serialisable payload. ``raw_json`` blobs stripped.
    """
    start_date, end_date = _month_date_range(month)
    weeks = _weeks_in_month(month)
    logger.info("collecting monthly data for %s (%s..%s, %d weeks)", month, start_date, end_date, len(weeks))

    # last 4 weeks_aggregates rows in time order (could be 4 or 5 depending on month).
    if weeks:
        placeholders = ",".join("?" * len(weeks))
        aggregates_rows = db.fetchall(
            f"SELECT * FROM weekly_aggregates WHERE week IN ({placeholders}) ORDER BY week, dimension, value",
            tuple(weeks),
        )
    else:
        aggregates_rows = []
    aggregates: list[dict[str, Any]] = [dict(r) for r in aggregates_rows]

    metrics_rows = db.fetchall(
        """
        SELECT m.*, p.piece_id, p.platform, p.utm_campaign, p.utm_content, p.utm_term
        FROM metrics_daily m
        LEFT JOIN publishings p ON p.id = m.publishing_id
        WHERE date(m.fetched_at) BETWEEN ? AND ?
        ORDER BY m.fetched_at
        """,
        (start_date, end_date),
    )
    metrics: list[dict[str, Any]] = [_strip_raw_json(dict(r)) for r in metrics_rows]

    landing_rows = db.fetchall(
        "SELECT * FROM landing_metrics WHERE snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date",
        (start_date, end_date),
    )
    landing: list[dict[str, Any]] = [_strip_raw_json(dict(r)) for r in landing_rows]

    newsletter_rows = db.fetchall(
        "SELECT * FROM newsletter_campaigns WHERE date(send_time) BETWEEN ? AND ? ORDER BY send_time",
        (start_date, end_date),
    )
    newsletters: list[dict[str, Any]] = [_strip_raw_json(dict(r)) for r in newsletter_rows]

    btouch_rows = db.fetchall(
        "SELECT * FROM btouch_daily WHERE snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date, touchpoint_id",
        (start_date, end_date),
    )
    btouch: list[dict[str, Any]] = [_strip_raw_json(dict(r)) for r in btouch_rows]

    publish_failures_rows = db.fetchall(
        "SELECT * FROM publish_failures WHERE date(occurred_at) BETWEEN ? AND ? ORDER BY occurred_at",
        (start_date, end_date),
    )
    failures: list[dict[str, Any]] = [dict(r) for r in publish_failures_rows]

    leads_rows = db.fetchall(
        """
        SELECT id, email_hash, first_seen_at, first_landing_page,
               first_utm_campaign, first_utm_content, first_utm_term,
               qualified, sql_status, bd_attribution_content_id
        FROM leads
        WHERE date(first_seen_at) BETWEEN ? AND ?
        ORDER BY first_seen_at
        """,
        (start_date, end_date),
    )
    leads: list[dict[str, Any]] = [dict(r) for r in leads_rows]

    payload = {
        "month": month,
        "date_range": {"start": start_date, "end": end_date},
        "iso_weeks": weeks,
        "totals": {
            "weekly_aggregates_rows": len(aggregates),
            "metrics_rows": len(metrics),
            "landing_rows": len(landing),
            "newsletter_campaigns": len(newsletters),
            "btouch_rows": len(btouch),
            "publish_failures": len(failures),
            "new_leads": len(leads),
        },
        "weekly_aggregates": aggregates,
        "metrics_daily": metrics,
        "landing_metrics": landing,
        "newsletter_campaigns": newsletters,
        "btouch_daily": btouch,
        "publish_failures": failures,
        "leads": leads,
    }
    logger.info(
        "monthly data collected: aggregates=%d metrics=%d landing=%d newsletters=%d btouch=%d failures=%d leads=%d",
        len(aggregates),
        len(metrics),
        len(landing),
        len(newsletters),
        len(btouch),
        len(failures),
        len(leads),
    )
    return payload


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #


def _load_prompt() -> str:
    if not _PROMPT_PATH.is_file():
        raise FileNotFoundError(f"monthly_reporter prompt missing at {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _call_llm(month: str, data: dict[str, Any], max_tokens: int = 10000) -> str:
    system = _load_prompt().replace("{month}", month)
    user = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    logger.info("calling LLM for monthly report (month=%s, prompt_chars=%d)", month, len(user))
    return llm.complete(system=system, user=user, max_tokens=max_tokens)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def run(month: str | None = None, output_path: Path | None = None) -> Path:
    """End-to-end monthly report generation."""
    started = time.monotonic()
    month = month or _current_month()
    logger.info("monthly_reporter starting (month=%s)", month)

    rows_written = 0
    status: str = "ok"
    error_message: str | None = None
    out_path: Path | None = None

    try:
        data = collect_monthly_data(month)
        report_md = _call_llm(month, data)

        if output_path is None:
            output_path = _RUNTIME_DIR / f"monthly_report_{month}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_md, encoding="utf-8")
        out_path = output_path
        rows_written = 1
        logger.info("monthly report written: %s (%d chars)", output_path, len(report_md))
    except LLMClientError as exc:
        status = "failed"
        error_message = f"LLM error: {exc}"
        logger.exception("monthly_reporter LLM error")
        raise
    except Exception as exc:
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
        logger.exception("monthly_reporter failed")
        try:
            alert("P1", f"monthly_reporter failed for {month}", {"error": str(exc)[:300]})
        except Exception:
            logger.exception("could not emit P1 alert for monthly_reporter failure")
        raise
    finally:
        duration = int(time.monotonic() - started)
        try:
            db.heartbeat.record(
                job_name=_JOB_NAME,
                status=status,
                duration_seconds=duration,
                rows_written=rows_written,
                error_message=error_message,
            )
        except Exception:
            logger.exception("failed to write heartbeat row for monthly_reporter")

    assert out_path is not None
    return out_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn Monthly Reporter (PRD §2.8)")
    p.add_argument("--month", default=None, help="YYYY-MM (default: current month)")
    p.add_argument("--output", default=None, help="override output markdown path")
    p.add_argument(
        "--force",
        action="store_true",
        help="run even if today is not the last Sunday of the month",
    )
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    if not args.force and not is_last_sunday_of_month():
        logger.info(
            "today (%s) is not the last Sunday of the month; skipping (use --force to override)",
            dt.date.today().isoformat(),
        )
        return 0

    try:
        out = run(
            month=args.month,
            output_path=Path(args.output) if args.output else None,
        )
    except Exception:
        logger.exception("monthly_reporter exited with error")
        return 1
    logger.info("monthly_reporter OK · %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
