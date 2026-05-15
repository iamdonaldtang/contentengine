"""Tests for ``jobs.ab_aggregator`` — winner detection, multi-arm classification,
small-sample noise, baseline selection, lift math.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest


def _import_module() -> Any:
    if "jobs.ab_aggregator" in sys.modules:
        del sys.modules["jobs.ab_aggregator"]
    return importlib.import_module("jobs.ab_aggregator")


def _seed_arm(
    tmp_db: Any,
    *,
    campaign: str,
    term: str,
    publishings: int,
    impressions_each: int,
    clicks_each: int,
    published_at: str = "2026-05-10 09:00:00",
) -> None:
    """Insert ``publishings`` rows for one arm plus matching metrics_daily rows
    at snapshot_type='7d'."""
    piece_id = f"piece-{campaign}-{term}"
    # Some tests reuse the same piece; create_if_missing.
    existing = tmp_db.fetchone("SELECT id FROM pieces WHERE id = ?", (piece_id,))
    if not existing:
        tmp_db.pieces.create(piece_id, '{"piece_id": "' + piece_id + '"}', actor="test")
    for i in range(publishings):
        pub_id = tmp_db.publishings.upsert(
            piece_id=piece_id,
            platform="twitter",
            external_post_id=f"ext-{campaign}-{term}-{i}",
            utm_campaign=campaign,
            utm_term=term,
            published_at=published_at,
        )
        tmp_db.metrics_daily.insert(
            publishing_id=pub_id,
            snapshot_type="7d",
            impressions=impressions_each,
            link_clicks=clicks_each,
        )


# --------------------------------------------------------------------------- #
# Test 1 — 2-arm clear winner detection
# --------------------------------------------------------------------------- #


def test_two_arm_winner(tmp_db: Any) -> None:
    """A clear 2x lift on a high-volume arm should be flagged as 'winner'."""
    mod = _import_module()
    # Baseline arm: 60 publishings, each with 1000 impressions and 20 clicks (CTR 2%).
    _seed_arm(tmp_db, campaign="camp1", term="hook_A",
              publishings=60, impressions_each=1000, clicks_each=20)
    # Challenger: 60 publishings, each with 1000 impressions and 40 clicks (CTR 4%).
    _seed_arm(tmp_db, campaign="camp1", term="hook_B",
              publishings=60, impressions_each=1000, clicks_each=40)

    rows = mod.compute_ab_results("2026-05", min_samples=30)
    by_term = {r["utm_term"]: r for r in rows if r["utm_campaign"] == "camp1"}

    # Baseline = whichever has higher sample_size (tied at 60 → tiebreak alphabetical = hook_A).
    assert by_term["hook_A"]["verdict"] == "baseline"
    assert by_term["hook_B"]["verdict"] == "winner", by_term["hook_B"]
    # Lift should be ~+100% (CTR doubled).
    assert by_term["hook_B"]["ctr_lift_vs_baseline"] > 0.5
    # p_value should be very small (huge sample, 2x effect).
    assert by_term["hook_B"]["p_value"] is not None
    assert by_term["hook_B"]["p_value"] < 0.001


# --------------------------------------------------------------------------- #
# Test 2 — 3-arm with significant + directional
# --------------------------------------------------------------------------- #


def test_three_arm_mixed(tmp_db: Any) -> None:
    """One arm significantly worse, one arm slightly better (directional)."""
    mod = _import_module()
    # Baseline: 50 pubs, 200 impr each, 6 clicks → CTR 3%, total 10000 impr / 300 clicks.
    _seed_arm(tmp_db, campaign="camp2", term="armA",
              publishings=50, impressions_each=200, clicks_each=6)
    # Significantly worse: 50 pubs, 200 impr each, 2 clicks → CTR 1%, total 10000/100.
    _seed_arm(tmp_db, campaign="camp2", term="armB",
              publishings=50, impressions_each=200, clicks_each=2)
    # Slight: 35 pubs, 200 impr each, 7 clicks per pub → 7000 impr / 245 clicks → CTR 3.5%.
    # vs baseline 3% on 10000 impr: pooled z≈1.82, two-sided p≈0.069 → directional.
    _seed_arm(tmp_db, campaign="camp2", term="armC",
              publishings=35, impressions_each=200, clicks_each=7)

    rows = mod.compute_ab_results("2026-05", min_samples=30)
    by_term = {r["utm_term"]: r for r in rows if r["utm_campaign"] == "camp2"}

    assert by_term["armA"]["verdict"] == "baseline"
    assert by_term["armB"]["verdict"] == "loser", by_term["armB"]
    # armC is at most a slight lift; should NOT be 'winner', could be 'noise' or 'directional'
    assert by_term["armC"]["verdict"] in ("directional", "noise"), by_term["armC"]


# --------------------------------------------------------------------------- #
# Test 3 — small samples => noise verdict
# --------------------------------------------------------------------------- #


def test_small_sample_marked_noise(tmp_db: Any) -> None:
    """Even a 10x CTR delta should be 'noise' when n<30 per arm."""
    mod = _import_module()
    _seed_arm(tmp_db, campaign="camp3", term="x",
              publishings=5, impressions_each=100, clicks_each=1)
    _seed_arm(tmp_db, campaign="camp3", term="y",
              publishings=5, impressions_each=100, clicks_each=10)

    rows = mod.compute_ab_results("2026-05", min_samples=30)
    challengers = [r for r in rows if r["utm_campaign"] == "camp3" and r["verdict"] != "baseline"]
    assert challengers, "expected at least one non-baseline arm"
    for r in challengers:
        assert r["verdict"] == "noise", r


# --------------------------------------------------------------------------- #
# Test 4 — baseline selection picks largest sample
# --------------------------------------------------------------------------- #


def test_baseline_picks_largest_sample(tmp_db: Any) -> None:
    """The arm with the most publishings should be baseline, regardless of CTR."""
    mod = _import_module()
    # arm 'big' has more publishings → baseline.
    _seed_arm(tmp_db, campaign="camp4", term="big",
              publishings=80, impressions_each=1000, clicks_each=10)
    _seed_arm(tmp_db, campaign="camp4", term="small",
              publishings=40, impressions_each=1000, clicks_each=50)

    rows = mod.compute_ab_results("2026-05", min_samples=30)
    by_term = {r["utm_term"]: r for r in rows if r["utm_campaign"] == "camp4"}
    assert by_term["big"]["verdict"] == "baseline"
    # 'small' has higher CTR → winner (5% vs 1%).
    assert by_term["small"]["verdict"] == "winner"


# --------------------------------------------------------------------------- #
# Test 5 — lift math is correct
# --------------------------------------------------------------------------- #


def test_lift_calculation(tmp_db: Any) -> None:
    """Lift = (arm_ctr / baseline_ctr) - 1. Check the math."""
    mod = _import_module()
    # baseline CTR 5%, challenger CTR 6%  → lift = +20%
    _seed_arm(tmp_db, campaign="camp5", term="base",
              publishings=50, impressions_each=1000, clicks_each=50)
    _seed_arm(tmp_db, campaign="camp5", term="chal",
              publishings=50, impressions_each=1000, clicks_each=60)

    rows = mod.compute_ab_results("2026-05", min_samples=30)
    by_term = {r["utm_term"]: r for r in rows if r["utm_campaign"] == "camp5"}
    chal = by_term["chal"]
    expected_lift = (0.06 / 0.05) - 1.0  # = 0.20
    assert chal["ctr_lift_vs_baseline"] == pytest.approx(expected_lift, abs=1e-6)
