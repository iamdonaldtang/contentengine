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

from lib.content_inject import inject_cta  # noqa: E402
from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from lib.media_url import MediaUrlConfigError, sign_media_url  # noqa: E402
from lib.yt_metadata import YouTubeMetadata, load_or_derive  # noqa: E402
from sources.postiz import PostizError, postiz  # noqa: E402


# Platform → network mapping used to look up CTA URL in utm_links.json
# (whose keys are ``<network>_<account>``). Kept separate from the
# alias_map in _pick_utm_for_platform because that one services the
# fallback "did the utm_links generator use platform key or network key"
# question; this one is the canonical platform→network bridge.
_PLATFORM_TO_NETWORK = {
    "x_thread": "twitter",
    "x_short": "twitter",
    "linkedin_post": "linkedin",
    "linkedin_carousel": "linkedin",
    "medium_long": "medium",
    "yt_shorts": "youtube",
    "yt_long": "youtube",
    "tiktok": "tiktok",
}


def _pick_cta_url(
    utm_links: dict[str, Any],
    platform_key: str,
    account: str,
    *,
    kind: str = "long_url",
) -> str | None:
    """Look up the CTA URL for ``(platform, account)`` from utm_links.json.

    Args:
        utm_links: Parsed ``utm_links.json`` contents (caller-loaded).
        platform_key: e.g. ``"yt_shorts"``; mapped to network via
            :data:`_PLATFORM_TO_NETWORK`.
        account: e.g. ``"donald_en"`` — second segment of the utm_links
            key (``{network}_{account}``).
        kind: Which URL flavour to return — ``"long_url"`` (full URL with
            UTM query) or ``"short_url"`` (l.taskon.xyz shlink). Per
            config.yaml ``postiz.cta_url_kind``.

    Returns:
        URL string or ``None`` if the platform/account combination is
        missing or malformed.
    """
    if kind not in ("long_url", "short_url"):
        logger.warning("_pick_cta_url: unknown kind=%r (using long_url)", kind)
        kind = "long_url"
    network = _PLATFORM_TO_NETWORK.get(platform_key)
    if not network:
        logger.warning("_pick_cta_url: no network mapping for platform=%s", platform_key)
        return None
    blob = utm_links.get(f"{network}_{account}")
    if not isinstance(blob, dict):
        return None
    url = blob.get(kind)
    if not isinstance(url, str) or not url:
        return None
    return url


# Platforms that require an mp4 attachment for Postiz publishing. The mp4 lives
# at runtime/drafts/<piece_id>/shorts_60s.mp4 (mpt_runner / mpt callback writes).
# schedule_planner signs a short-lived URL via lib.media_url and passes it to
# postiz.create_post(media_urls=...) so Postiz can fetch it through the
# cloudflared tunnel. Without media_urls, Postiz YT provider crashes with
# "TypeError: Invalid URL" (verified 2026-05-16 piece-02 S9.5).
_VIDEO_PLATFORMS: frozenset[str] = frozenset({"yt_shorts", "yt_long", "tiktok"})
_VIDEO_MEDIA_FILENAME = "shorts_60s.mp4"

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
    account: str | None = None,
) -> dict[str, str | None]:
    """Pull (utm_campaign, utm_content, utm_term) from utm_links.json.

    Tolerates two shapes the utm_generator may emit:
        a)  ``{"<platform>": {"campaign":..., "content":..., "term":...}}``
        b)  ``{"<platform>": {"<account>": {...}}}`` (per-account fan-out)

    Args:
        utm_links: Parsed ``utm_links.json`` blob.
        platform_key: Engine platform key, e.g. ``yt_shorts``.
        account: When set, prefer the account-keyed sub-blob (shape b) over
            the flat (shape a) lookup. Used by T-08 matrix routing where the
            same platform fires for multiple accounts (donald_en primary +
            taskon_official cross-post). When None, falls back to whatever
            sub-dict appears first (legacy single-account behaviour).

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
    flat_keys = {"campaign", "content", "term"}
    # Per-account fan-out shape (b) when caller passed an account hint —
    # T-08 matrix routing needs per-account UTM differentiation so GA4 can
    # tell "Donald EN YT vs TaskOn Official YT" apart.
    if account and account in blob and isinstance(blob[account], dict):
        sub = blob[account]
        if flat_keys & sub.keys():
            out["utm_campaign"] = sub.get("campaign")
            out["utm_content"] = sub.get("content")
            out["utm_term"] = sub.get("term")
            return out
    # Flat shape (a)
    if flat_keys & blob.keys():
        out["utm_campaign"] = blob.get("campaign")
        out["utm_content"] = blob.get("content")
        out["utm_term"] = blob.get("term")
        return out
    # Per-account fan-out shape (b) without an account hint: pick the first
    # sub-dict (legacy single-account behaviour).
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
    postiz_cfg = cfg.get("postiz") or {}
    integrations: dict[str, str] = postiz_cfg.get("integrations") or {}
    accounts_map: dict[str, str] = postiz_cfg.get("accounts") or {}
    routing_cfg: dict[str, Any] = postiz_cfg.get("routing") or {}
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
                "account": accounts_map.get(platform_key, "donald_en"),
                "role": "primary",
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

        primary_account = accounts_map.get(platform_key, "donald_en")
        utm = _pick_utm_for_platform(
            utm_links or {}, platform_key, account=primary_account,
        ) if utm_links else {
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

        primary_scheduled_at = compute_slot_datetime(base_monday, weekday, hour, tz)

        plans.append({
            "platform": platform_key,
            "account": primary_account,
            "role": "primary",
            "integration_id": integrations.get(platform_key, ""),
            "content": content,
            "scheduled_at": primary_scheduled_at,
            "utm_campaign": utm["utm_campaign"],
            "utm_content": utm["utm_content"],
            "utm_term": utm["utm_term"],
            "skip_reason": skip_reason,
        })

        # T-08 matrix cross-post (B3 §5.2). Each entry produces an additional
        # plan with the same content + media but a different (account,
        # integration_id) pair and a delayed scheduled_at. Default config
        # has empty cross_post lists for every platform, so this loop is a
        # no-op until Donald connects secondary accounts.
        platform_routing = routing_cfg.get(platform_key) or {}
        cross_post = platform_routing.get("cross_post") or []
        if not isinstance(cross_post, list):
            logger.warning(
                "routing.%s.cross_post is not a list (got %s) — skipping",
                platform_key, type(cross_post).__name__,
            )
            cross_post = []
        for idx, entry in enumerate(cross_post):
            if not isinstance(entry, dict):
                logger.warning(
                    "routing.%s.cross_post[%d] not a dict (got %s) — skipping",
                    platform_key, idx, type(entry).__name__,
                )
                continue
            cp_account = entry.get("account")
            cp_integration = entry.get("integration_id")
            cp_offset = entry.get("offset_minutes")
            if not (cp_account and cp_integration and isinstance(cp_offset, (int, float))):
                logger.warning(
                    "routing.%s.cross_post[%d] missing required fields "
                    "(account/integration_id/offset_minutes) — skipping: %r",
                    platform_key, idx, entry,
                )
                continue
            if cp_offset <= 0:
                logger.warning(
                    "routing.%s.cross_post[%d] non-positive offset_minutes=%s — skipping",
                    platform_key, idx, cp_offset,
                )
                continue
            cp_scheduled_at = primary_scheduled_at + dt.timedelta(minutes=int(cp_offset))
            cp_utm = _pick_utm_for_platform(
                utm_links or {}, platform_key, account=cp_account,
            ) if utm_links else {
                "utm_campaign": None, "utm_content": None, "utm_term": None,
            }
            cp_skip: str | None = None
            if utm_links is None:
                cp_skip = "utm_links.json missing — UTM is mandatory (B1 §4)"
            elif content is None:
                cp_skip = f"draft missing: {fname}"
            plans.append({
                "platform": platform_key,
                "account": cp_account,
                "role": "cross_post",
                "integration_id": cp_integration,
                "content": content,
                "scheduled_at": cp_scheduled_at,
                "utm_campaign": cp_utm["utm_campaign"],
                "utm_content": cp_utm["utm_content"],
                "utm_term": cp_utm["utm_term"],
                "skip_reason": cp_skip,
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

    # Store the scheduled publish moment in UTC SQLite-native format
    # (``YYYY-MM-DD HH:MM:SS``) so cron queries can compare it directly
    # against ``datetime('now','-31 min')``. Anchor for T-05/T-07 30-min
    # post-publish nudges (see migration 011).
    sched_dt = plan.get("scheduled_at")
    scheduled_at_utc: str | None
    if isinstance(sched_dt, dt.datetime):
        scheduled_at_utc = (
            sched_dt.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        )
    else:
        scheduled_at_utc = None

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
        scheduled_at=scheduled_at_utc,
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


def _ensure_piece_in_db(piece_id: str) -> None:
    """Bootstrap a missing piece row from the on-disk selection_card.yaml.

    Manual runbooks (e.g. ``scripts/run_publish.ps1``) skip the normal
    piece-creation pipeline and jump straight to publishing. Without this
    defensive bootstrap, the publishings + state_events FK to pieces(id)
    fires a ``FOREIGN KEY constraint failed`` IntegrityError when we try
    to persist the Postiz response.
    """
    if db.pieces.get(piece_id) is not None:
        return
    card_path = _drafts_dir() / piece_id / "selection_card.yaml"
    if not card_path.exists():
        raise RuntimeError(
            f"piece {piece_id!r} not in DB and no selection_card.yaml at "
            f"{card_path} — cannot bootstrap"
        )
    logger.warning(
        "bootstrap: creating piece=%s from on-disk selection_card.yaml",
        piece_id,
    )
    db.pieces.create(
        piece_id,
        card_path.read_text(encoding="utf-8"),
        actor="schedule_planner.bootstrap",
    )
    # Jump straight to 'reviewed' so the state_events log in
    # _persist_publishing (from_state='reviewed' → 'scheduled') tells the
    # truth instead of recording a phantom transition.
    db.pieces.update_state(
        piece_id,
        "reviewed",
        actor="schedule_planner.bootstrap",
        notes="auto-promoted by schedule_planner; piece missing from DB",
    )


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

    if not dry_run:
        _ensure_piece_in_db(piece_id)

    plans = build_schedule(piece_id, base_monday=base_monday, cfg=cfg)
    scheduled = 0
    skipped = 0
    failures = 0
    results: list[dict[str, Any]] = []

    # Cache UTM + account/CTA-kind config once for all plans below.
    piece_dir = _drafts_dir() / piece_id
    utm_links = _load_utm_links(piece_dir) or {}
    postiz_cfg = cfg.get("postiz") or {}
    accounts_map: dict[str, str] = postiz_cfg.get("accounts") or {}
    cta_kind: str = postiz_cfg.get("cta_url_kind") or "long_url"

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

        # Video platforms need a media URL Postiz can fetch. Without it, the
        # YouTube provider crashes with "TypeError: Invalid URL". We sign a
        # short-lived URL on the existing cloudflared tunnel so Postiz can
        # pull the mp4 directly from engine's disk.
        media_urls: list[str] | None = None
        if plat in _VIDEO_PLATFORMS:
            mp4_path = _drafts_dir() / piece_id / _VIDEO_MEDIA_FILENAME
            if not mp4_path.is_file():
                # mp4 hasn't been rendered yet (mpt_runner pending or failed).
                # Skip this platform rather than letting Postiz throw a cryptic
                # error 10 min later when the post fires.
                failures += 1
                results.append({
                    "platform": plat,
                    "status": "failed",
                    "error": f"no media file at {mp4_path} — mpt_runner not yet completed",
                })
                logger.warning(
                    "skip %s for piece=%s: missing %s (mpt_runner still pending?)",
                    plat, piece_id, _VIDEO_MEDIA_FILENAME,
                )
                continue
            try:
                media_urls = [sign_media_url(piece_id, _VIDEO_MEDIA_FILENAME)]
            except (MediaUrlConfigError, ValueError) as exc:
                failures += 1
                results.append({
                    "platform": plat,
                    "status": "failed",
                    "error": f"media URL signing failed: {exc}",
                })
                logger.exception("sign_media_url failed for piece=%s: %s", piece_id, exc)
                try:
                    alert(
                        "P0",
                        f"schedule_planner: cannot sign media URL for {piece_id}/{plat}",
                        {"error": str(exc)[:300]},
                    )
                except Exception:
                    logger.exception("alert() emission failed")
                continue

        # ───── CTA URL injection ─────────────────────────────────────────── #
        # Replace ``{{CTA_URL}}`` placeholder in the platform content (+ the
        # LLM-derived YouTube description) with the long/short URL bound to
        # this (platform, account) pair. Falls back to appending the URL when
        # no placeholder is present (legacy markdown without SOP) — emits a
        # WARN so the author notices for next piece. 2026-05-16 introduction.
        # T-08 (2026-05-18): account now comes from the plan itself, not the
        # platform-level accounts_map, so cross_post plans get their own CTA.
        cta_account = plan.get("account") or accounts_map.get(plat, "donald_en")
        cta_url = _pick_cta_url(utm_links, plat, cta_account, kind=cta_kind)
        cta_label = "no_cta_url"
        if cta_url:
            try:
                plan["content"] = inject_cta(plan["content"], cta_url, strict=False)
                if extra_settings.get("description"):
                    extra_settings["description"] = inject_cta(
                        extra_settings["description"], cta_url, strict=False,
                    )
                cta_label = f"cta_kind={cta_kind} account={cta_account}"
            except Exception as exc:  # noqa: BLE001 — never block on this
                logger.exception(
                    "inject_cta failed for piece=%s platform=%s: %s",
                    piece_id, plat, exc,
                )
                cta_label = f"cta=ERR ({type(exc).__name__})"
        else:
            # Missing CTA URL is a soft failure — the post will go live but
            # without attribution. Loud P2 so the operator notices.
            logger.warning(
                "no CTA URL for piece=%s platform=%s account=%s — post will "
                "publish without attribution link",
                piece_id, plat, cta_account,
            )
            try:
                alert(
                    "P2",
                    f"schedule_planner: missing CTA URL for {piece_id}/{plat}",
                    {"account": cta_account, "kind": cta_kind},
                )
            except Exception:
                logger.exception("alert() emission failed")

        if dry_run:
            logger.info(
                "DRY-RUN · piece=%s platform=%s role=%s account=%s scheduled_at=%s "
                "utm_campaign=%s integration=%s cta=%s %s",
                piece_id, plat,
                plan.get("role", "primary"),
                plan.get("account", "(none)"),
                plan["scheduled_at"].isoformat(),
                plan["utm_campaign"],
                (plan["integration_id"][:8] + "...") if plan["integration_id"] else "(empty)",
                cta_label,
                yt_meta_label or "",
            )
            entry: dict[str, Any] = {
                "platform": plat,
                "role": plan.get("role", "primary"),
                "account": plan.get("account"),
                "status": "dry_run",
                "scheduled_at": plan["scheduled_at"].isoformat(),
                "utm_campaign": plan["utm_campaign"],
                "cta": cta_label,
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
                media_urls=media_urls,
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
