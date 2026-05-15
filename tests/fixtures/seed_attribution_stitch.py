"""Seed scenario · anonymous-impression → signed-up-lead cookie stitching.

Models the canonical T2 story from B1 §4.3:

    1. User visits /benchmark-report via Twitter ad (impression #1, anon cookie)
    2. Returns 1 day later via LinkedIn ad (impression #2, same cookie)
    3. Submits the form (signup, same cookie, email captured)

Without stitching, attribution_engine sees only the signup row and credits
everything to the landing page. With stitching (cookie_email_map populated by
ingestion at signup time), the engine unions cookie-keyed impressions with
the email_hash, so first_touch correctly credits the Twitter ad.

Public surface:

    seed_stitch_scenario(tmp_db, *, email=..., cookie_id=..., piece_id=...) -> dict
        Insert all rows for the standard 3-touch scenario. Returns key ids
        for callers to assert against.

    seed_no_stitch_scenario(tmp_db, *, email=..., piece_id=...) -> dict
        Insert leads + signup-only journey, NO cookie_email_map entry.

    seed_multi_cookie_scenario(tmp_db, *, email=..., piece_id=...) -> dict
        Insert 2 cookies for the same email — exercises the plural lookup.

    seed_shared_cookie_scenario(tmp_db, *, piece_id=...) -> dict
        Insert 1 cookie mapped to 2 different emails (device-sharing case).

Run as ``python -m tests.fixtures.seed_attribution_stitch`` against the
live runtime/state.db to support the manual verification command in the
T2 spec:

    docker compose exec engine python -m tests.fixtures.seed_attribution_stitch
    docker compose exec engine python -m jobs.attribution_engine --rerun-stitch

Then SELECT * FROM leads WHERE email LIKE 'stitch_seed_%' should show
``first_touch_piece_id`` populated with the seeded piece.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any


# Canonical piece + campaign used across seeds — change once, propagates
# through tests and the manual verification flow.
DEFAULT_PIECE_ID = "stitch-piece-2026W19"
DEFAULT_CAMPAIGN = "stitch_piece_2026w19"


def _now() -> dt.datetime:
    return dt.datetime.now()


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _ts(t: dt.datetime) -> int:
    return int(t.timestamp())


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _ensure_piece_and_publishings(db: Any, piece_id: str) -> None:
    """Idempotently create a piece + twitter/linkedin publishings rows.

    Safe to call multiple times within the same test — uses INSERT OR IGNORE
    semantics via the pieces adapter and a manual check on publishings.
    """
    if db.pieces.get(piece_id) is None:
        card = {
            "piece_id": piece_id,
            "hook_type": "47pct_bot",
            "narrative_anchor": "trust_collapse",
            "target_persona": "crypto_cmo",
        }
        db.pieces.create(piece_id, json.dumps(card), actor="seed")
        db.pieces.update_state(piece_id, "published", actor="seed")

    # Publishings — one twitter, one linkedin. Both stamped with the SAME
    # utm_campaign so _resolve_piece_id_for_campaign() can map either back.
    existing = db.fetchall(
        "SELECT platform FROM publishings WHERE piece_id = ?", (piece_id,)
    )
    seen_platforms = {r["platform"] for r in existing}
    now = _now()
    if "twitter" not in seen_platforms:
        db.publishings.upsert(
            piece_id=piece_id,
            platform="twitter",
            external_post_id=f"ext-tw-{piece_id}",
            utm_campaign=DEFAULT_CAMPAIGN,
            utm_content="donald_en",
            utm_term="47pct_bot",
            published_at=_iso(now - dt.timedelta(days=4)),
        )
    if "linkedin" not in seen_platforms:
        db.publishings.upsert(
            piece_id=piece_id,
            platform="linkedin",
            external_post_id=f"ext-li-{piece_id}",
            utm_campaign=DEFAULT_CAMPAIGN,
            utm_content="taskon_official",
            utm_term="47pct_bot",
            published_at=_iso(now - dt.timedelta(days=3)),
        )


def _insert_cookie_mapping(db: Any, cookie_id: str, e_hash: str, when: dt.datetime) -> None:
    """Upsert one cookie_email_map row keyed on (cookie_id, email_hash)."""
    ts = _ts(when)
    db.execute(
        """
        INSERT INTO cookie_email_map (cookie_id, email_hash, first_seen, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(cookie_id, email_hash) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (cookie_id, e_hash, ts, ts),
    )


def seed_stitch_scenario(
    db: Any,
    *,
    email: str = "stitch_seed_alice@example.com",
    cookie_id: str = "stitch_cookie_alice_001",
    piece_id: str = DEFAULT_PIECE_ID,
) -> dict[str, Any]:
    """Canonical 3-touch stitch scenario · twitter → linkedin → signup."""
    _ensure_piece_and_publishings(db, piece_id)
    now = _now()
    e_hash = _email_hash(email)

    t_twitter = now - dt.timedelta(days=4)
    t_linkedin = now - dt.timedelta(days=2)
    t_signup = now - dt.timedelta(hours=2)

    # 1 · Anonymous twitter impression (oldest)
    db.user_journey.insert(
        user_id=cookie_id,
        action="impression",
        utm_source="twitter",
        utm_medium="thread",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="donald_en",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        referrer="https://t.co/abc",
        timestamp=_iso(t_twitter),
        raw_data=json.dumps({"src": "seed"}),
    )
    # 2 · Anonymous linkedin impression
    db.user_journey.insert(
        user_id=cookie_id,
        action="impression",
        utm_source="linkedin",
        utm_medium="post",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="taskon_official",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        referrer="https://linkedin.com/feed",
        timestamp=_iso(t_linkedin),
        raw_data=json.dumps({"src": "seed"}),
    )
    # 3 · Signup (still keyed by cookie because the form submission carried
    # the same cookie_id; landing_signup picks user_id = cookie_id || e_hash).
    db.user_journey.insert(
        user_id=cookie_id,
        action="signup",
        utm_source="linkedin",
        utm_medium="post",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="taskon_official",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        referrer="https://linkedin.com/feed",
        timestamp=_iso(t_signup),
        raw_data=json.dumps({"src": "seed"}),
    )

    # Cookie ↔ email_hash stitching row (would normally be written by
    # ingestion at signup time).
    _insert_cookie_mapping(db, cookie_id, e_hash, t_signup)

    # Lead row (would normally be UPSERTed by ingestion).
    db.leads.insert(
        email=email,
        email_hash=e_hash,
        first_seen_at=_iso(t_signup),
        last_seen_at=_iso(t_signup),
        first_landing_page="/benchmark-report",
        # Deliberately leave first_utm_* empty so we can assert the engine
        # writes them based on the stitched journey.
    )

    lead_row = db.fetchone("SELECT id FROM leads WHERE email = ?", (email,))
    return {
        "lead_id": int(lead_row["id"]),
        "email": email,
        "email_hash": e_hash,
        "cookie_id": cookie_id,
        "piece_id": piece_id,
        "campaign": DEFAULT_CAMPAIGN,
    }


def seed_no_stitch_scenario(
    db: Any,
    *,
    email: str = "stitch_seed_bob@example.com",
    piece_id: str = DEFAULT_PIECE_ID,
) -> dict[str, Any]:
    """Lead with ONLY a signup journey row, no cookie mapping."""
    _ensure_piece_and_publishings(db, piece_id)
    now = _now()
    e_hash = _email_hash(email)
    t_signup = now - dt.timedelta(hours=2)

    db.user_journey.insert(
        user_id=e_hash,
        action="signup",
        utm_source="linkedin",
        utm_medium="post",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="taskon_official",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        timestamp=_iso(t_signup),
    )
    db.leads.insert(
        email=email,
        email_hash=e_hash,
        first_seen_at=_iso(t_signup),
        last_seen_at=_iso(t_signup),
        first_landing_page="/benchmark-report",
    )
    lead_row = db.fetchone("SELECT id FROM leads WHERE email = ?", (email,))
    return {
        "lead_id": int(lead_row["id"]),
        "email": email,
        "email_hash": e_hash,
        "piece_id": piece_id,
    }


def seed_multi_cookie_scenario(
    db: Any,
    *,
    email: str = "stitch_seed_carol@example.com",
    cookie_ids: tuple[str, str] = ("carol_cookie_old", "carol_cookie_new"),
    piece_id: str = DEFAULT_PIECE_ID,
) -> dict[str, Any]:
    """One email stitched to TWO cookies (cleared cookies / two devices)."""
    _ensure_piece_and_publishings(db, piece_id)
    now = _now()
    e_hash = _email_hash(email)
    cookie_old, cookie_new = cookie_ids

    t_old_impression = now - dt.timedelta(days=5)
    t_new_impression = now - dt.timedelta(days=1)
    t_signup = now - dt.timedelta(hours=2)

    # Impression from the OLD cookie (twitter) — should become first_touch.
    db.user_journey.insert(
        user_id=cookie_old,
        action="impression",
        utm_source="twitter",
        utm_medium="thread",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="donald_en",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        timestamp=_iso(t_old_impression),
    )
    # Impression from the NEW cookie (linkedin).
    db.user_journey.insert(
        user_id=cookie_new,
        action="impression",
        utm_source="linkedin",
        utm_medium="post",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="taskon_official",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        timestamp=_iso(t_new_impression),
    )
    # Signup keyed by NEW cookie.
    db.user_journey.insert(
        user_id=cookie_new,
        action="signup",
        utm_source="linkedin",
        utm_medium="post",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="taskon_official",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        timestamp=_iso(t_signup),
    )

    # Both cookies map to the same email — old was stitched first, new at signup.
    _insert_cookie_mapping(db, cookie_old, e_hash, t_old_impression)
    _insert_cookie_mapping(db, cookie_new, e_hash, t_signup)

    db.leads.insert(
        email=email,
        email_hash=e_hash,
        first_seen_at=_iso(t_signup),
        last_seen_at=_iso(t_signup),
        first_landing_page="/benchmark-report",
    )
    lead_row = db.fetchone("SELECT id FROM leads WHERE email = ?", (email,))
    return {
        "lead_id": int(lead_row["id"]),
        "email": email,
        "email_hash": e_hash,
        "cookie_old": cookie_old,
        "cookie_new": cookie_new,
        "piece_id": piece_id,
    }


def seed_shared_cookie_scenario(
    db: Any,
    *,
    cookie_id: str = "shared_device_cookie_001",
    email_a: str = "stitch_seed_alice2@example.com",
    email_b: str = "stitch_seed_dave@example.com",
    piece_id: str = DEFAULT_PIECE_ID,
) -> dict[str, Any]:
    """One cookie stitched to TWO emails — shared device.

    Used to verify that ``lookup_cookies_for_email_hash(e_hash_B)`` returns
    the cookie when the cookie is mapped to BOTH A and B (the WHERE clause
    on email_hash is the specificity filter).
    """
    _ensure_piece_and_publishings(db, piece_id)
    now = _now()
    e_hash_a = _email_hash(email_a)
    e_hash_b = _email_hash(email_b)
    t_old = now - dt.timedelta(days=3)
    t_new = now - dt.timedelta(hours=4)

    db.user_journey.insert(
        user_id=cookie_id,
        action="impression",
        utm_source="twitter",
        utm_medium="thread",
        utm_campaign=DEFAULT_CAMPAIGN,
        utm_content="donald_en",
        utm_term="47pct_bot",
        page_path="/benchmark-report",
        timestamp=_iso(t_old),
    )
    # Cookie maps to A (first), then to B (later).
    _insert_cookie_mapping(db, cookie_id, e_hash_a, t_old)
    _insert_cookie_mapping(db, cookie_id, e_hash_b, t_new)
    db.leads.insert(
        email=email_a,
        email_hash=e_hash_a,
        first_seen_at=_iso(t_old),
        last_seen_at=_iso(t_old),
        first_landing_page="/benchmark-report",
    )
    db.leads.insert(
        email=email_b,
        email_hash=e_hash_b,
        first_seen_at=_iso(t_new),
        last_seen_at=_iso(t_new),
        first_landing_page="/benchmark-report",
    )
    return {
        "email_a": email_a,
        "email_b": email_b,
        "email_hash_a": e_hash_a,
        "email_hash_b": e_hash_b,
        "cookie_id": cookie_id,
        "piece_id": piece_id,
    }


# --------------------------------------------------------------------------- #
# Manual CLI · seed against the live runtime/state.db
# --------------------------------------------------------------------------- #


def main() -> int:
    """Insert all four scenarios into the live state.db, idempotent enough
    that re-running adds new (timestamped) rows but doesn't crash.

    Manual verification (per T2 spec):

        docker compose exec engine python -m tests.fixtures.seed_attribution_stitch
        docker compose exec engine python -m jobs.attribution_engine --rerun-stitch
        docker compose exec engine python -c "from lib.db import db; \\
            [print(dict(r)) for r in db.fetchall(\\"SELECT email, first_touch_piece_id FROM leads WHERE email LIKE 'stitch_seed_%'\\")]"
    """
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    logger = logging.getLogger(__name__)

    from lib.db import db

    alice = seed_stitch_scenario(db)
    bob = seed_no_stitch_scenario(db)
    carol = seed_multi_cookie_scenario(db)
    shared = seed_shared_cookie_scenario(db)

    logger.info("seeded alice (stitch-hit): %s", alice)
    logger.info("seeded bob (no-stitch):    %s", bob)
    logger.info("seeded carol (multi-cookie): %s", carol)
    logger.info("seeded shared-device pair: %s", shared)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
