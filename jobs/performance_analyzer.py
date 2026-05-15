"""Performance Analyzer · weekly Sun 18:30 cron (PRD §7 M2 V2).

Runs after weekly_reporter (18:00). For the week just closed, picks the Top1
piece (most 7d impressions) and the Bottom1 piece (fewest 7d impressions among
publications that cleared ``--min-impressions``), packages their selection-card
metadata + 24h/7d metrics + leads attribution rows, and asks the LLM for
three concrete hypotheses each plus two actionable levers for next week.

CLI::

    python -m jobs.performance_analyzer
    python -m jobs.performance_analyzer --week 2026W19
    python -m jobs.performance_analyzer --week 2026W19 --min-impressions 500
    python -m jobs.performance_analyzer --output runtime/custom.md
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

# Allow running as `python jobs/performance_analyzer.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from lib.llm_client import LLMClientError, llm  # noqa: E402

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR") or (_ENGINE_ROOT / "runtime"))
_PROMPT_PATH = _ENGINE_ROOT / "config" / "prompts" / "performance_analyzer.txt"

_JOB_NAME = "performance_analyzer"
_DEFAULT_MIN_IMPRESSIONS = 500


# --------------------------------------------------------------------------- #
# Week helpers (shared shape with weekly_reporter)
# --------------------------------------------------------------------------- #


def _current_iso_week(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    year, week, _ = today.isocalendar()
    return f"{year}W{week:02d}"


def _week_to_date_range(week: str) -> tuple[str, str]:
    if "W" not in week or len(week) < 6:
        raise ValueError(f"week must be YYYYWWW (e.g. 2026W19); got {week!r}")
    year_s, _, week_s = week.partition("W")
    try:
        year = int(year_s)
        weeknum = int(week_s)
    except ValueError as exc:
        raise ValueError(f"week parse failed: {week!r}") from exc
    monday = dt.date.fromisocalendar(year, weeknum, 1)
    sunday = monday + dt.timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


# --------------------------------------------------------------------------- #
# Data assembly
# --------------------------------------------------------------------------- #


def _load_card(piece_id: str) -> dict[str, Any]:
    """Return the parsed selection card for ``piece_id`` (empty dict if unknown)."""
    piece = db.pieces.get(piece_id)
    if piece is None:
        return {}
    try:
        return piece.card() or {}
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to parse selection card for piece_id=%s", piece_id)
        return {}


def _metrics_for_publishings(
    publishing_ids: list[int], snapshot_type: str
) -> list[dict[str, Any]]:
    """Pull metrics_daily rows of ``snapshot_type`` for the given publishings."""
    if not publishing_ids:
        return []
    placeholders = ",".join(["?"] * len(publishing_ids))
    rows = db.fetchall(
        f"""
        SELECT id, publishing_id, snapshot_type, snapshot_at, impressions, likes,
               replies, quotes, retweets, shares, bookmarks, profile_clicks,
               link_clicks
        FROM metrics_daily
        WHERE publishing_id IN ({placeholders})
          AND snapshot_type = ?
        ORDER BY snapshot_at DESC
        """,
        (*publishing_ids, snapshot_type),
    )
    return [dict(r) for r in rows]


def _leads_for_piece(piece_id: str) -> int:
    """Count leads attributed to this piece across all three models."""
    row = db.fetchone(
        """
        SELECT COUNT(DISTINCT l.id) AS n
        FROM leads l
        LEFT JOIN user_journey uj
               ON uj.user_id = l.email_hash AND uj.piece_id = ? AND uj.action = 'signup'
        WHERE uj.id IS NOT NULL
           OR l.first_touch_piece_id = ?
           OR l.bd_attribution_content_id = ?
           OR (
               l.linear_weights_json IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM json_each(l.linear_weights_json) je WHERE je.key = ?
               )
           )
        """,
        (piece_id, piece_id, piece_id, piece_id),
    )
    return int(row["n"]) if row and row["n"] is not None else 0


def _aggregates_for_piece(piece_id: str, week: str) -> list[dict[str, Any]]:
    """Find weekly_aggregates rows whose ``value`` matches one of the piece's
    selection-card dimensions (hook_type / narrative_anchor / platform).
    """
    card = _load_card(piece_id)
    hook = card.get("hook_type")
    anchor = card.get("narrative_anchor")
    platform_row = db.fetchone(
        "SELECT platform FROM publishings WHERE piece_id = ? LIMIT 1",
        (piece_id,),
    )
    platform = platform_row["platform"] if platform_row else None

    candidates = [
        ("hook_type", hook),
        ("narrative_anchor", anchor),
        ("format", platform),
    ]
    out: list[dict[str, Any]] = []
    for dim, val in candidates:
        if not val:
            continue
        rows = db.fetchall(
            """
            SELECT * FROM weekly_aggregates
            WHERE week = ? AND dimension = ? AND value = ?
            ORDER BY attribution_model
            """,
            (week, dim, str(val)),
        )
        out.extend(dict(r) for r in rows)
    return out


def _publishings_for_piece(piece_id: str) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT * FROM publishings WHERE piece_id = ? ORDER BY published_at",
        (piece_id,),
    )
    return [dict(r) for r in rows]


def _rank_by_impressions(
    week: str, min_impressions: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pick (top1, bottom1) piece summaries based on 7d impressions.

    A piece's score = SUM(m.impressions) across its publishings published in
    the week and joined to the 7d snapshot. Only pieces whose score >=
    ``min_impressions`` qualify as Bottom1; Top1 has no floor.

    Returns ``(None, None)`` if no eligible piece exists this week.
    """
    week_start, week_end = _week_to_date_range(week)
    rows = db.fetchall(
        """
        SELECT pu.piece_id AS piece_id,
               COALESCE(SUM(m.impressions), 0) AS impressions
        FROM publishings pu
        LEFT JOIN metrics_daily m
               ON m.publishing_id = pu.id AND m.snapshot_type = '7d'
        WHERE date(pu.published_at) BETWEEN ? AND ?
        GROUP BY pu.piece_id
        HAVING pu.piece_id IS NOT NULL
        ORDER BY impressions DESC
        """,
        (week_start, week_end),
    )
    ranked = [dict(r) for r in rows]
    if not ranked:
        return None, None
    top = ranked[0] if ranked[0]["impressions"] > 0 else None
    bottom_candidates = [r for r in ranked if r["impressions"] >= min_impressions]
    if not bottom_candidates:
        bottom = None
    else:
        bottom = bottom_candidates[-1]
        # If only one piece met the floor and it equals top, no separate bottom.
        if top is not None and bottom["piece_id"] == top["piece_id"]:
            bottom = None
    return top, bottom


def _build_piece_payload(piece_id: str, week: str) -> dict[str, Any]:
    """Assemble the LLM-input dict describing a single piece's performance."""
    card = _load_card(piece_id)
    pubs = _publishings_for_piece(piece_id)
    pub_ids = [int(p["id"]) for p in pubs]
    return {
        "piece_id": piece_id,
        "title": card.get("title") or card.get("title_hypothesis"),
        "hook_type": card.get("hook_type"),
        "narrative_anchor": card.get("narrative_anchor"),
        "target_persona": card.get("target_persona"),
        "platform": pubs[0]["platform"] if pubs else None,
        "publishings": pubs,
        "metrics_24h": _metrics_for_publishings(pub_ids, "24h"),
        "metrics_7d": _metrics_for_publishings(pub_ids, "7d"),
        "leads_attributed": _leads_for_piece(piece_id),
        "aggregates_rows": _aggregates_for_piece(piece_id, week),
    }


def build_payload(week: str, min_impressions: int) -> dict[str, Any]:
    """Return the JSON payload handed to the LLM (or ``{}`` if nothing qualifies)."""
    top, bottom = _rank_by_impressions(week, min_impressions)
    if top is None:
        return {}
    payload: dict[str, Any] = {
        "week": week,
        "min_impressions": min_impressions,
        "top1": _build_piece_payload(str(top["piece_id"]), week),
        "bottom1": (
            _build_piece_payload(str(bottom["piece_id"]), week) if bottom else None
        ),
    }
    return payload


# --------------------------------------------------------------------------- #
# LLM call
# --------------------------------------------------------------------------- #


def _load_prompt() -> str:
    if not _PROMPT_PATH.is_file():
        raise FileNotFoundError(
            f"performance_analyzer prompt missing at {_PROMPT_PATH}"
        )
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _call_llm(week: str, payload: dict[str, Any], max_tokens: int = 4000) -> str:
    system = _load_prompt().replace("{week}", week)
    user = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    logger.info(
        "performance_analyzer LLM call (week=%s, payload_chars=%d)", week, len(user)
    )
    return llm.complete(system=system, user=user, max_tokens=max_tokens)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def run(
    week: str | None = None,
    min_impressions: int = _DEFAULT_MIN_IMPRESSIONS,
    output_path: Path | None = None,
) -> Path | None:
    """Generate the weekly performance-analysis Markdown.

    Returns the path of the written file, or ``None`` if no piece qualified
    (heartbeat is still recorded). Raises only on LLM/I/O hard failures.
    """
    started = time.monotonic()
    week = week or _current_iso_week()
    logger.info(
        "performance_analyzer starting (week=%s, min_impressions=%d)",
        week,
        min_impressions,
    )

    rows_written = 0
    status: str = "ok"
    error_message: str | None = None
    out_path: Path | None = None

    try:
        payload = build_payload(week=week, min_impressions=min_impressions)
        if not payload:
            logger.warning(
                "performance_analyzer: no piece met min_impressions=%d for week=%s; skipping",
                min_impressions,
                week,
            )
            error_message = (
                f"no eligible piece (min_impressions={min_impressions}) for {week}"
            )
            return None

        report_md = _call_llm(week, payload)

        if output_path is None:
            output_path = _RUNTIME_DIR / f"performance_analysis_{week}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_md, encoding="utf-8")
        out_path = output_path
        rows_written = 1
        logger.info(
            "performance_analyzer written: %s (%d chars)", output_path, len(report_md)
        )
    except LLMClientError as exc:
        status = "failed"
        error_message = f"LLM error: {exc}"
        logger.exception("performance_analyzer LLM error")
        raise
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
        logger.exception("performance_analyzer failed")
        try:
            alert(
                "P1",
                f"performance_analyzer failed for {week}",
                {"error": str(exc)[:300]},
            )
        except Exception:
            logger.exception(
                "could not emit P1 alert for performance_analyzer failure"
            )
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
            logger.exception("failed to write heartbeat row for performance_analyzer")

    return out_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m jobs.performance_analyzer",
        description="TaskOn Performance Analyzer (PRD §7 M2 V2)",
    )
    p.add_argument(
        "--week",
        default=None,
        help="ISO week label, e.g. 2026W19 (default: current ISO week).",
    )
    p.add_argument(
        "--min-impressions",
        type=int,
        default=_DEFAULT_MIN_IMPRESSIONS,
        help=(
            "Bottom1 floor: a piece must reach this many 7d impressions to be a "
            f"candidate (default: {_DEFAULT_MIN_IMPRESSIONS})."
        ),
    )
    p.add_argument("--output", default=None, help="Override output markdown path.")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    try:
        out = run(
            week=args.week,
            min_impressions=args.min_impressions,
            output_path=Path(args.output) if args.output else None,
        )
    except Exception:
        logger.exception("performance_analyzer exited with error")
        return 1
    if out is None:
        logger.info("performance_analyzer skipped — no qualifying piece this week")
        return 0
    logger.info("performance_analyzer OK · %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
