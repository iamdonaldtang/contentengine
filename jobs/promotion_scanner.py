"""Promotion Scanner -- two-tier "bet on proven winners" data layer (2026-06-30).

WHY THIS EXISTS
---------------
Tier-1 content (text + no-GPU visuals/video) is cheap and gets published broadly
to act as a "content futures market". Tier-2 (digital-human / long video) is GPU-
expensive, so we only ever spend it on pieces the cheap tier has already proven.

This job is the *measurement + flag* half of that loop. It does NOT render and it
does NOT flip the GPU ``avatar``/``longform`` stages on -- that is a later (phase 2,
cloud-GPU) action gated on a human content review. Here we only:

  1. Find pieces published >= ``--days`` ago that have engagement metrics.
  2. Aggregate per-piece engagement across all platforms (from ``metrics_daily``).
  3. Rank the cohort by engagement rate; flag the top ``--top-pct`` as winners.
  4. Write a machine-owned sidecar ``runtime/drafts/<piece>/promotion.json``
     with the score + ``promote`` boolean.

DESIGN CHOICE -- sidecar, not the human card
--------------------------------------------
We deliberately write a separate ``promotion.json`` rather than mutating the
author-written ``selection_card.yaml`` (which carries hand comments a yaml dump
would destroy). Downstream phase-2 tooling reads ``promotion.json``; the human
card stays pristine.

Threshold: engagement-rate percentile, default top 20% (``--top-pct 0.20``),
deliberately conservative -- tune after a few weeks of real data. Pieces below
``--min-impressions`` are excluded from ranking (too little exposure to judge).

Hard rules (Prompt_AI系统化编程_v1.md §7): no silent failures; heartbeat always
recorded; flag-only -- never triggers a paid render.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402

logger = logging.getLogger(__name__)

JOB_NAME = "promotion_scanner"

DEFAULT_DAYS = 7
DEFAULT_TOP_PCT = 0.20          # flag the top 20% by engagement rate
DEFAULT_MIN_IMPRESSIONS = 300   # below this, too little exposure to rank fairly

# Engagement = sum of these interaction columns from metrics_daily.
_ENGAGE_COLS = ("likes", "replies", "quotes", "retweets", "shares", "bookmarks")
# Snapshot preference: a settled 7d window beats 24h beats 30m.
_SNAPSHOT_PREF = ("7d", "24h", "30m")


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    return Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)


def _drafts_dir() -> Path:
    return Path(os.environ.get("DRAFTS_DIR") or (_engine_root() / "runtime" / "drafts"))


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #


def _aged_publishings(cutoff_iso: str) -> list[dict[str, Any]]:
    """Publishings whose published_at is on/older than cutoff_iso."""
    rows = db.fetchall(
        """
        SELECT id, piece_id, platform, published_at
        FROM publishings
        WHERE published_at IS NOT NULL AND published_at <= ?
        ORDER BY published_at DESC
        """,
        (cutoff_iso,),
    )
    return [dict(r) for r in rows]


def _best_metrics(publishing_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Return {publishing_id: metrics_row} picking the most-settled snapshot."""
    if not publishing_ids:
        return {}
    placeholders = ",".join(["?"] * len(publishing_ids))
    rows = db.fetchall(
        f"""
        SELECT publishing_id, snapshot_type, snapshot_at, impressions, likes,
               replies, quotes, retweets, shares, bookmarks, profile_clicks,
               link_clicks
        FROM metrics_daily
        WHERE publishing_id IN ({placeholders})
        ORDER BY snapshot_at DESC
        """,
        tuple(publishing_ids),
    )
    best: dict[int, dict[str, Any]] = {}
    best_rank: dict[int, int] = {}
    for r in rows:
        d = dict(r)
        pid = d["publishing_id"]
        st = d.get("snapshot_type")
        rank = _SNAPSHOT_PREF.index(st) if st in _SNAPSHOT_PREF else len(_SNAPSHOT_PREF)
        # lower rank index = more preferred; for equal snapshot pick the newest
        # (rows already ordered by snapshot_at DESC, so first seen wins ties)
        if pid not in best or rank < best_rank[pid]:
            best[pid] = d
            best_rank[pid] = rank
    return best


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _agg_piece(rows: list[dict[str, Any]]) -> dict[str, Any]:
    imp = sum(int(r.get("impressions") or 0) for r in rows)
    eng = sum(int(r.get(c) or 0) for r in rows for c in _ENGAGE_COLS)
    clicks = sum(int(r.get("link_clicks") or 0) for r in rows)
    eng_rate = (eng / imp) if imp > 0 else 0.0
    ctr = (clicks / imp) if imp > 0 else 0.0
    return {
        "impressions": imp,
        "engagements": eng,
        "link_clicks": clicks,
        "engagement_rate": round(eng_rate, 5),
        "ctr": round(ctr, 5),
        "platforms": len(rows),
    }


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in [0,1]); empty -> 0.0."""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = pct * (len(xs) - 1)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


# --------------------------------------------------------------------------- #
# Sidecar write
# --------------------------------------------------------------------------- #


def _write_promotion(piece_id: str, record: dict[str, Any], *, force: bool) -> bool:
    """Write promotion.json for a piece. Returns True if written/changed."""
    piece_dir = _drafts_dir() / piece_id
    if not piece_dir.is_dir():
        logger.info("promotion_scanner: no draft dir for piece=%s - skipping sidecar", piece_id)
        return False
    path = piece_dir / "promotion.json"
    if not force and path.is_file():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = {}
        # ignore volatile scanned_at when comparing
        a = {k: v for k, v in prev.items() if k != "scanned_at"}
        b = {k: v for k, v in record.items() if k != "scanned_at"}
        if a == b:
            return False
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    *,
    days: int = DEFAULT_DAYS,
    top_pct: float = DEFAULT_TOP_PCT,
    min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
    dry_run: bool = False,
    force: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Scan aged pieces, score them, flag the top ``top_pct`` as promotion winners."""
    started_at = time.monotonic()
    if not (0.0 < top_pct < 1.0):
        raise ValueError(f"top_pct must be in (0,1), got {top_pct}")
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff_iso = (now - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    pubs = _aged_publishings(cutoff_iso)
    if not pubs:
        logger.info("promotion_scanner: no publishings older than %dd (cutoff=%s)", days, cutoff_iso)
        _record_heartbeat("ok", started_at, error_message=None, rows=0)
        return {"status": "ok", "scanned": 0, "winners": 0, "reason": "no_aged_publishings"}

    by_piece: dict[str, list[int]] = {}
    for p in pubs:
        by_piece.setdefault(p["piece_id"], []).append(p["id"])

    metrics = _best_metrics([pid for ids in by_piece.values() for pid in ids])

    # aggregate per piece
    scored: dict[str, dict[str, Any]] = {}
    for piece_id, pub_ids in by_piece.items():
        rows = [metrics[i] for i in pub_ids if i in metrics]
        if not rows:
            continue
        scored[piece_id] = _agg_piece(rows)

    if not scored:
        logger.info("promotion_scanner: %d aged pieces but none have metrics yet", len(by_piece))
        _record_heartbeat("ok", started_at, error_message=None, rows=0)
        return {"status": "ok", "scanned": 0, "winners": 0, "reason": "no_metrics"}

    # cohort = pieces with enough exposure to rank fairly
    cohort = {pid: s for pid, s in scored.items() if s["impressions"] >= min_impressions}
    rates = [s["engagement_rate"] for s in cohort.values()]
    cutoff_rate = _percentile(rates, 1.0 - top_pct) if rates else 0.0

    written = 0
    winners = 0
    details: list[dict[str, Any]] = []
    for piece_id, s in scored.items():
        in_cohort = piece_id in cohort
        is_winner = in_cohort and s["engagement_rate"] >= cutoff_rate and cutoff_rate > 0
        if is_winner:
            winners += 1
        record = {
            "piece_id": piece_id,
            "promote": bool(is_winner),
            "engagement_rate": s["engagement_rate"],
            "ctr": s["ctr"],
            "impressions": s["impressions"],
            "engagements": s["engagements"],
            "link_clicks": s["link_clicks"],
            "in_cohort": in_cohort,
            "cohort_size": len(cohort),
            "threshold_top_pct": top_pct,
            "cutoff_engagement_rate": round(cutoff_rate, 5),
            "min_impressions": min_impressions,
            "days_since_publish_min": days,
            # explicit: this flag does NOT trigger a GPU render. Phase-2 tooling
            # reads promote + a human review before any avatar/longform spend.
            "action": "flag_only_no_render",
            "scanned_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        details.append({"piece_id": piece_id, "promote": is_winner, "eng_rate": s["engagement_rate"], "impressions": s["impressions"]})
        if not dry_run:
            if _write_promotion(piece_id, record, force=force):
                written += 1

    duration_s = int(time.monotonic() - started_at)
    logger.info(
        "promotion_scanner: scanned=%d cohort=%d winners=%d written=%d cutoff_rate=%.4f (%s)",
        len(scored), len(cohort), winners, written, cutoff_rate,
        "DRY-RUN" if dry_run else "live",
    )
    _record_heartbeat("ok", started_at, error_message=None, rows=written)
    return {
        "status": "dry_run" if dry_run else "ok",
        "scanned": len(scored),
        "cohort": len(cohort),
        "winners": winners,
        "written": written,
        "cutoff_engagement_rate": round(cutoff_rate, 5),
        "duration_seconds": duration_s,
        "details": sorted(details, key=lambda d: d["eng_rate"], reverse=True),
    }


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #


def _record_heartbeat(status: str, started_at: float, *, error_message: str | None, rows: int) -> None:
    duration = int(time.monotonic() - started_at)
    try:
        db.heartbeat.record(JOB_NAME, status, duration, rows_written=rows, error_message=error_message)
    except Exception:
        logger.exception("heartbeat write failed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m jobs.promotion_scanner",
        description="Flag tier-1 winners for tier-2 (GPU) promotion. Flag-only; never renders.",
    )
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="min age (days) since publish to score (default 7)")
    p.add_argument("--top-pct", type=float, default=DEFAULT_TOP_PCT, help="flag this top fraction by engagement rate (default 0.20)")
    p.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS, help="exclude pieces below this exposure (default 300)")
    p.add_argument("--dry-run", action="store_true", help="score + report; write no promotion.json")
    p.add_argument("--force", action="store_true", help="rewrite promotion.json even if unchanged")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    try:
        summary = run(
            days=args.days,
            top_pct=args.top_pct,
            min_impressions=args.min_impressions,
            dry_run=args.dry_run,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("promotion_scanner top-level failure: %s", exc)
        try:
            alert("P2", "promotion_scanner crashed", {"error": str(exc)[:300]})
        except Exception:
            logger.exception("alert emission failed")
        return 1
    return 0 if summary.get("status") in ("ok", "dry_run") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
