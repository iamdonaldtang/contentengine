"""Tests for ``lib.yt_metadata`` (T14 · Cowork-primary + LLM fallback).

Coverage:
1. **Cowork yaml exists + valid** → use as-is, no audit file written.
2. **Cowork yaml absent + LLM works** → llm_auto + persists ``yt_metadata_auto.yaml``.
3. **Cowork yaml absent + LLM raises** → fallback + persists ``yt_metadata_fallback.yaml`` + P2 alert.
4. **Cowork yaml present but invalid** (banned phrase) → falls through to LLM (graceful).
5. **LLM output contains banned phrase** → rejected → hard fallback engages.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml


def _reload_yt_metadata() -> Any:
    """Reload lib.yt_metadata so it picks up freshly-monkeypatched llm."""
    for mod_name in ("lib.yt_metadata", "lib.llm_client"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("lib.yt_metadata")


def _make_piece(tmp_path: Path, piece_id: str, *, with_script: bool = True, with_card: bool = True) -> Path:
    """Create a piece_dir under tmp_path with minimal drafts.

    Returns the piece_dir path.
    """
    piece_dir = tmp_path / "drafts" / piece_id
    piece_dir.mkdir(parents=True, exist_ok=True)
    if with_script:
        (piece_dir / "shorts_60s.md").write_text(
            "0:00 反共识钩子:47% Quest 预算被 Bot 吃\n"
            "0:10 数据展开:Q1 平台数据交叉验证\n"
            "0:50 CTA:留邮箱拿 Benchmark PDF\n",
            encoding="utf-8",
        )
    if with_card:
        (piece_dir / "selection_card.yaml").write_text(
            yaml.safe_dump({
                "piece_id": piece_id,
                "hook_type": "47pct_bot",
                "narrative_anchor": "trust_collapse",
                "target_persona": "crypto_cmo",
            }, allow_unicode=True),
            encoding="utf-8",
        )
    (piece_dir / "utm_links.json").write_text(
        json.dumps({
            "youtube": {
                "short_url": "https://l.taskon.xyz/q1-bench-yt",
                "campaign": "2026w19_thread01",
                "content": "donald_en",
                "term": "47pct_bot",
            },
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return piece_dir


# --------------------------------------------------------------------------- #
# Test 1 — Cowork yaml exists, valid → tier 1
# --------------------------------------------------------------------------- #


def test_cowork_yaml_used_as_is(tmp_db: Any, tmp_path: Path) -> None:
    yt_module = _reload_yt_metadata()
    piece_dir = _make_piece(tmp_path, "ytmeta-001")
    (piece_dir / "yt_metadata.yaml").write_text(
        yaml.safe_dump({
            "title": "47% Quest 预算被 Bot 吃 · Q1 数据 | TaskOn",
            "description": "本月 TaskOn 全平台数据交叉:47% claim 来自重复地址。\n\n🔗 https://l.taskon.xyz/q1-bench-yt\n\nTaskOn · Web3 Growth.",
            "privacy": "public",
            "tags": ["web3-marketing", "quest", "anti-sybil"],
            "category_id": 22,
            "not_made_for_kids": True,
        }, allow_unicode=True),
        encoding="utf-8",
    )

    meta = yt_module.load_or_derive("ytmeta-001", piece_dir)
    assert meta.source == "cowork"
    assert meta.title.startswith("47% Quest")
    assert "https://l.taskon.xyz" in meta.description
    assert meta.privacy == "public"
    assert "web3-marketing" in meta.tags

    # No audit files written (Cowork already owns truth).
    assert not (piece_dir / "yt_metadata_auto.yaml").exists()
    assert not (piece_dir / "yt_metadata_fallback.yaml").exists()


# --------------------------------------------------------------------------- #
# Test 2 — Cowork absent, LLM works → tier 2 + audit
# --------------------------------------------------------------------------- #


def test_llm_derives_and_persists_auto_yaml(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yt_module = _reload_yt_metadata()
    piece_dir = _make_piece(tmp_path, "ytmeta-002")

    fake_llm_response = {
        "title": "Bot 吃了 47% 的 Web3 增长预算 · 数据真相",
        "description": (
            "本月 TaskOn 全平台数据交叉验证发现:47% 的 claim 来自重复地址。"
            "Sybil 过滤后的真实 LTV 提升 14×。\n\n"
            "🔗 https://l.taskon.xyz/q1-bench-yt\n\n"
            "TaskOn · Web3 Growth Platform"
        ),
        "privacy": "public",
        "tags": ["web3", "quest", "anti-sybil", "growth", "crypto-marketing"],
        "category_id": 22,
        "not_made_for_kids": True,
    }
    monkeypatch.setattr(
        yt_module.llm, "complete_json",
        lambda system, user, schema_hint=None, **kw: fake_llm_response,
    )

    meta = yt_module.load_or_derive("ytmeta-002", piece_dir)
    assert meta.source == "llm_auto"
    assert meta.title.startswith("Bot 吃了 47%")
    assert "anti-sybil" in meta.tags

    # Audit YAML written (so Donald can inspect).
    audit = piece_dir / "yt_metadata_auto.yaml"
    assert audit.is_file()
    persisted = yaml.safe_load(audit.read_text(encoding="utf-8"))
    assert persisted["source"] == "llm_auto"
    assert persisted["title"] == meta.title

    # Postiz settings shape is correct
    settings = meta.to_postiz_settings()
    assert settings["title"] == meta.title
    assert settings["description"] == meta.description
    assert settings["type"] == "public"
    assert settings["tags"] == list(meta.tags)
    assert settings["notMadeForKids"] is True


# --------------------------------------------------------------------------- #
# Test 3 — LLM raises → tier 3 fallback + P2 alert
# --------------------------------------------------------------------------- #


def test_llm_failure_engages_hard_fallback_and_alerts(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yt_module = _reload_yt_metadata()
    piece_dir = _make_piece(tmp_path, "ytmeta-003")

    def _boom(*args: Any, **kw: Any) -> dict[str, Any]:
        raise yt_module.LLMClientError("simulated MiniMaxi + Anthropic outage")

    monkeypatch.setattr(yt_module.llm, "complete_json", _boom)

    fired: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        yt_module, "alert",
        lambda severity, message, details=None: fired.append((severity, message, details or {})) or True,
    )

    meta = yt_module.load_or_derive("ytmeta-003", piece_dir)
    assert meta.source == "fallback"
    # Hard fallback title pulls from script's first non-timecode line.
    assert "47%" in meta.title or "Quest" in meta.title or "Bot" in meta.title
    assert meta.title.endswith("| TaskOn")
    assert len(meta.title) <= yt_module.TITLE_MAX
    assert "https://l.taskon.xyz/q1-bench-yt" in meta.description
    # Fallback tags derived from selection_card.
    assert "47pct-bot" in meta.tags or "trust-collapse" in meta.tags
    assert "taskon" in meta.tags

    # Audit YAML written under _fallback suffix.
    audit = piece_dir / "yt_metadata_fallback.yaml"
    assert audit.is_file()

    # P2 alert fired.
    p2 = [a for a in fired if a[0] == "P2"]
    assert p2, f"expected P2 alert; fired={fired}"
    assert "fallback" in p2[0][1].lower() or "hard template" in p2[0][1].lower()


# --------------------------------------------------------------------------- #
# Test 4 — Cowork yaml present but invalid → falls through to LLM
# --------------------------------------------------------------------------- #


def test_invalid_cowork_yaml_falls_through_to_llm(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yt_module = _reload_yt_metadata()
    piece_dir = _make_piece(tmp_path, "ytmeta-004")
    # Cowork yaml exists but contains a banned phrase in title.
    (piece_dir / "yt_metadata.yaml").write_text(
        yaml.safe_dump({
            "title": "革命性 Web3 增长方法 | TaskOn",   # banned: "革命性"
            "description": "data data data\nhttps://l.taskon.xyz/x",
            "privacy": "public",
            "tags": ["web3"],
        }, allow_unicode=True),
        encoding="utf-8",
    )

    llm_resp = {
        "title": "Clean LLM title with data · TaskOn",
        "description": "Body.\n\nhttps://l.taskon.xyz/q1-bench-yt\n\nTaskOn.",
        "privacy": "public",
        "tags": ["web3", "growth"],
        "category_id": 22,
        "not_made_for_kids": True,
    }
    monkeypatch.setattr(
        yt_module.llm, "complete_json",
        lambda system, user, schema_hint=None, **kw: llm_resp,
    )

    meta = yt_module.load_or_derive("ytmeta-004", piece_dir)
    # Cowork yaml was rejected → LLM tier won.
    assert meta.source == "llm_auto"
    assert meta.title == "Clean LLM title with data · TaskOn"

    # Cowork yaml STILL exists on disk (we don't auto-delete invalid input).
    assert (piece_dir / "yt_metadata.yaml").is_file()
    # And the auto audit file is also written.
    assert (piece_dir / "yt_metadata_auto.yaml").is_file()


# --------------------------------------------------------------------------- #
# Test 5 — LLM returns banned-phrase output → rejected → hard fallback
# --------------------------------------------------------------------------- #


def test_llm_banned_phrase_output_rejected(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yt_module = _reload_yt_metadata()
    piece_dir = _make_piece(tmp_path, "ytmeta-005")

    bad_llm_resp = {
        "title": "颠覆 Web3 增长 · TaskOn",   # banned: "颠覆"
        "description": "x",
        "privacy": "public",
        "tags": [],
        "category_id": 22,
        "not_made_for_kids": True,
    }
    monkeypatch.setattr(
        yt_module.llm, "complete_json",
        lambda system, user, schema_hint=None, **kw: bad_llm_resp,
    )

    fired: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        yt_module, "alert",
        lambda severity, message, details=None: fired.append((severity, message, details or {})) or True,
    )

    meta = yt_module.load_or_derive("ytmeta-005", piece_dir)
    assert meta.source == "fallback"
    # No banned phrase in resolved title.
    assert "颠覆" not in meta.title
    # P2 still fires (LLM output rejected counts as fallback path).
    assert any(a[0] == "P2" for a in fired), fired
