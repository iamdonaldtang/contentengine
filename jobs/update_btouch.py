"""Update Btouch · PRD §2.9.

Every Monday 09:00 — push last week's published Blog / Insight / Case pieces
into the 6 TaskOn in-platform touchpoints (dashboard / docs / changelog /
template / metrics_page / error_explain).

Per-touchpoint flow:
  1. Pick best-matching piece from last week's ``state='published'`` set,
     comparing ``selection_card.content_type`` against the touchpoint's
     ``preferred_type`` list.
  2. Build UTM (source=taskon, medium=touchpoint.utm_medium, campaign=piece_id,
     content=touchpoint.name, term=touchpoint.id).
  3. Shorten via ``lib.shlink``; fall back to the long URL on failure.
  4. Push to TaskOn admin API via ``sources.btouch.push_content_card``.
  5. Record one ``heartbeat`` row per touchpoint + a single aggregate row at
     the end of the run.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from lib.db import Piece, db

load_dotenv(override=False)

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_CONFIG_YAML = _ENGINE_ROOT / "config.yaml"

_JOB_NAME = "update_btouch"


# --------------------------------------------------------------------------- #
# Config + date helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Touchpoint:
    id: int
    name: str
    preferred_type: list[str]
    utm_medium: str


def _load_touchpoints() -> list[Touchpoint]:
    if not _CONFIG_YAML.is_file():
        raise FileNotFoundError(f"config.yaml not found at {_CONFIG_YAML}")
    with _CONFIG_YAML.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    raw = (doc.get("btouch") or {}).get("touchpoints") or []
    if not raw:
        raise ValueError("btouch.touchpoints empty in config.yaml")
    out: list[Touchpoint] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                Touchpoint(
                    id=int(item["id"]),
                    name=str(item["name"]),
                    preferred_type=list(item.get("preferred_type") or []),
                    utm_medium=str(item.get("utm_medium") or ""),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("skipping malformed touchpoint entry %s: %s", item, exc)
    return out


def _last_monday_iso(now: datetime | None = None) -> str:
    """Return the ISO date string of the most recent Monday (inclusive).

    If today *is* Monday, returns 7 days ago — i.e. "last week's Monday" so
    that yesterday's freshly-published pieces are included.
    """
    base = now or datetime.now(timezone.utc)
    days_since_monday = base.weekday()  # Mon=0
    last_monday = base - timedelta(days=days_since_monday + 7)
    return last_monday.date().isoformat()


# --------------------------------------------------------------------------- #
# Piece matching
# --------------------------------------------------------------------------- #


def _piece_content_type(piece: Piece) -> str:
    card = piece.card()
    if not isinstance(card, dict):
        return ""
    return str(card.get("content_type") or card.get("type") or "").lower()


def _piece_title(piece: Piece) -> str:
    card = piece.card()
    if isinstance(card, dict):
        for k in ("title", "headline", "title_hypothesis"):
            v = card.get(k)
            if v:
                return str(v)
    return piece.id


def _piece_summary(piece: Piece) -> str:
    card = piece.card()
    if isinstance(card, dict):
        for k in ("summary", "preview", "abstract", "tldr"):
            v = card.get(k)
            if v:
                return str(v)
    return ""


def _piece_blog_url(piece: Piece, fallback_base: str = "https://taskon.xyz/blog/") -> str:
    card = piece.card()
    if isinstance(card, dict):
        for k in ("blog_url", "url", "canonical_url"):
            v = card.get(k)
            if v:
                return str(v)
    # last resort: synthesize from piece id
    return f"{fallback_base.rstrip('/')}/{piece.id}"


def _match_for_touchpoint(
    pieces: list[Piece], touchpoint: Touchpoint, already_used: set[str]
) -> Piece | None:
    """Pick the best-matching piece for ``touchpoint``.

    Preference order:
      1. Unused piece whose ``content_type`` ∈ touchpoint.preferred_type.
      2. Used-elsewhere piece matching preferred_type (allow reuse — better
         than nothing).
      3. None (touchpoint will be skipped).
    """
    preferred = {p.lower() for p in touchpoint.preferred_type}

    # First pass: unused + preferred type.
    for piece in pieces:
        if piece.id in already_used:
            continue
        if _piece_content_type(piece) in preferred:
            return piece
    # Second pass: any unused piece (so the touchpoint isn't blank).
    for piece in pieces:
        if piece.id in already_used:
            continue
        return piece
    # Third pass: reuse a piece even if used elsewhere.
    for piece in pieces:
        if _piece_content_type(piece) in preferred:
            return piece
    return None


# --------------------------------------------------------------------------- #
# Push pipeline
# --------------------------------------------------------------------------- #


def _build_utm(*, piece: Piece, touchpoint: Touchpoint) -> tuple[str, str]:
    """Return ``(utm_query, long_url)`` for the touchpoint."""
    from lib.utm import UTMParams, append_utm, build_utm

    params = UTMParams(
        source="taskon",
        medium=touchpoint.utm_medium or "in_platform",
        campaign=piece.id.lower(),
        content=touchpoint.name.lower(),
        term=str(touchpoint.id),
    )
    base = _piece_blog_url(piece)
    return build_utm(params), append_utm(base, params)


def _shorten(long_url: str, slug: str) -> str:
    try:
        from lib.shlink import shlink  # type: ignore[attr-defined]
    except ImportError:
        logger.warning("lib.shlink unavailable; using long URL")
        return long_url
    try:
        return shlink.shorten(long_url, custom_slug=slug)
    except Exception as exc:
        logger.warning("shlink failed for slug=%s: %s; using long URL", slug, exc)
        return long_url


def _push_to_taskon(
    *,
    touchpoint: Touchpoint,
    piece: Piece,
    short_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Push one card via ``sources.btouch.push_content_card``. Honours dry-run."""
    title = _piece_title(piece)
    preview = _piece_summary(piece)

    if dry_run:
        logger.info(
            "[DRY-RUN] would push touchpoint=%s(id=%s) piece=%s title=%s short=%s",
            touchpoint.name,
            touchpoint.id,
            piece.id,
            title,
            short_url,
        )
        return {"dry_run": True, "touchpoint_id": touchpoint.id, "piece_id": piece.id}

    from sources.btouch import btouch  # lazy import

    return btouch.push_content_card(
        touchpoint_id=touchpoint.id,
        title=title,
        short_url=short_url,
        preview=preview,
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(dry_run: bool = False, week_anchor: datetime | None = None) -> int:
    """Push last week's published pieces to all 6 touchpoints.

    Returns the number of touchpoints successfully pushed.
    """
    started = time.monotonic()
    touchpoints = _load_touchpoints()
    last_monday = _last_monday_iso(week_anchor)
    pieces = db.pieces.published_after(last_monday)

    logger.info(
        "update_btouch start · since=%s touchpoints=%s pieces=%s dry_run=%s",
        last_monday,
        len(touchpoints),
        len(pieces),
        dry_run,
    )
    if not pieces:
        logger.warning("no published pieces since %s; nothing to push", last_monday)
        db.heartbeat.record(
            job_name=_JOB_NAME,
            status="warning",
            duration_seconds=int(time.monotonic() - started),
            rows_written=0,
            error_message="no published pieces",
        )
        return 0

    used: set[str] = set()
    pushed = 0
    failures = 0

    for tp in touchpoints:
        match = _match_for_touchpoint(pieces, tp, used)
        if match is None:
            logger.info("touchpoint=%s skipped (no matching piece)", tp.name)
            continue

        try:
            utm_query, long_url = _build_utm(piece=match, touchpoint=tp)
        except ImportError as exc:
            logger.error("lib.utm unavailable; cannot push touchpoint=%s: %s", tp.name, exc)
            failures += 1
            db.publish_failures.insert(
                severity="P1",
                source=_JOB_NAME,
                failure_type="utm_unavailable",
                failure_detail=str(exc),
            )
            continue
        except Exception as exc:
            logger.exception("UTM build failed for touchpoint=%s piece=%s", tp.name, match.id)
            failures += 1
            db.publish_failures.insert(
                severity="P1",
                source=_JOB_NAME,
                failure_type="utm_build_error",
                failure_detail=f"{tp.name}/{match.id}: {exc}",
            )
            continue

        slug = f"bt-{tp.id}-{match.id.lower()}"
        short_url = _shorten(long_url, slug)

        try:
            push_result = _push_to_taskon(
                touchpoint=tp, piece=match, short_url=short_url, dry_run=dry_run
            )
            logger.info(
                "pushed touchpoint=%s(id=%s) piece=%s result=%s",
                tp.name,
                tp.id,
                match.id,
                push_result,
            )
            used.add(match.id)
            pushed += 1
            # Heartbeat per touchpoint (rows_written=1 each).
            db.heartbeat.record(
                job_name=f"{_JOB_NAME}.{tp.name}",
                status="ok",
                duration_seconds=0,
                rows_written=1,
            )
        except Exception as exc:
            logger.exception("push failed for touchpoint=%s piece=%s", tp.name, match.id)
            failures += 1
            db.publish_failures.insert(
                severity="P1",
                source=_JOB_NAME,
                failure_type="btouch_push_failed",
                failure_detail=f"{tp.name}/{match.id}: {type(exc).__name__}: {exc}",
            )

    duration = int(time.monotonic() - started)
    status = "ok" if failures == 0 else ("warning" if pushed > 0 else "failed")
    db.heartbeat.record(
        job_name=_JOB_NAME,
        status=status,
        duration_seconds=duration,
        rows_written=pushed,
        error_message=None if failures == 0 else f"{failures} touchpoint(s) failed",
    )
    logger.info(
        "update_btouch done · pushed=%s failures=%s duration=%ss", pushed, failures, duration
    )
    return pushed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn Update Btouch (PRD §2.9)")
    p.add_argument("--dry-run", action="store_true", help="log only, no API calls")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        pushed = run(dry_run=args.dry_run)
    except FileNotFoundError as exc:
        logger.error("update_btouch config error: %s", exc)
        return 2
    except Exception:
        logger.exception("update_btouch failed")
        return 1
    return 0 if pushed > 0 else 3


if __name__ == "__main__":
    sys.exit(main())
