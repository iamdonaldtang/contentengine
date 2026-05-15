"""Tests for ``jobs.kol_daily_replier`` (T5 · B1 §6).

Coverage:
1. **Mock X API + LLM happy path** — fetches tweets, LLM produces clean
   drafts, markdown file emitted with Tier A first.
2. **Banned-phrase filter** — LLM returns a draft containing "革命性";
   draft is dropped from the candidate list (server-side guardrail).
3. **<5 candidates → P2 alert** — only 2 candidates produced; status
   becomes 'warning' and a P2 alert fires.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _reload_replier() -> Any:
    for mod_name in (
        "jobs.kol_daily_replier", "sources.twitter_x", "lib.llm_client",
    ):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("jobs.kol_daily_replier")


def _stub_kol_tweets(replier: Any, monkeypatch: pytest.MonkeyPatch, payload: dict[str, list[dict[str, Any]]]) -> None:
    monkeypatch.setattr(replier, "_fetch_kol_tweets", lambda handles: payload)


def _stub_llm_responses(replier: Any, monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]) -> list[int]:
    """Patch llm.complete_json to return ``responses`` in order. Returns a
    list with one int (call count) so callers can read it post-run."""
    counter = [0]

    def _fake_json(*, system: str, user: str, schema_hint: str | None = None, **kw: Any) -> dict[str, Any]:
        if counter[0] >= len(responses):
            from lib.llm_client import LLMClientError
            raise LLMClientError("test ran out of mock LLM responses")
        out = responses[counter[0]]
        counter[0] += 1
        return out

    monkeypatch.setattr(replier.llm, "complete_json", _fake_json)
    return counter


def _make_tweet(text: str, *, tid: str, likes: int = 50) -> dict[str, Any]:
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "id": tid,
        "text": text,
        "created_at": now_iso,
        "public_metrics": {"like_count": likes, "reply_count": 5, "quote_count": 3, "retweet_count": 2},
    }


# --------------------------------------------------------------------------- #
# Test 1 — happy path
# --------------------------------------------------------------------------- #


def test_kol_daily_replier_happy_path(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    replier = _reload_replier()

    _stub_kol_tweets(replier, monkeypatch, {
        "0xngmi":       [_make_tweet("Sybil detection on chain — 47% of Quest claimers", tid="100", likes=120)],
        "hildobby":     [_make_tweet("Dune dashboard shows Perps DEX wash-trading 60%+", tid="101", likes=88)],
        "smyyguy":      [_make_tweet("Quest CAC blew up in Q2, retention is the real problem", tid="103", likes=95)],
        "rchen8":       [_make_tweet("85% of VC-backed tokens underperform after TGE", tid="104", likes=110)],
        "cobie":        [_make_tweet("Crypto narratives keep recycling every 6 months", tid="102", likes=70)],
    })

    _stub_llm_responses(replier, monkeypatch, [
        {
            "tweet_url": "https://twitter.com/0xngmi/status/100",
            "kol_handle": "@0xngmi", "kol_tier": "A",
            "original_tweet_excerpt": "Sybil detection on chain — 47%...",
            "matched_ammo": "ammo_04_trust_collapse.md",
            "reply_draft": "Same playbook here — our Quest data shows 47% Sybil rate at Q1 baseline. Trust collapse is the bottleneck, not LTV.",
            "why_pickable": "Tier A 数据共鸣",
            "risk_note": "无",
        },
        {
            "tweet_url": "https://twitter.com/hildobby/status/101",
            "kol_handle": "@hildobby", "kol_tier": "A",
            "original_tweet_excerpt": "Dune dashboard shows Perps DEX...",
            "matched_ammo": "ammo_05_pmf_missing.md",
            "reply_draft": "Cross-checked your data with TaskOn Quest CTR — same 60% noise on traders, real PMF segment is <15%.",
            "why_pickable": "数据交叉",
            "risk_note": "无",
        },
        {
            "tweet_url": "https://twitter.com/smyyguy/status/103",
            "kol_handle": "@smyyguy", "kol_tier": "A",
            "original_tweet_excerpt": "Quest CAC blew up in Q2...",
            "matched_ammo": "ammo_05_pmf_missing.md",
            "reply_draft": "CAC inflation is real but our Q1 data shows D7 retention split: 12% real vs 0.4% airdrop-only. Filtering matters more than CAC.",
            "why_pickable": "数据反共识，引出 retention 话题",
            "risk_note": "无",
        },
        {
            "tweet_url": "https://twitter.com/rchen8/status/104",
            "kol_handle": "@rchen8", "kol_tier": "A",
            "original_tweet_excerpt": "85% of VC-backed tokens underperform...",
            "matched_ammo": "ammo_03_internal_fracture.md",
            "reply_draft": "Mapped to wallet concentration: 85% underperformance = 60% pre-TGE allocation. We see this in Sybil dashboards weekly.",
            "why_pickable": "数据强化",
            "risk_note": "无",
        },
        {
            "tweet_url": "https://twitter.com/cobie/status/102",
            "kol_handle": "@cobie", "kol_tier": "B",
            "original_tweet_excerpt": "Crypto narratives keep recycling...",
            "matched_ammo": "ammo_06_narrative_fatigue.md",
            "reply_draft": "The cycle averages 5.8 months per narrative in our 2024-25 data. Quest topics follow the same curve — fatigue is structural.",
            "why_pickable": "周期数据",
            "risk_note": "无",
        },
    ])

    summary = replier.run(date="2026-05-13")

    assert summary["status"] == "ok", summary
    assert summary["candidates"] >= 5
    assert summary["tier_a_count"] >= 4
    out_path = Path(summary["path"])
    assert out_path.is_file()

    md = out_path.read_text(encoding="utf-8")
    # Tier A should appear before Tier B in the rendered output.
    a_pos = md.find("@0xngmi")
    b_pos = md.find("@cobie")
    assert 0 < a_pos < b_pos, "Tier A should render before Tier B"


# --------------------------------------------------------------------------- #
# Test 2 — banned-phrase filter
# --------------------------------------------------------------------------- #


def test_kol_daily_replier_drops_banned_phrase(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    replier = _reload_replier()
    _stub_kol_tweets(replier, monkeypatch, {
        "smyyguy": [_make_tweet("Growth shifts in Q2", tid="200", likes=40)],
        "0xCygaar": [_make_tweet("L2 user activity wave", tid="201", likes=35)],
    })

    _stub_llm_responses(replier, monkeypatch, [
        # First: banned phrase "革命性" — must be dropped.
        {
            "tweet_url": "https://twitter.com/smyyguy/status/200",
            "kol_handle": "@smyyguy", "kol_tier": "A",
            "original_tweet_excerpt": "Growth shifts in Q2",
            "matched_ammo": "ammo_05_pmf_missing.md",
            "reply_draft": "革命性的增长机会 — 我们的 Quest 数据显示...",  # banned phrase
            "why_pickable": "增长视角",
            "risk_note": "无",
        },
        # Second: clean
        {
            "tweet_url": "https://twitter.com/0xCygaar/status/201",
            "kol_handle": "@0xCygaar", "kol_tier": "A",
            "original_tweet_excerpt": "L2 user activity wave",
            "matched_ammo": "ammo_05_pmf_missing.md",
            "reply_draft": "L2 actives 上 14% 这周 — but our funnel shows 23% of those are repeat-bot wallets.",
            "why_pickable": "数据交叉",
            "risk_note": "无",
        },
    ])

    summary = replier.run(date="2026-05-13")

    # 1 dropped, 1 kept → 1 final candidate → <5 triggers warning (test 3
    # covers the alert path; here we just check the drop happened).
    assert summary["candidates"] == 1, summary
    out_path = Path(summary["path"])
    md = out_path.read_text(encoding="utf-8")
    assert "革命性" not in md, "banned phrase must be filtered out of the output"
    assert "L2 actives" in md


# --------------------------------------------------------------------------- #
# Test 3 — <5 candidates → P2 alert, status='warning'
# --------------------------------------------------------------------------- #


def test_kol_daily_replier_low_count_fires_p2(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    replier = _reload_replier()
    _stub_kol_tweets(replier, monkeypatch, {
        "milesdeutscher": [_make_tweet("Macro view on crypto winter", tid="300", likes=200)],
        "rchen8":         [_make_tweet("85% VC token underperformance", tid="301", likes=150)],
    })

    _stub_llm_responses(replier, monkeypatch, [
        {
            "tweet_url": "u1", "kol_handle": "@milesdeutscher", "kol_tier": "A",
            "original_tweet_excerpt": "macro view",
            "matched_ammo": "ammo_06_narrative_fatigue.md",
            "reply_draft": "Macro pressure is real but our cohort retention shows the segment paying customers grew 3x in Q1.",
            "why_pickable": "数据反共识",
            "risk_note": "无",
        },
        {
            "tweet_url": "u2", "kol_handle": "@rchen8", "kol_tier": "A",
            "original_tweet_excerpt": "VC dump",
            "matched_ammo": "ammo_03_internal_fracture.md",
            "reply_draft": "Crosscheck on chain: 85% underperformance maps to 60% wallet concentration in pre-TGE allocations.",
            "why_pickable": "数据强化",
            "risk_note": "无",
        },
    ])

    # Capture alert calls.
    fired: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        replier, "alert",
        lambda severity, message, details=None: fired.append((severity, message, details or {})) or True,
    )

    summary = replier.run(date="2026-05-13")
    assert summary["status"] == "warning"
    assert summary["candidates"] == 2
    p2 = [a for a in fired if a[0] == "P2"]
    assert p2, f"expected a P2 alert; got {fired}"
    assert "only 2 candidates" in p2[0][1] or "candidates" in p2[0][1].lower()
