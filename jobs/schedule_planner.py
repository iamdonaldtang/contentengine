"""Schedule Planner · Sunday 22:00 cron (B1 §3.2 + §2).

Reads one piece's adapter-orchestrator drafts, computes per-platform
scheduled_at per B1 §3.2 (X Mon 09:00 ET → LinkedIn Tue 09:00 ET → Carousel
Tue 10:00 → Medium Wed 09:00 → YT Shorts Thu 09:00 → TikTok Thu 10:00), and
hands off to Postiz Public API to create the scheduled posts. Persists each
scheduled handoff into ``publishings`` (with ``postiz_post_id``) plus a
``state_events`` audit row.

NEVER drafts content — that's Cowork's job (C §原则1 红线). The planner
strictly orchestrates pre-written drafts + pre-generated UTM links.

Inputs
------
* ``runtime/drafts/<piece_id>/<filename>.md`` — per config.yaml
  ``schedule_planner.draft_filenames``. Missing files = skip with WARNING.
* ``runtime/drafts/<piece_id>/utm_links.json`` — output of
  ``jobs.utm_generator``. Missing = skip ALL platforms with P1 alert (UTM
  is non-negotiable — B1 §4 "任何外链不带 UTM = 这条内容白发").

Outputs
-------
* For each scheduled platform: one ``publishings`` row with
  ``postiz_post_id`` + ``utm_campaign/content/term`` stamped, plus a
  ``state_events`` row.
* Heartbeat written on completion (status=ok|warning|failed).

CLI::

    python -m jobs.schedule_planner --piece-id 2026W19-thread01
    python -m jobs.schedule_planner --piece-id 2026W19-thread01 --dry-run
    python -m jobs.schedule_planner --piece-id 2026W19-thread01 \\
        --base-monday 2026-05-18   # override the anchor (testing)

Cron: Sunday 22:00 (post topic_ranker + UTM gen).
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
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

load_dotenv(override=False)

# Make `python jobs/schedule_planner.py` work as well as `-m jobs.schedule_planner`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from lib.yt_metadata import YouTubeMetadata, load_or_derive  # noqa: E402
from sources.postiz import PostizError, postiz  # noqa: E402

logger = logging.getLogger(__name__)

JOB_NAME = "schedule_planner"


# --------------------------------------------------------------------------- #
# Path / config helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    env_root = os.environ.get("ENGINE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _drafts_dir() -> Path:
    env_drafts = os.environ.get("DRAFTS_DIR")
    if env_drafts:
        return Path(env_drafts).expanduser().resolve()
    return _engine_root() / "runtime" / "drafts"


def _config_yaml_path() -> Path:
    return _engine_root() / "config.yaml"


def _load_config() -> dict[str, Any]:
    path = _config_yaml_path()
    if not path.is_file():
        raise FileNotFoundError(f"config.yaml missing at {path}")
    with path.open("r", encoding="utf-8") as fp:
        doc = yaml.safe_load(fp) or {}
    if not isinstance(doc, dict):
        raise RuntimeError(f"config.yaml not a mapping: {type(doc).__name__}")
    return doc


# --------------------------------------------------------------------------- #
# Time math
# --------------------------------------------------------------------------- #


def _next_monday_local(now_local: dt.datetime) -> dt.date:
    """Return the upcoming Monday's date in the same timezone as ``now_local``.

    If ``now_local`` is itself a Monday before 09:00, we still return the
    NEXT Monday — schedule_planner is a Sunday-22:00 cron, and we never want
    to schedule a Monday-09:00 post 11 hours from now (publishings get an
    unreviewed delivery window). For deterministic testing, callers can
    override the anchor via ``--base-monday`` on the CLI.
    """
    today = now_local.date()
    # weekday(): Mon=0..Sun=6. Distance to next Monday: 7 when today is Mon,
    # else (7 - weekday) mod 7 — but we want STRICTLY next Monday for cron
    # safety.
    delta = (0 - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + dt.timedelta(days=delta)


def compute_slot_datetime(
    base_monday: dt.date,
    weekday: int,
    hour: int,
    tz: ZoneInfo,
) -> dt.datetime:
    """Convert (weekday_0_to_6, hour) anchored to ``base_monday`` → aware datetime.

    Example: base_monday=2026-05-18, weekday=1 (Tue), hour=9, tz=ET
            → 2026-05-19T09:00:00-04:00 (or -05:00 in winter).
    """
    if not 0 <= weekday <= 6:
        raise ValueError(f"weekday must be 0..6, got {weekday}")
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0..23, got {hour}")
    slot_date = base_monday + dt.timedelta(days=weekday)
    return dt.datetime(
        slot_date.year, slot_date.month, slot_date.day,
        hour, 0, 0, tzinfo=tz,
    )


# --------------------------------------------------------------------------- #
# Draft + UTM loaders
# --------------------------------------------------------------------------- #


def _read_text_file(path: Path) -> str | None:
    """Return file text or None if missing / empty (logs WARNING)."""
    if not path.is_file():
        logger.warning("missing draft file: %s", path)
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        logger.warning("empty draft file: %s", path)
        return None
    return text


def _load_utm_links(piece_dir: Path) -> dict[str, Any] | None:
    """Parse utm_links.json. Returns None on any failure (caller alerts)."""
    path = piece_dir / "utm_links.json"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:
        logger.exception("utm_links.json parse failure at %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("utm_links.json not a mapping at %s", path)
        return None
    return data


def _pick_utm_for_platform(
    utm_links: dict[str, Any],
    platform_key: str,
) -> dict[str, str | None]:
    """Pull (utm_campaign, utm_content, utm_term) from utm_links.json.

    Tolerates two shapes the utm_generator may emit:
        a)  ``{"<platform>": {"campaign":..., "content":..., "term":...}}``
        b)  ``{"<platform>": {"<account>": {...}}}`` (per-account fan-out)

    Returns ``{"utm_campaign": ..., "utm_content": ..., "utm_term": ...}``
    with ``None`` values when a segment isn't present. The schedule_planner
    never invents UTM segments; missing fields stay None.
    """
    out: dict[str, str | None] = {
        "utm_campaign": None,
        "utm_content": None,
        "utm_term": None,
    }
    blob = utm_links.get(platform_key)
    if blob is None:
        # Try common alias mappings (linkedin_post → linkedin, etc.)
        alias_map = {
            "x_thread": "twitter",
            "x_short": "twitter",
            "linkedin_post": "linkedin",
            "linkedin_carousel": "linkedin",
            "medium_long": "medium",
            "yt_shorts": "youtube",
            "tiktok": "tiktok",
        }
        alias = alias_map.get(platform_key)
        if alias:
            blob = utm_links.get(alias)
    if not isinstance(blob, dict):
        return out
    # Flat shape (a)
    flat_keys = {"campaign", "content", "term"}
    if flat_keys & blob.keys():
        out["utm_campaign"] = blob.get("campaign")
        out["utm_content"] = blob.get("content")
        out["utm_term"] = blob.get("term")
        return out
    # Per-account fan-out shape (b): pick the first sub-dict.
    for sub in blob.values():
        if isinstance(sub, dict) and flat_keys & sub.keys():
            out["utm_campaign"] = sub.get("campaign")
            out["utm_content"] = sub.get("content")
            out["utm_term"] = sub.get("term")
            return out
    return out


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def build_schedule(
    piece_id: str,
    *,
    base_monday: dt.date | None = None,
    cfg: dict[str, Any] | None = None,
    now_local: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Return one plan dict per platform with content + scheduled_at + UTM.

    A plan dict has keys: ``platform``, ``integration_id``, ``content``,
    ``scheduled_at`` (aware datetime in slot tz), ``utm_campaign``,
    ``utm_content``, ``utm_term``, ``skip_reason`` (str|None — when set,
    the platform should be skipped, but the entry is still returned so
    callers can log/audit).

    Args:
        piece_id: Folder name under ``runtime/drafts/`` (e.g. ``2026W19-thread01``).
        base_monday: Override the next-Monday anchor for deterministic tests.
        cfg: Pre-loaded config.yaml dict (avoids reading twice in tests).
        now_local: Override "now" for deterministic tests when base_monday
            is not supplied.
    """
    cfg = cfg or _load_config()
    sp_cfg = cfg.get("schedule_planner") or {}
    integrations: dict[str, str] = (
        (cfg.get("postiz") or {}).get("integrations") or {}
    )
    slots: dict[str, list[int]] = sp_cfg.get("slots") or {}
    filenames: dict[str, str] = sp_cfg.get("draft_filenames") or {}
    tz_name = sp_cfg.get("timezone") or "America/New_York"
    tz = ZoneInfo(tz_name)

    if base_monday is None:
        local_now = now_local or dt.datetime.now(tz)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=tz)
        else:
            local_now = local_now.astimezone(tz)
        base_monday = _next_monday_local(local_now)

    piece_dir = _drafts_dir() / piece_id
    utm_links = _load_utm_links(piece_dir)

    plans: list[dict[str, Any]] = []
    for platform_key, slot_pair in slots.items():
        if not (isinstance(slot_pair, (list, tuple)) and len(slot_pair) == 2):
            logger.warning("malformed slot for %s: %r — skipping", platform_key, slot_pair)
            continue
        weekday, hour = int(slot_pair[0]), int(slot_pair[1])

        # Draft content
        fname = filenames.get(platform_key)
        if not fname:
            plans.append({
                "platform": platform_key,
                "integration_id": integrations.get(platform_key, ""),
                "content": None,
                "scheduled_at": None,
                "utm_campaign": None,
                "utm_content": None,
                "utm_term": None,
                "skip_reason": f"no draft filename mapped for {platform_key}",
            })
            continue
        content = _read_text_file(piece_dir / fname)

        # UTM
        utm = _pick_utm_for_platform(utm_links or {}, platform_key) if utm_links else {
            "utm_campaign": None, "utm_content": None, "utm_term": None,
        }

        # Skip reasons (preserve order: missing utm is more critical than missing draft)
        skip_reason: str | None = None
        if utm_links is None:
            skip_reason = "utm_links.json missing — UTM is mandatory (B1 §4)"
        elif content is None:
            skip_reason = f"draft missing: {fname}"
        elif not integrations.get(platform_key):
            skip_reason = f"postiz.integrations.{platform_key} not configured"

        scheduled_at = compute_slot_datetime(base_monday, weekday, hour, tz)

        plans.append({
            "platform": platform_key,
            "integration_id": integrations.get(platform_key, ""),
            "content": content,
            "scheduled_at": scheduled_at,
            "utm_campaign": utm["utm_campaign"],
            "utm_content": utm["utm_content"],
            "utm_term": utm["utm_term"],
            "skip_reason": skip_reason,
        })
    return plans


def _persist_publishing(
    piece_id: str,
    plan: dict[str, Any],
    postiz_response: dict[str, Any] | None,
) -> int:
    """Insert publishings + state_events rows. Returns the new publishings.id."""
    post_id = None
    if isinstance(postiz_response, dict):
        # Newer Postiz: {"posts": [{"id": "..."}]} ; older: {"id": "..."}
        posts = postiz_response.get("posts")
        if isinstance(posts, list) and posts and isinstance(posts[0], dict):
            post_id = posts[0].get("id") or posts[0].get("releaseId")
        else:
            post_id = postiz_response.get("id") or postiz_response.get("releaseId")

    pub_id = db.publishings.upsert(
        piece_id=piece_id,
        platform=plan["platform"],
        postiz_post_id=str(post_id) if post_id else None,
        utm_campaign=plan.get("utm_campaign"),
        utm_content=plan.get("utm_content"),
        utm_term=plan.get("utm_term"),
        # published_at intentionally left NULL — Postiz fires the actual
        # publish; metrics_collector backfills published_at when the
        # /analytics endpoint returns it.
        published_at=None,
    )
    db.state_events.log(
        piece_id,
        from_state="reviewed",
        to_state="scheduled",
        actor=JOB_NAME,
        notes=(
            f"platform={plan['platform']} "
            f"scheduled_at={plan['scheduled_at'].isoformat() if plan['scheduled_at'] else 'unknown'} "
            f"postiz_post_id={post_id}"
        ),
    )
    return pub_id


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    piece_id: str,
    *,
    dry_run: bool = False,
    base_monday: dt.date | None = None,
) -> dict[str, Any]:
    """Plan + schedule one piece across every configured platform.

    Args:
        piece_id: Folder name under ``runtime/drafts/``.
        dry_run: When True, print the plan and DO NOT call Postiz / write DB.
        base_monday: Override anchor for deterministic testing.

    Returns:
        Summary dict (planned/scheduled/skipped counts, per-platform results).
    """
    started_at = time.monotonic()
    cfg = _load_config()

    plans = build_schedule(piece_id, base_monday=base_monday, cfg=cfg)
    scheduled = 0
    skipped = 0
    failures = 0
    results: list[dict[str, Any]] = []

    for plan in plans:
        plat = plan["platform"]
        if plan.get("skip_reason"):
            logger.warning("skip %s for piece=%s: %s", plat, piece_id, plan["skip_reason"])
            skipped += 1
            results.append({
                "platform": plat,
                "status": "skipped",
                "reason": plan["skip_reason"],
                "scheduled_at": (
                    plan["scheduled_at"].isoformat() if plan["scheduled_at"] else None
                ),
            })
            continue

        # YouTube needs platform-specific settings (title / description /
        # privacy / tags / category). Load via Cowork-primary + LLM-fallback
        # chain. Other platforms pass an empty settings dict.
        extra_settings: dict[str, Any] = {}
        yt_meta_label: str | None = None
        if plat in ("yt_shorts", "yt_long"):
            try:
                yt_meta = load_or_derive(piece_id, _drafts_dir() / piece_id)
                extra_settings = yt_meta.to_postiz_settings()
                yt_meta_label = (
                    f"yt_meta.source={yt_meta.source} title={yt_meta.title[:40]!r}"
                )
            except Exception as exc:  # noqa: BLE001 — never block on this
                logger.exception("yt_metadata load_or_derive failed for %s: %s", piece_id, exc)
                yt_meta_label = f"yt_meta=ERR ({type(exc).__name__})"

        if dry_run:
            logger.info(
                "DRY-RUN · piece=%s platform=%s scheduled_at=%s utm_campaign=%s integration=%s %s",
                piece_id, plat,
                plan["scheduled_at"].isoformat(),
                plan["utm_campaign"],
                (plan["integration_id"][:8] + "...") if plan["integration_id"] else "(empty)",
                yt_meta_label or "",
            )
            entry: dict[str, Any] = {
                "platform": plat,
                "status": "dry_run",
                "scheduled_at": plan["scheduled_at"].isoformat(),
                "utm_campaign": plan["utm_campaign"],
            }
            if yt_meta_label:
                entry["yt_meta"] = yt_meta_label
            results.append(entry)
            scheduled += 1  # count for dry-run reporting
            continue

        try:
            resp = postiz.create_post(
                integration_id=plan["integration_id"],
                content=plan["content"],
                scheduled_at=plan["scheduled_at"],
                extra_settings=extra_settings or None,
            )
            pub_id = _persist_publishing(piece_id, plan, resp)
            scheduled += 1
            results.append({
                "platform": plat,
                "status": "scheduled",
                "scheduled_at": plan["scheduled_at"].isoformat(),
                "publishing_id": pub_id,
                "postiz_response_keys": list(resp.keys())[:8] if isinstance(resp, dict) else [],
            })
            logger.info(
                "scheduled %s for piece=%s scheduled_at=%s publishing_id=%s",
                plat, piece_id, plan["scheduled_at"].isoformat(), pub_id,
            )
        except PostizError as exc:
            failures += 1
            results.append({
                "platform": plat,
                "status": "failed",
                "error": str(exc)[:300],
            })
            logger.exception("postiz.create_post failed for %s: %s", plat, exc)
            try:
                db.publish_failures.insert(
                    severity="P1",
                    source=JOB_NAME,
                    failure_type="postiz_create_post_failed",
                    failure_detail=f"piece={piece_id} platform={plat} err={exc}",
                )
            except Exception:
                logger.exception("publish_failures insert failed")
            try:
                alert(
                    "P1",
                    f"schedule_planner: Postiz create_post failed for {piece_id}/{plat}",
                    {"error": str(exc)[:300]},
                )
            except Exception:
                logger.exception("alert() emission failed")
        except Exception as exc:  # noqa: BLE001 — defensive
            failures += 1
            results.append({
                "platform": plat,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            logger.exception("schedule_planner unexpected error on %s: %s", plat, exc)

    duration = int(time.monotonic() - started_at)
    status = "ok"
    if failures and scheduled == 0:
        status = "failed"
    elif failures or skipped:
        status = "warning"

    if not dry_run:
        try:
            db.heartbeat.record(
                JOB_NAME, status, duration,
                rows_written=scheduled,
                error_message=(
                    f"failures={failures} skipped={skipped}" if (failures or skipped) else None
                ),
            )
        except Exception:
            logger.exception("heartbeat write failed")

    summary = {
        "piece_id": piece_id,
        "planned": len(plans),
        "scheduled": scheduled,
        "skipped": skipped,
        "failures": failures,
        "status": status,
        "dry_run": dry_run,
        "results": results,
        "duration_seconds": duration,
    }
    logger.info("schedule_planner done: %s", {k: v for k, v in summary.items() if k != "results"})
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m jobs.schedule_planner",
        description="Schedule one piece across all platforms via Postiz (B1 §3.2).",
    )
    p.add_argument("--piece-id", required=True, help="folder name under runtime/drafts/")
    p.add_argument("--dry-run", action="store_true", help="don't call Postiz; print plan only")
    p.add_argument(
        "--base-monday",
        default=None,
        help="override next-Monday anchor (YYYY-MM-DD) for deterministic testing",
    )
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    base_monday: dt.date | None = None
    if args.base_monday:
        try:
            base_monday = dt.date.fromisoformat(args.base_monday)
        except ValueError as exc:
            logger.error("invalid --base-monday %r: %s", args.base_monday, exc)
            return 2
    try:
        summary = run(args.piece_id, dry_run=args.dry_run, base_monday=base_monday)
    except FileNotFoundError as exc:
        logger.error("schedule_planner: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception("schedule_planner top-level failure: %s", exc)
        return 2
    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
