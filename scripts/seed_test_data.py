"""Idempotent fixture loader for local dev + integration tests.

Seeds the database with a representative slice of state across all major
tables:
  * 3 pieces (candidate / drafted / published)
  * 2 publishings per published piece (twitter + linkedin)
  * 5 metrics_daily rows (30m / 24h / 7d snapshots)
  * 3 leads with full UTM trail
  * 8 user_journey events (impression / click / signup)
  * 1 newsletter_campaigns row
  * 6 btouch_daily rows (one per touchpoint)

Idempotency: keyed off deterministic ``piece_id`` / ``campaign_id`` /
``email`` values. Re-running is a no-op (skipped rows are logged at INFO).

CLI::

    python -m scripts.seed_test_data
    python -m scripts.seed_test_data --clean    # truncate seed rows first
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Deterministic fixture identifiers
# --------------------------------------------------------------------------- #


SEED_PIECE_IDS: tuple[str, ...] = (
    "seed-2026W19-cand01",
    "seed-2026W19-draft01",
    "seed-2026W19-pub01",
)
SEED_NEWSLETTER_CAMPAIGN_ID: str = "seed-nl-2026-05"
SEED_LEAD_EMAILS: tuple[str, ...] = (
    "seed.lead1@example.com",
    "seed.lead2@example.com",
    "seed.lead3@example.com",
)
SEED_TOUCHPOINTS: tuple[tuple[int, str, str], ...] = (
    (1, "dashboard_card", "dashboard"),
    (2, "docs_link", "docs"),
    (3, "changelog", "changelog"),
    (4, "template_link", "template"),
    (5, "metrics_page", "metrics_page"),
    (6, "error_explain", "error_explain"),
)

_SEED_BASELINE_DATE = dt.date(2026, 5, 7)  # ISO 2026-W19, Thursday


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _hash_email(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def _yaml_card(piece_id: str, hook: str, anchor: str) -> str:
    return json.dumps(
        {
            "piece_id": piece_id,
            "hook_type": hook,
            "narrative_anchor": anchor,
            "target_persona": "crypto_cmo",
            "data_sources_required": ["dune", "taskon_internal"],
        },
        ensure_ascii=False,
    )


def _ts(date: dt.date, hour: int = 10, minute: int = 0) -> str:
    return dt.datetime.combine(date, dt.time(hour=hour, minute=minute)).isoformat(sep=" ")


# --------------------------------------------------------------------------- #
# Cleanup (for --clean)
# --------------------------------------------------------------------------- #


def clean_seed_rows() -> None:
    """Delete only rows owned by this seed (matched by deterministic keys)."""
    logger.info("cleaning seed rows ...")
    with db.transaction():
        db.execute(
            "DELETE FROM user_journey WHERE user_id IN (SELECT email_hash FROM leads WHERE email IN (?, ?, ?))",
            tuple(SEED_LEAD_EMAILS),
        )
        db.execute("DELETE FROM leads WHERE email IN (?, ?, ?)", tuple(SEED_LEAD_EMAILS))
        db.execute(
            "DELETE FROM newsletter_link_clicks WHERE campaign_id = ?",
            (SEED_NEWSLETTER_CAMPAIGN_ID,),
        )
        db.execute(
            "DELETE FROM newsletter_campaigns WHERE campaign_id = ?",
            (SEED_NEWSLETTER_CAMPAIGN_ID,),
        )
        db.execute(
            "DELETE FROM btouch_daily WHERE snapshot_date = ? AND touchpoint_name LIKE '%' ",
            (_SEED_BASELINE_DATE.isoformat(),),
        )
        # delete metrics_daily for publishings of seed pieces
        db.execute(
            """
            DELETE FROM metrics_daily WHERE publishing_id IN (
                SELECT id FROM publishings WHERE piece_id IN (?, ?, ?)
            )
            """,
            tuple(SEED_PIECE_IDS),
        )
        db.execute("DELETE FROM publishings WHERE piece_id IN (?, ?, ?)", tuple(SEED_PIECE_IDS))
        db.execute("DELETE FROM state_events WHERE piece_id IN (?, ?, ?)", tuple(SEED_PIECE_IDS))
        db.execute("DELETE FROM pieces WHERE id IN (?, ?, ?)", tuple(SEED_PIECE_IDS))
    logger.info("seed cleanup done")


# --------------------------------------------------------------------------- #
# Seeders (each one idempotent)
# --------------------------------------------------------------------------- #


def _seed_pieces() -> None:
    fixtures: list[tuple[str, str, str, str, str]] = [
        (SEED_PIECE_IDS[0], "candidate", "47pct_bot", "假用户代价", "system"),
        (SEED_PIECE_IDS[1], "drafted", "d7_15pct_truth", "信任崩坏", "claude"),
        (SEED_PIECE_IDS[2], "published", "47pct_bot", "假用户代价", "donald"),
    ]
    for piece_id, state, hook, anchor, actor in fixtures:
        existing = db.fetchone("SELECT id FROM pieces WHERE id = ?", (piece_id,))
        if existing:
            logger.info("piece already seeded: %s (state=%s)", piece_id, state)
            continue
        card_yaml = _yaml_card(piece_id, hook, anchor)
        # create() ALWAYS lands the row in state='candidate' (per adapter contract)
        # so for non-candidate fixtures we then update_state to the target.
        db.pieces.create(piece_id, card_yaml, actor=actor)
        if state != "candidate":
            db.pieces.update_state(piece_id, state, actor=actor, notes="seeded")
        logger.info("seeded piece: %s state=%s", piece_id, state)


def _seed_publishings() -> list[int]:
    """Return list of publishing IDs created (or already present) for the published piece."""
    published_piece = SEED_PIECE_IDS[2]
    rows = [
        {
            "piece_id": published_piece,
            "platform": "twitter",
            "external_post_id": "seed-tw-001",
            "postiz_post_id": "seed-postiz-tw-001",
            "published_at": _ts(_SEED_BASELINE_DATE, 9, 0),
            "utm_campaign": published_piece.lower().replace("-", "_"),
            "utm_content": "donald_en",
            "utm_term": "47pct_bot",
        },
        {
            "piece_id": published_piece,
            "platform": "linkedin",
            "external_post_id": "seed-li-001",
            "postiz_post_id": "seed-postiz-li-001",
            "published_at": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=1), 10, 0),
            "utm_campaign": published_piece.lower().replace("-", "_"),
            "utm_content": "taskon_official",
            "utm_term": "47pct_bot",
        },
    ]
    ids: list[int] = []
    for row in rows:
        existing = db.fetchone(
            "SELECT id FROM publishings WHERE platform = ? AND external_post_id = ?",
            (row["platform"], row["external_post_id"]),
        )
        if existing:
            logger.info(
                "publishing already seeded: platform=%s ext_id=%s",
                row["platform"],
                row["external_post_id"],
            )
            ids.append(existing["id"])
            continue
        new_id = db.publishings.upsert(**row)
        ids.append(new_id)
        logger.info("seeded publishing #%d (platform=%s)", new_id, row["platform"])
    return ids


def _seed_metrics(publishing_ids: list[int]) -> None:
    if not publishing_ids:
        logger.warning("no publishing ids to attach metrics to; skipping")
        return
    if len(publishing_ids) < 2:
        publishing_ids = publishing_ids + [publishing_ids[0]]

    fixtures = [
        # twitter: 30m / 24h / 7d
        {
            "publishing_id": publishing_ids[0],
            "snapshot_type": "30m",
            "impressions": 2800,
            "likes": 30,
            "replies": 8,
            "quotes": 4,
            "retweets": 5,
            "bookmarks": 2,
            "profile_clicks": 12,
            "link_clicks": 22,
            "impressions_30m": 2800,
            "engagement_count_30m": 47,
        },
        {
            "publishing_id": publishing_ids[0],
            "snapshot_type": "24h",
            "impressions": 8500,
            "likes": 120,
            "replies": 45,
            "quotes": 23,
            "retweets": 18,
            "bookmarks": 12,
            "profile_clicks": 35,
            "link_clicks": 88,
        },
        {
            "publishing_id": publishing_ids[0],
            "snapshot_type": "7d",
            "impressions": 18500,
            "likes": 245,
            "replies": 72,
            "quotes": 41,
            "retweets": 38,
            "bookmarks": 25,
            "profile_clicks": 73,
            "link_clicks": 178,
        },
        # linkedin: 24h / 7d
        {
            "publishing_id": publishing_ids[1],
            "snapshot_type": "24h",
            "impressions": 3200,
            "likes": 64,
            "replies": 18,
            "shares": 12,
            "link_clicks": 41,
        },
        {
            "publishing_id": publishing_ids[1],
            "snapshot_type": "7d",
            "impressions": 7400,
            "likes": 132,
            "replies": 35,
            "shares": 27,
            "link_clicks": 88,
        },
    ]
    # Idempotency: skip if a row with the same publishing_id + snapshot_type already exists.
    for row in fixtures:
        existing = db.fetchone(
            "SELECT id FROM metrics_daily WHERE publishing_id = ? AND snapshot_type = ?",
            (row["publishing_id"], row["snapshot_type"]),
        )
        if existing:
            logger.info(
                "metrics already seeded: publishing=%s snapshot=%s",
                row["publishing_id"],
                row["snapshot_type"],
            )
            continue
        db.metrics_daily.insert(**row)
        logger.info(
            "seeded metrics: publishing=%s snapshot=%s",
            row["publishing_id"],
            row["snapshot_type"],
        )


def _seed_leads() -> None:
    base_campaign = SEED_PIECE_IDS[2].lower().replace("-", "_")
    fixtures = [
        {
            "email": SEED_LEAD_EMAILS[0],
            "email_hash": _hash_email(SEED_LEAD_EMAILS[0]),
            "first_seen_at": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=2), 14, 30),
            "first_landing_page": "/benchmark-report",
            "first_utm_campaign": base_campaign,
            "first_utm_content": "donald_en",
            "first_utm_term": "47pct_bot",
            "qualified": True,
            "sql_status": "pending",
        },
        {
            "email": SEED_LEAD_EMAILS[1],
            "email_hash": _hash_email(SEED_LEAD_EMAILS[1]),
            "first_seen_at": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=3), 9, 15),
            "first_landing_page": "/free-diagnostic",
            "first_utm_campaign": base_campaign,
            "first_utm_content": "taskon_official",
            "first_utm_term": "47pct_bot",
            "qualified": True,
            "sql_status": "verified",
            "sql_verified_by": "bd_alex",
            "bd_attribution_content_id": SEED_PIECE_IDS[2],
        },
        {
            "email": SEED_LEAD_EMAILS[2],
            "email_hash": _hash_email(SEED_LEAD_EMAILS[2]),
            "first_seen_at": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=4), 22, 0),
            "first_landing_page": "/growth-playbook",
            "first_utm_campaign": base_campaign,
            "first_utm_content": "donald_en",
            "first_utm_term": "47pct_bot",
            "qualified": False,
            "sql_status": "pending",
        },
    ]
    for row in fixtures:
        existing = db.fetchone("SELECT id FROM leads WHERE email = ?", (row["email"],))
        if existing:
            logger.info("lead already seeded: %s", row["email"])
            continue
        db.leads.insert(**row)
        logger.info("seeded lead: %s", row["email"])


def _seed_user_journey() -> None:
    base_campaign = SEED_PIECE_IDS[2].lower().replace("-", "_")
    lead1_hash = _hash_email(SEED_LEAD_EMAILS[0])
    lead2_hash = _hash_email(SEED_LEAD_EMAILS[1])
    lead3_hash = _hash_email(SEED_LEAD_EMAILS[2])

    fixtures = [
        # lead1: impression -> click -> signup
        {
            "user_id": lead1_hash,
            "touchpoint_seq": 1,
            "action": "impression",
            "utm_source": "twitter",
            "utm_medium": "thread",
            "utm_campaign": base_campaign,
            "utm_content": "donald_en",
            "utm_term": "47pct_bot",
            "page_path": "",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE, 9, 5),
        },
        {
            "user_id": lead1_hash,
            "touchpoint_seq": 2,
            "action": "click",
            "utm_source": "twitter",
            "utm_medium": "thread",
            "utm_campaign": base_campaign,
            "utm_content": "donald_en",
            "utm_term": "47pct_bot",
            "page_path": "/benchmark-report",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE, 9, 12),
        },
        {
            "user_id": lead1_hash,
            "touchpoint_seq": 3,
            "action": "signup",
            "utm_source": "twitter",
            "utm_medium": "thread",
            "utm_campaign": base_campaign,
            "utm_content": "donald_en",
            "utm_term": "47pct_bot",
            "page_path": "/benchmark-report",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=2), 14, 30),
        },
        # lead2: impression -> click -> signup (linkedin path)
        {
            "user_id": lead2_hash,
            "touchpoint_seq": 1,
            "action": "impression",
            "utm_source": "linkedin",
            "utm_medium": "post",
            "utm_campaign": base_campaign,
            "utm_content": "taskon_official",
            "utm_term": "47pct_bot",
            "page_path": "",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=1), 10, 5),
        },
        {
            "user_id": lead2_hash,
            "touchpoint_seq": 2,
            "action": "click",
            "utm_source": "linkedin",
            "utm_medium": "post",
            "utm_campaign": base_campaign,
            "utm_content": "taskon_official",
            "utm_term": "47pct_bot",
            "page_path": "/free-diagnostic",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=1), 10, 22),
        },
        {
            "user_id": lead2_hash,
            "touchpoint_seq": 3,
            "action": "signup",
            "utm_source": "linkedin",
            "utm_medium": "post",
            "utm_campaign": base_campaign,
            "utm_content": "taskon_official",
            "utm_term": "47pct_bot",
            "page_path": "/free-diagnostic",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=3), 9, 15),
        },
        # lead3: impression -> signup (no click — direct conversion)
        {
            "user_id": lead3_hash,
            "touchpoint_seq": 1,
            "action": "impression",
            "utm_source": "twitter",
            "utm_medium": "thread",
            "utm_campaign": base_campaign,
            "utm_content": "donald_en",
            "utm_term": "47pct_bot",
            "page_path": "",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE, 9, 18),
        },
        {
            "user_id": lead3_hash,
            "touchpoint_seq": 2,
            "action": "signup",
            "utm_source": "twitter",
            "utm_medium": "thread",
            "utm_campaign": base_campaign,
            "utm_content": "donald_en",
            "utm_term": "47pct_bot",
            "page_path": "/growth-playbook",
            "piece_id": SEED_PIECE_IDS[2],
            "timestamp": _ts(_SEED_BASELINE_DATE + dt.timedelta(days=4), 22, 0),
        },
    ]
    # Idempotency: skip if the (user_id, action, timestamp) tuple already exists.
    for row in fixtures:
        existing = db.fetchone(
            "SELECT id FROM user_journey WHERE user_id = ? AND action = ? AND timestamp = ?",
            (row["user_id"], row["action"], row["timestamp"]),
        )
        if existing:
            logger.info("user_journey already seeded: user=%s action=%s", row["user_id"][:8], row["action"])
            continue
        db.user_journey.insert(**row)
    logger.info("seeded user_journey rows (target=%d)", len(fixtures))


def _seed_newsletter() -> None:
    existing = db.fetchone(
        "SELECT id FROM newsletter_campaigns WHERE campaign_id = ?",
        (SEED_NEWSLETTER_CAMPAIGN_ID,),
    )
    if existing:
        logger.info("newsletter already seeded: %s", SEED_NEWSLETTER_CAMPAIGN_ID)
        return
    db.newsletter_campaigns.insert(
        campaign_id=SEED_NEWSLETTER_CAMPAIGN_ID,
        send_time=_ts(_SEED_BASELINE_DATE, 10, 0),
        subject_a="47% Quest 预算被 Bot 吃 — 你可能在踩这个坑",
        subject_b="一份 Q1 Sybil 真相报告（行业第一份）",
        emails_sent=198,
        opens=52,
        open_rate=0.262,
        clicks=12,
        click_rate=0.061,
        hard_bounces=2,
        soft_bounces=1,
        unsubscribed=1,
    )
    logger.info("seeded newsletter campaign: %s", SEED_NEWSLETTER_CAMPAIGN_ID)


def _seed_btouch() -> None:
    """6 btouch_daily rows — one per touchpoint, same snapshot date."""
    for tp_id, name, _utm_medium in SEED_TOUCHPOINTS:
        existing = db.fetchone(
            "SELECT id FROM btouch_daily WHERE snapshot_date = ? AND touchpoint_id = ?",
            (_SEED_BASELINE_DATE.isoformat(), tp_id),
        )
        if existing:
            logger.info("btouch already seeded: id=%d name=%s", tp_id, name)
            continue
        impressions = 850 - tp_id * 90  # decreasing fixture numbers
        clicks = max(impressions // 20, 1)
        db.btouch_daily.insert(
            snapshot_date=_SEED_BASELINE_DATE.isoformat(),
            touchpoint_id=tp_id,
            touchpoint_name=name,
            impressions=impressions,
            clicks=clicks,
            ctr=round(clicks / impressions, 4),
        )
        logger.info("seeded btouch: id=%d name=%s", tp_id, name)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def seed_all() -> None:
    logger.info("seeding test fixtures into %s", db.path)
    _seed_pieces()
    publishing_ids = _seed_publishings()
    _seed_metrics(publishing_ids)
    _seed_leads()
    _seed_user_journey()
    _seed_newsletter()
    _seed_btouch()
    logger.info("seed complete")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn Marketing Engine · seed test fixtures")
    p.add_argument("--clean", action="store_true", help="delete seed rows before re-seeding")
    p.add_argument("--clean-only", action="store_true", help="only clean; do not re-seed")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        if args.clean or args.clean_only:
            clean_seed_rows()
        if not args.clean_only:
            seed_all()
    except Exception:
        logger.exception("seed_test_data failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
