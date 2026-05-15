"""Tests for ``jobs.channel_attribution`` — Markov removal-effect math,
hand-verified 2-channel cases, monotonicity, credit normalization, and
empty-input graceful handling.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest


def _import_module() -> Any:
    if "jobs.channel_attribution" in sys.modules:
        del sys.modules["jobs.channel_attribution"]
    return importlib.import_module("jobs.channel_attribution")


# --------------------------------------------------------------------------- #
# Test 1 — 2-channel simple Markov (computed by hand)
# --------------------------------------------------------------------------- #


def test_two_channel_simple_markov() -> None:
    """Hand-verified 2-channel chain.

    Journeys:
      4 journeys: START -> A -> CONVERSION
      1 journey : START -> A -> NULL
      3 journeys: START -> B -> CONVERSION
      2 journeys: START -> B -> NULL

    Transitions:
      START → A: 0.5, B: 0.5
      A → CONVERSION: 0.8, NULL: 0.2
      B → CONVERSION: 0.6, NULL: 0.4

    Baseline P(START → CONV) = 0.5*0.8 + 0.5*0.6 = 0.70

    Remove A: every visit to A is rerouted to NULL.
      START → A=0.5 redirected to NULL; START → B=0.5 → CONV 0.6
      New conv prob = 0.5 * 0.0 + 0.5 * 0.6 = 0.30
      Removal effect of A = (0.70 - 0.30) / 0.70 ≈ 0.5714

    Remove B: analogously
      New conv prob = 0.5 * 0.8 + 0.5 * 0.0 = 0.40
      Removal effect of B = (0.70 - 0.40) / 0.70 ≈ 0.4286
    """
    mod = _import_module()
    journeys = []
    for _ in range(4):
        journeys.append([mod.START, "twitter/thread", mod.CONVERSION])
    journeys.append([mod.START, "twitter/thread", mod.NULL])
    for _ in range(3):
        journeys.append([mod.START, "linkedin/post", mod.CONVERSION])
    for _ in range(2):
        journeys.append([mod.START, "linkedin/post", mod.NULL])

    transitions = mod.build_transitions(journeys)
    effects, baseline = mod.compute_removal_effects(transitions)

    assert baseline == pytest.approx(0.70, abs=1e-4)
    assert effects["twitter/thread"] == pytest.approx(0.5714, abs=1e-3)
    assert effects["linkedin/post"] == pytest.approx(0.4286, abs=1e-3)


# --------------------------------------------------------------------------- #
# Test 2 — Removal-effect monotonicity (higher-impact channel = larger effect)
# --------------------------------------------------------------------------- #


def test_removal_effect_monotonicity() -> None:
    """The channel that drives more conversions should have a strictly larger
    removal effect than a weaker channel."""
    mod = _import_module()
    journeys = []
    # 'strong' channel: 90 of 100 visits convert.
    for _ in range(90):
        journeys.append([mod.START, "strong/x", mod.CONVERSION])
    for _ in range(10):
        journeys.append([mod.START, "strong/x", mod.NULL])
    # 'weak' channel: 10 of 100 visits convert.
    for _ in range(10):
        journeys.append([mod.START, "weak/y", mod.CONVERSION])
    for _ in range(90):
        journeys.append([mod.START, "weak/y", mod.NULL])

    transitions = mod.build_transitions(journeys)
    effects, baseline = mod.compute_removal_effects(transitions)

    assert baseline > 0
    assert effects["strong/x"] > effects["weak/y"]


# --------------------------------------------------------------------------- #
# Test 3 — Credit shares sum to 1.0 (within tolerance)
# --------------------------------------------------------------------------- #


def test_credit_shares_sum_to_one() -> None:
    mod = _import_module()
    journeys = [
        [mod.START, "a/x", "b/y", mod.CONVERSION],
        [mod.START, "a/x", mod.NULL],
        [mod.START, "b/y", "c/z", mod.CONVERSION],
        [mod.START, "c/z", mod.CONVERSION],
        [mod.START, "a/x", "c/z", mod.CONVERSION],
        [mod.START, "b/y", mod.NULL],
    ]
    transitions = mod.build_transitions(journeys)
    effects, baseline = mod.compute_removal_effects(transitions)
    total_conversions = 4
    rows = mod.attribute_credits(effects, total_conversions)

    assert baseline > 0
    total_share = sum(r["credit_share"] for r in rows)
    assert total_share == pytest.approx(1.0, abs=1e-6)
    total_credit = sum(r["conversion_credit"] for r in rows)
    assert total_credit == pytest.approx(float(total_conversions), abs=1e-6)


# --------------------------------------------------------------------------- #
# Test 4 — Empty journeys → graceful degradation (no crash, zero output)
# --------------------------------------------------------------------------- #


def test_empty_journeys_graceful(tmp_db: Any) -> None:
    """``run()`` over an empty database should NOT crash; it returns status
    'warning' or 'ok' with zero channels and 0 inserted rows.
    """
    mod = _import_module()
    summary = mod.run(month="2026-05", output=None)
    assert summary["channels"] == 0
    assert summary["inserted"] == 0
    assert summary["conversions"] == 0
    # Status should be one of 'ok' / 'warning' (definitely not 'failed').
    assert summary["status"] in ("ok", "warning")
