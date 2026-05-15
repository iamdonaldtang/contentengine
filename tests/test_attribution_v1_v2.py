"""Tests for V1 (first-touch) + V2 (linear) attribution models and the
attribution_model stratification on weekly_aggregates.

Covers PRD §2.7 升级路径 + Metrics doc §4.3.
"""
from __future__ import annotations

import importlib
import json
import sys
from typing import Any

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _reload_attribution() -> Any:
    """Force-reload jobs.attribution_engine so it binds to the fresh tmp_db."""
    if "jobs.attribution_engine" in sys.modules:
        del sys.modules["jobs.attribution_engine"]
    return importlib.import_module("jobs.attribution_engine")


def _seed_piece(tmp_db: Any, piece_id: str, hook: str, anchor: str, platform: str = "twitter") -> int:
    """Insert a piece + one publishing. Returns the publishing id."""
    card = {
        "piece_id": piece_id,
        "hook_type": hook,
        "narrative_anchor": anchor,
        "title": f"title-{piece_id}",
    }
    tmp_db.pieces.create(piece_id, card, actor="test")
    tmp_db.pieces.update_state(piece_id, "published", actor="test")
    return tmp_db.publishings.upsert(
        piece_id=piece_id,
        platform=platform,
        external_post_id=f"ext-{piece_id}",
        utm_campaign=piece_id,
        utm_content="donald_en",
        utm_term=hook,
        published_at="2026-05-06 09:00:00",
    )


def _seed_journey(
    tmp_db: Any,
    user_id: str,
    touchpoints: list[tuple[str, str, str]],
) -> None:
    """Each tuple is (piece_id, action, timestamp_iso)."""
    for piece_id, action, ts in touchpoints:
        tmp_db.user_journey.insert(
            user_id=user_id,
            action=action,
            utm_source="twitter",
            utm_medium="thread",
            utm_campaign=piece_id,
            utm_content="donald_en",
            utm_term="hook",
            piece_id=piece_id,
            timestamp=ts,
        )


def _seed_lead(tmp_db: Any, email: str, user_id: str, first_seen: str) -> int:
    cur = tmp_db.execute(
        """
        INSERT INTO leads (email, email_hash, first_seen_at, first_landing_page)
        VALUES (?, ?, ?, '/benchmark-report')
        """,
        (email, user_id, first_seen),
    )
    return cur.lastrowid or 0


# --------------------------------------------------------------------------- #
# Test 1 — first-touch picks earliest touchpoint piece_id
# --------------------------------------------------------------------------- #


def test_first_touch_picks_earliest(tmp_db: Any) -> None:
    eng = _reload_attribution()

    _seed_piece(tmp_db, "p-A", "47pct_bot", "假用户代价")
    _seed_piece(tmp_db, "p-B", "data_drop", "信任崩坏", platform="linkedin")
    _seed_piece(tmp_db, "p-C", "self_mock", "叙事疲劳")

    user = "cookie_user_1"
    _seed_journey(
        tmp_db,
        user,
        [
            ("p-A", "impression", "2026-05-04 09:15:00"),
            ("p-B", "click", "2026-05-05 09:30:00"),
            ("p-C", "signup", "2026-05-06 14:00:00"),
        ],
    )
    lead_id = _seed_lead(tmp_db, "lead-1@x.com", user, "2026-05-06 14:00:00")

    credited = eng.apply_first_touch(lead_id)
    assert credited == "p-A"

    row = tmp_db.fetchone(
        "SELECT first_touch_piece_id FROM leads WHERE id = ?", (lead_id,)
    )
    assert row is not None
    assert row["first_touch_piece_id"] == "p-A"


# --------------------------------------------------------------------------- #
# Test 2 — linear splits equally; weights sum to 1.0
# --------------------------------------------------------------------------- #


def test_linear_splits_equally(tmp_db: Any) -> None:
    eng = _reload_attribution()

    _seed_piece(tmp_db, "p-A", "47pct_bot", "假用户代价")
    _seed_piece(tmp_db, "p-B", "data_drop", "信任崩坏", platform="linkedin")
    _seed_piece(tmp_db, "p-C", "self_mock", "叙事疲劳")
    _seed_piece(tmp_db, "p-D", "case_study", "PMF缺位", platform="medium")

    user = "cookie_user_2"
    _seed_journey(
        tmp_db,
        user,
        [
            ("p-A", "impression", "2026-05-04 09:00:00"),
            ("p-B", "click", "2026-05-04 10:00:00"),
            ("p-C", "click", "2026-05-05 14:00:00"),
            ("p-D", "signup", "2026-05-06 14:00:00"),
        ],
    )
    lead_id = _seed_lead(tmp_db, "lead-2@x.com", user, "2026-05-06 14:00:00")

    weights = eng.apply_linear(lead_id)
    assert set(weights.keys()) == {"p-A", "p-B", "p-C", "p-D"}
    for w in weights.values():
        assert abs(w - 0.25) < 1e-9
    assert abs(sum(weights.values()) - 1.0) < 1e-9

    row = tmp_db.fetchone(
        "SELECT linear_weights_json FROM leads WHERE id = ?", (lead_id,)
    )
    assert row is not None
    stored = json.loads(row["linear_weights_json"])
    assert abs(sum(stored.values()) - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# Test 3 — single-touchpoint lead: first == last == linear[piece]==1.0
# --------------------------------------------------------------------------- #


def test_single_touchpoint_all_models_agree(tmp_db: Any) -> None:
    eng = _reload_attribution()

    _seed_piece(tmp_db, "p-solo", "solo_hook", "solo_anchor")
    user = "cookie_solo"
    _seed_journey(tmp_db, user, [("p-solo", "signup", "2026-05-06 14:00:00")])
    lead_id = _seed_lead(tmp_db, "solo@x.com", user, "2026-05-06 14:00:00")

    # Run all three via run() with default models tuple.
    eng.run(lead_id=lead_id)

    row = tmp_db.fetchone(
        "SELECT first_utm_campaign, first_touch_piece_id, linear_weights_json "
        "FROM leads WHERE id = ?",
        (lead_id,),
    )
    assert row is not None
    # last-touch stamped its UTMs
    assert row["first_utm_campaign"] == "p-solo"
    # first-touch piece_id stamped
    assert row["first_touch_piece_id"] == "p-solo"
    # linear: one piece, weight 1.0
    weights = json.loads(row["linear_weights_json"])
    assert weights == {"p-solo": 1.0}


# --------------------------------------------------------------------------- #
# Test 4 — weekly_aggregates stratified by attribution_model
# --------------------------------------------------------------------------- #


def test_weekly_aggregates_stratified_by_model(tmp_db: Any) -> None:
    eng = _reload_attribution()

    # Two pieces with different hooks; both publish in 2026W19 (Mon May 4).
    pub_a = _seed_piece(tmp_db, "p-A", "47pct_bot", "假用户代价")
    pub_b = _seed_piece(tmp_db, "p-B", "data_drop", "信任崩坏", platform="linkedin")
    # Bump publish dates into the target week explicitly.
    tmp_db.execute(
        "UPDATE publishings SET published_at = ? WHERE id = ?",
        ("2026-05-05 09:00:00", pub_a),
    )
    tmp_db.execute(
        "UPDATE publishings SET published_at = ? WHERE id = ?",
        ("2026-05-06 09:00:00", pub_b),
    )
    # Metrics so impressions show up in the aggregator.
    tmp_db.metrics_daily.insert(
        publishing_id=pub_a, snapshot_type="7d", impressions=5000, likes=80, link_clicks=60
    )
    tmp_db.metrics_daily.insert(
        publishing_id=pub_b, snapshot_type="7d", impressions=2000, likes=10, link_clicks=20
    )

    # Lead 1: signup journey points at p-A (last-touch path).
    user1 = "cookie_lt"
    _seed_journey(
        tmp_db,
        user1,
        [
            ("p-A", "impression", "2026-05-05 10:00:00"),
            ("p-A", "signup", "2026-05-06 12:00:00"),
        ],
    )
    lead1 = _seed_lead(tmp_db, "lt@x.com", user1, "2026-05-06 12:00:00")

    # Lead 2: first-touch ONLY on p-B (no signup row joining p-B).
    user2 = "cookie_ft"
    _seed_journey(tmp_db, user2, [("p-B", "impression", "2026-05-05 09:30:00")])
    lead2 = _seed_lead(tmp_db, "ft@x.com", user2, "2026-05-06 09:00:00")
    tmp_db.execute(
        "UPDATE leads SET first_touch_piece_id = 'p-B' WHERE id = ?", (lead2,)
    )

    # Lead 3: linear weights spanning both pieces.
    user3 = "cookie_ln"
    lead3 = _seed_lead(tmp_db, "ln@x.com", user3, "2026-05-06 09:00:00")
    tmp_db.execute(
        "UPDATE leads SET linear_weights_json = ? WHERE id = ?",
        (json.dumps({"p-A": 0.5, "p-B": 0.5}), lead3),
    )

    eng.compute_weekly_aggregates("2026W19")

    # Pull leads_attributed counts per (dimension=hook_type, model).
    rows = tmp_db.fetchall(
        """
        SELECT attribution_model, value, leads_attributed
        FROM weekly_aggregates
        WHERE week = ? AND dimension = 'hook_type'
        ORDER BY attribution_model, value
        """,
        ("2026W19",),
    )
    by_model: dict[str, dict[str, int]] = {}
    for r in rows:
        by_model.setdefault(r["attribution_model"], {})[r["value"]] = r["leads_attributed"]

    # last_touch: signup row was on p-A (47pct_bot) → that hook gets >= 1 lead.
    assert by_model.get("last_touch", {}).get("47pct_bot", 0) >= 1
    # first_touch: lead2 stamped at p-B (data_drop) → data_drop bucket has lead2.
    assert by_model.get("first_touch", {}).get("data_drop", 0) >= 1
    # linear: lead3 has weight on BOTH p-A & p-B → each hook bucket counts it.
    linear_buckets = by_model.get("linear", {})
    assert linear_buckets.get("47pct_bot", 0) >= 1
    assert linear_buckets.get("data_drop", 0) >= 1

    # Sanity: the three models produce DIFFERENT count distributions.
    assert by_model.get("last_touch") != by_model.get("first_touch"), (
        "expected last_touch and first_touch leads to differ in stratified aggregates"
    )


# --------------------------------------------------------------------------- #
# Test 5 — --model linear CLI runs only linear path
# --------------------------------------------------------------------------- #


def test_cli_model_linear_only_runs_linear(tmp_db: Any) -> None:
    eng = _reload_attribution()

    _seed_piece(tmp_db, "p-X", "hook_x", "anchor_x")
    user = "cookie_X"
    _seed_journey(
        tmp_db,
        user,
        [
            ("p-X", "impression", "2026-05-04 09:00:00"),
            ("p-X", "signup", "2026-05-06 14:00:00"),
        ],
    )
    lead_id = _seed_lead(tmp_db, "x@x.com", user, "2026-05-06 14:00:00")

    summary = eng.run(lead_id=lead_id, models=("linear",))
    assert summary["models"] == ["linear"]

    row = tmp_db.fetchone(
        "SELECT first_utm_campaign, first_touch_piece_id, linear_weights_json "
        "FROM leads WHERE id = ?",
        (lead_id,),
    )
    assert row is not None
    # last_touch did NOT run → first_utm_campaign stayed NULL
    assert row["first_utm_campaign"] is None
    # first_touch did NOT run → first_touch_piece_id stayed NULL
    assert row["first_touch_piece_id"] is None
    # linear DID run → JSON written
    assert row["linear_weights_json"] is not None
    weights = json.loads(row["linear_weights_json"])
    # Single distinct piece in journey → 1.0
    assert weights == {"p-X": 1.0}


# --------------------------------------------------------------------------- #
# Test 6 — _resolve_models CLI arg parser
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "flag,expected",
    [
        ("all", ("last_touch", "first_touch", "linear")),
        ("last_touch", ("last_touch",)),
        ("first_touch", ("first_touch",)),
        ("linear", ("linear",)),
    ],
)
def test_resolve_models(flag: str, expected: tuple[str, ...]) -> None:
    eng = _reload_attribution()
    assert eng._resolve_models(flag) == expected
