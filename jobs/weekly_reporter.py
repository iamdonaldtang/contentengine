"""Weekly Reporter Wrapper · PRD §2.8.

Sunday 18:00 cron entrypoint. Pulls last week's metrics from SQLite, calls
``jobs.attribution_engine.compute_weekly_aggregates(week)`` to refresh the
5-dim aggregates, hands the resulting JSON to the LLM, and writes
``runtime/weekly_report_<YYYYWWW>.md``.

Design (Prompt_AI系统化编程_v1.md §7):
  * No silent failures; every branch logs or raises.
  * No hard-coded paths — runtime root from ``$RUNTIME_DIR`` / fallback.
  * No ``print``; ``logging`` everywhere.
  * SQLite access ONLY via ``lib.db.db`` singleton.
  * Heartbeat row appended on success / failure.

CLI::

    python -m jobs.weekly_reporter
    python -m jobs.weekly_reporter --week 2026W19
    python -m jobs.weekly_reporter --week 2026W19 --output runtime/custom.md
"""
from __future__ import annotations

import argparse
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

# Allow running as `python jobs/weekly_reporter.py` from repo root too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from lib.llm_client import LLMClientError, llm  # noqa: E402

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR") or (_ENGINE_ROOT / "runtime"))
_PROMPT_PATH = _ENGINE_ROOT / "config" / "prompts" / "weekly_reporter.txt"

_JOB_NAME = "weekly_reporter"


# --------------------------------------------------------------------------- #
# Week-string helpers
# --------------------------------------------------------------------------- #


def _current_iso_week(today: dt.date | None = None) -> str:
    """Return the ISO-week label (e.g. ``2026W19``) of *last* completed week.

    The Sunday 18:00 cron is intended to summarise the week that just ended,
    not the calendar week containing today. So we step back 1 day to land
    inside Saturday — which is the last day of the *previous* ISO week (ISO
    weeks are Monday-Sunday; Sunday IS the last day of the same ISO week, so
    stepping back 1 day from Sunday lands on Saturday in the same week, which
    is fine — we want to report on the week that contains the current Sunday).

    More carefully: ISO week 19 of 2026 runs Mon May 4 – Sun May 10. A cron at
    Sun May 10 18:00 should summarise W19. We use today's ISO week directly.
    """
    today = today or dt.date.today()
    year, week, _ = today.isocalendar()
    return f"{year}W{week:02d}"


def _week_to_date_range(week: str) -> tuple[str, str]:
    """Return ``(start_iso_date, end_iso_date)`` (inclusive) for ``YYYYWWW``.

    Raises ``ValueError`` if the format does not match.
    """
    if "W" not in week or len(week) < 6:
        raise ValueError(f"week must be YYYYWWW (e.g. 2026W19); got {week!r}")
    year_s, _, week_s = week.partition("W")
    try:
        year = int(year_s)
        weeknum = int(week_s)
    except ValueError as exc:
        raise ValueError(f"week parse failed: {week!r}") from exc
    # ISO week: Monday of week N.
    monday = dt.date.fromisocalendar(year, weeknum, 1)
    sunday = monday + dt.timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #


def _strip_raw_json(row: dict[str, Any]) -> dict[str, Any]:
    """Drop large ``raw_json`` / ``raw_data`` payloads to save tokens.

    Keep the row otherwise intact — callers reason about it as a dict.
    """
    out = dict(row)
    for k in ("raw_json", "raw_data"):
        if k in out:
            out.pop(k, None)
    return out


def collect_weekly_data(week: str) -> dict[str, Any]:
    """Pull all rows touching ``[monday, sunday]`` for ``week``.

    Returns a JSON-serialisable dict. ``raw_json`` blobs are stripped so the
    LLM prompt stays token-bounded.
    """
    start_date, end_date = _week_to_date_range(week)
    logger.info("collecting weekly data for %s (%s..%s)", week, start_date, end_date)

    # metrics_daily rows whose fetched_at is in [start, end+1).
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
        """
        SELECT * FROM newsletter_campaigns
        WHERE date(send_time) BETWEEN ? AND ?
        ORDER BY send_time
        """,
        (start_date, end_date),
    )
    newsletters: list[dict[str, Any]] = [_strip_raw_json(dict(r)) for r in newsletter_rows]

    btouch_rows = db.fetchall(
        "SELECT * FROM btouch_daily WHERE snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date, touchpoint_id",
        (start_date, end_date),
    )
    btouch: list[dict[str, Any]] = [_strip_raw_json(dict(r)) for r in btouch_rows]

    aggregates_rows = db.fetchall(
        "SELECT * FROM weekly_aggregates WHERE week = ? ORDER BY dimension, value",
        (week,),
    )
    aggregates: list[dict[str, Any]] = [dict(r) for r in aggregates_rows]

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
        "week": week,
        "date_range": {"start": start_date, "end": end_date},
        "totals": {
            "metrics_rows": len(metrics),
            "landing_rows": len(landing),
            "newsletter_campaigns": len(newsletters),
            "btouch_rows": len(btouch),
            "aggregates_rows": len(aggregates),
            "publish_failures": len(failures),
            "new_leads": len(leads),
        },
        "metrics_daily": metrics,
        "landing_metrics": landing,
        "newsletter_campaigns": newsletters,
        "btouch_daily": btouch,
        "weekly_aggregates": aggregates,
        "publish_failures": failures,
        "leads": leads,
    }
    logger.info(
        "weekly data collected: metrics=%d landing=%d newsletters=%d btouch=%d aggregates=%d failures=%d leads=%d",
        len(metrics),
        len(landing),
        len(newsletters),
        len(btouch),
        len(aggregates),
        len(failures),
        len(leads),
    )
    return payload


# --------------------------------------------------------------------------- #
# Aggregate refresh + LLM call
# --------------------------------------------------------------------------- #


def _refresh_aggregates(week: str) -> None:
    """Best-effort refresh of weekly_aggregates via attribution_engine.

    The aggregator is built by another subagent; we import lazily so this
    module remains testable in isolation. On import or call failure we log
    WARNING and proceed with whatever rows are already in the table — the
    Reporter must not block on a broken aggregator.
    """
    try:
        from jobs.attribution_engine import compute_weekly_aggregates  # type: ignore
    except ImportError as exc:
        logger.warning(
            "attribution_engine.compute_weekly_aggregates unavailable (%s); "
            "using existing weekly_aggregates rows as-is",
            exc,
        )
        return
    try:
        compute_weekly_aggregates(week)
        logger.info("weekly_aggregates refreshed for %s", week)
    except Exception:
        # Aggregator failure is non-fatal: degrade to whatever's in the table.
        logger.exception("compute_weekly_aggregates(%s) failed; continuing", week)


def _load_prompt() -> str:
    if not _PROMPT_PATH.is_file():
        raise FileNotFoundError(f"weekly_reporter prompt missing at {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _call_llm(week: str, data: dict[str, Any], max_tokens: int = 8000) -> str:
    system = _load_prompt().replace("{week}", week)
    user = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    logger.info("calling LLM for weekly report (week=%s, prompt_chars=%d)", week, len(user))
    return llm.complete(system=system, user=user, max_tokens=max_tokens)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def render_bare_report(week: str, data: dict[str, Any]) -> str:
    """Render a structured plain-data markdown when LLM is unavailable.

    Includes:
      * Header with the week label and a "bare data — LLM unavailable" banner
      * Per-section row counts (audit trail)
      * Markdown tables for the most-used sections (metrics_daily,
        weekly_aggregates, leads, publish_failures)
      * Truncated raw JSON dump of the rest for forensic completeness

    Output is deliberately bare so Donald knows at a glance to either
    re-trigger after the LLM recovers OR write the prose section by hand.
    The filename suffix ``_bare`` is set by ``run()`` to make this obvious
    in the runtime/ listing.
    """
    lines: list[str] = []
    lines.append(f"# Weekly Report · {week} · ⚠️ LLM 不可达，bare 数据回退版")
    lines.append("")
    lines.append(
        "> 本周报由 `weekly_reporter` 在 LLM 全失败 fallback 路径下生成。"
        "兼职女生 / Donald 周一上午可基于此手写一版叙事。"
    )
    lines.append("")
    lines.append(f"**日期范围**: {data.get('date_range', {}).get('start')} → "
                 f"{data.get('date_range', {}).get('end')}")
    lines.append("")

    totals = data.get("totals") or {}
    if totals:
        lines.append("## 计数总览")
        lines.append("")
        lines.append("| section | rows |")
        lines.append("| --- | ---: |")
        for k, v in totals.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    # Aggregates table — the highest-signal section.
    aggs = data.get("weekly_aggregates") or []
    if aggs:
        lines.append("## 5 维归因汇总 (weekly_aggregates)")
        lines.append("")
        cols = ["attribution_model", "dimension", "value", "publish_count",
                "avg_impressions", "avg_engagement_rate", "avg_click_through_rate",
                "leads_attributed", "sql_attributed", "weight_for_next_week"]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("| " + " | ".join("---" for _ in cols) + " |")
        for r in aggs[:200]:  # cap to keep the file readable
            row_vals = [str(r.get(c)) if r.get(c) is not None else "" for c in cols]
            lines.append("| " + " | ".join(row_vals) + " |")
        if len(aggs) > 200:
            lines.append(f"| ... (truncated; {len(aggs) - 200} more rows) |" + " |" * (len(cols) - 1))
        lines.append("")

    # New leads — small set, very signal-rich.
    leads = data.get("leads") or []
    if leads:
        lines.append(f"## 新增 leads ({len(leads)} 条)")
        lines.append("")
        cols = ["id", "first_seen_at", "first_landing_page", "first_utm_campaign",
                "first_utm_content", "first_utm_term", "qualified", "sql_status"]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("| " + " | ".join("---" for _ in cols) + " |")
        for r in leads[:50]:
            row_vals = [str(r.get(c)) if r.get(c) is not None else "" for c in cols]
            lines.append("| " + " | ".join(row_vals) + " |")
        if len(leads) > 50:
            lines.append(f"| ... (truncated; {len(leads) - 50} more leads) |" + " |" * (len(cols) - 1))
        lines.append("")

    # Failures — important for ops triage.
    failures = data.get("publish_failures") or []
    if failures:
        lines.append(f"## publish_failures ({len(failures)} 条)")
        lines.append("")
        cols = ["occurred_at", "severity", "source", "failure_type", "failure_detail"]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("| " + " | ".join("---" for _ in cols) + " |")
        for r in failures[:100]:
            detail = (r.get("failure_detail") or "")[:120].replace("|", "\\|")
            vals = [str(r.get("occurred_at") or ""), str(r.get("severity") or ""),
                    str(r.get("source") or ""), str(r.get("failure_type") or ""), detail]
            lines.append("| " + " | ".join(vals) + " |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 原始数据 JSON (截断版)")
    lines.append("")
    lines.append("```json")
    # Short-form dump: keep counts but drop the bulk arrays we already
    # tabulated above to keep the file under ~100 KB.
    short = dict(data)
    for k in ("metrics_daily", "weekly_aggregates", "leads", "publish_failures",
              "landing_metrics", "newsletter_campaigns", "btouch_daily"):
        if k in short:
            arr = short[k] or []
            short[k] = f"<{len(arr)} rows — see table above or query state.db>"
    lines.append(json.dumps(short, indent=2, ensure_ascii=False, default=str))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def run(week: str | None = None, output_path: Path | None = None) -> Path:
    """End-to-end weekly report generation.

    Returns the path of the written markdown file. Records a heartbeat row
    regardless of success.

    Fallback semantics (T7 · docs/architecture.md §7 TODO):
        When the LLM call raises ``LLMClientError``, the reporter does NOT
        propagate the failure. Instead it writes a "bare data" markdown
        report — same metrics tables, no narrative — to
        ``runtime/weekly_report_<week>_bare.md`` and fires a P1 Lark alert
        (with the LLM error nested in ``details``) so the on-call sees
        both "LLM is broken" AND "report still produced". The job's
        heartbeat status is ``warning``, exit code 0 — Donald gets a
        review-ready artifact even on LLM outage.

    Non-LLM failures (DB unreachable, prompt missing, etc.) still raise.
    """
    started = time.monotonic()
    week = week or _current_iso_week()
    logger.info("weekly_reporter starting (week=%s)", week)

    rows_written = 0
    status: str = "ok"
    error_message: str | None = None
    out_path: Path | None = None

    try:
        _refresh_aggregates(week)
        data = collect_weekly_data(week)

        try:
            report_md = _call_llm(week, data)
            if output_path is None:
                output_path = _RUNTIME_DIR / f"weekly_report_{week}.md"
        except LLMClientError as llm_exc:
            # T7 fallback: produce a bare-data report instead of failing.
            logger.exception("weekly_reporter LLM unavailable; falling back to bare data")
            status = "warning"
            error_message = f"LLM unavailable: {llm_exc}"
            report_md = render_bare_report(week, data)
            if output_path is None:
                output_path = _RUNTIME_DIR / f"weekly_report_{week}_bare.md"
            try:
                alert(
                    "P1",
                    f"weekly_reporter LLM unavailable → bare report for {week}",
                    {
                        "error": str(llm_exc)[:300],
                        "fallback_path": str(output_path),
                    },
                )
            except Exception:
                logger.exception("failed to emit P1 alert for LLM fallback")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_md, encoding="utf-8")
        out_path = output_path
        rows_written = 1
        logger.info("weekly report written: %s (%d chars, status=%s)",
                    output_path, len(report_md), status)
    except Exception as exc:
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
        logger.exception("weekly_reporter failed")
        try:
            alert("P1", f"weekly_reporter failed for {week}", {"error": str(exc)[:300]})
        except Exception:
            logger.exception("could not emit P1 alert for weekly_reporter failure")
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
            logger.exception("failed to write heartbeat row for weekly_reporter")

    assert out_path is not None  # for type checker
    return out_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn Weekly Reporter (PRD §2.8)")
    p.add_argument("--week", default=None, help="ISO week label, e.g. 2026W19 (default: current)")
    p.add_argument("--output", default=None, help="override output markdown path")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        out = run(
            week=args.week,
            output_path=Path(args.output) if args.output else None,
        )
    except Exception:
        logger.exception("weekly_reporter exited with error")
        return 1
    logger.info("weekly_reporter OK · %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
