"""Tests for ``lib.utm`` + ``jobs.utm_generator``.

Covers:
  * build_utm() output matches PRD §2.3 format
  * append_utm() merges with existing query string
  * parse_utm() round-trips
  * shlink failure → falls back to long URL
  * piece_id normalisation (uppercase→lower, hyphen→underscore)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------- #
# Test 1 — build_utm() output matches PRD §2.3 format
# --------------------------------------------------------------------------- #


def test_build_utm_matches_prd_format() -> None:
    from lib.utm import UTMParams, build_utm

    p = UTMParams(
        source="twitter",
        medium="thread",
        campaign="perps_dex_lies_v1",
        content="donald_en",
        term="47pct_bot",
    )
    q = build_utm(p)
    # Order is canonical: source, medium, campaign, content, term
    assert q.startswith("?")
    assert "utm_source=twitter" in q
    assert "utm_medium=thread" in q
    assert "utm_campaign=perps_dex_lies_v1" in q
    assert "utm_content=donald_en" in q
    assert "utm_term=47pct_bot" in q
    # canonical order
    assert q.index("utm_source") < q.index("utm_medium") < q.index("utm_campaign") < q.index("utm_content") < q.index("utm_term")


# --------------------------------------------------------------------------- #
# Test 2 — append_utm() merges with existing query string
# --------------------------------------------------------------------------- #


def test_append_utm_preserves_existing_query() -> None:
    from lib.utm import UTMParams, append_utm

    p = UTMParams(
        source="linkedin",
        medium="post",
        campaign="2026w19_thread01",
        content="taskon_official",
        term="d7_15pct_truth",
    )
    base = "https://taskon.xyz/benchmark-report?ref=newsletter&lang=en#cta"
    merged = append_utm(base, p)

    # Both the original query keys AND the new utm_* keys present.
    assert "ref=newsletter" in merged
    assert "lang=en" in merged
    assert "utm_source=linkedin" in merged
    assert "utm_campaign=2026w19_thread01" in merged
    # Fragment preserved
    assert merged.endswith("#cta")
    # No duplicated path segments
    assert merged.count("/benchmark-report") == 1


def test_append_utm_replaces_existing_utm_keys() -> None:
    from lib.utm import UTMParams, append_utm

    base = "https://taskon.xyz/x?utm_source=old&utm_medium=old&keep=yes"
    p = UTMParams(
        source="twitter",
        medium="thread",
        campaign="new_campaign",
        content="new_content",
        term="new_term",
    )
    merged = append_utm(base, p)
    assert "utm_source=twitter" in merged
    assert "utm_source=old" not in merged
    assert "utm_medium=thread" in merged
    assert "utm_medium=old" not in merged
    assert "keep=yes" in merged


# --------------------------------------------------------------------------- #
# Test 3 — parse_utm() round-trips
# --------------------------------------------------------------------------- #


def test_parse_utm_roundtrip() -> None:
    from lib.utm import UTMParams, append_utm, parse_utm

    p = UTMParams(
        source="twitter",
        medium="thread",
        campaign="2026w19_thread01",
        content="donald_en",
        term="47pct_bot",
    )
    url = append_utm("https://taskon.xyz/anywhere", p)
    parsed = parse_utm(url)

    assert parsed is not None
    assert parsed.source == "twitter"
    assert parsed.medium == "thread"
    assert parsed.campaign == "2026w19_thread01"
    assert parsed.content == "donald_en"
    assert parsed.term == "47pct_bot"


def test_parse_utm_missing_segment_returns_none() -> None:
    from lib.utm import parse_utm

    # Missing utm_term entirely.
    assert (
        parse_utm("https://x.com/?utm_source=twitter&utm_medium=thread&utm_campaign=c&utm_content=donald_en")
        is None
    )


# --------------------------------------------------------------------------- #
# Test 4 — shlink failure → falls back to long URL
# --------------------------------------------------------------------------- #


def test_shlink_failure_falls_back_to_long_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``lib.shlink.shlink.shorten`` raises, the generator must keep the long URL.

    ``lib.shlink`` is intentionally not implemented in subagent A's scope, so
    we synthesise a fake module here and install it before importing the
    generator.
    """
    import types

    fake_shlink_mod = types.ModuleType("lib.shlink")

    class _BoomShlink:
        def shorten(self, long_url: str, custom_slug: str | None = None) -> str:
            raise RuntimeError("shlink API down")

    fake_shlink_mod.shlink = _BoomShlink()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lib.shlink", fake_shlink_mod)

    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))

    import importlib
    import jobs.utm_generator as ug

    importlib.reload(ug)

    out = ug.generate_utm(
        piece_id="Round-Trip-Demo",
        target_url="https://taskon.xyz/benchmark-report",
        platforms=["twitter"],
        accounts=["donald_en"],
        hook_type="47pct_bot",
    )

    assert "twitter_donald_en" in out
    entry = out["twitter_donald_en"]
    # short_url falls back to long_url (since shlink raised)
    assert entry["short_url"] == entry["long_url"]
    assert "utm_source=twitter" in entry["long_url"]


# --------------------------------------------------------------------------- #
# Test 5 — piece_id normalisation (uppercase→lower, hyphen→underscore)
# --------------------------------------------------------------------------- #


def test_piece_id_normalisation_in_utm_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``utm_campaign`` must be lowercased, with hyphens turned into underscores."""
    # Suppress shlink to keep the test fast and isolated.
    import sys
    import types

    fake_shlink_mod = types.ModuleType("lib.shlink")
    # Module without `shlink` attr → utm_generator detects + warns + skips.
    monkeypatch.setitem(sys.modules, "lib.shlink", fake_shlink_mod)

    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))

    import importlib
    import jobs.utm_generator as ug

    importlib.reload(ug)

    out = ug.generate_utm(
        piece_id="2026W19-Thread01",
        target_url="https://taskon.xyz/x",
        platforms=["TWITTER"],
        accounts=["Donald_EN"],
        hook_type="47PCT_BOT",
    )

    key = next(iter(out))  # "TWITTER_Donald_EN" → key composed lowercase by client; but key isn't the focus
    long_url = out[key]["long_url"]

    # The campaign must be normalised — lowercase + hyphen→underscore.
    assert "utm_campaign=2026w19_thread01" in long_url
    # The source must be lowercase as well.
    assert "utm_source=twitter" in long_url
    # The content (account) is normalised too.
    assert "utm_content=donald_en" in long_url


# --------------------------------------------------------------------------- #
# Bonus — utm_links.json is written to the right path
# --------------------------------------------------------------------------- #


def test_utm_links_json_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke: utm_generator writes a JSON file to ``$DRAFTS_DIR/<piece_id>/utm_links.json``."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "lib.shlink", types.ModuleType("lib.shlink"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))

    import importlib
    import jobs.utm_generator as ug

    importlib.reload(ug)

    ug.generate_utm(
        piece_id="bonus-001",
        target_url="https://taskon.xyz/bonus",
        platforms=["twitter", "linkedin"],
        accounts=["donald_en"],
        hook_type="hook",
    )

    out_path = tmp_path / "drafts" / "bonus-001" / "utm_links.json"
    assert out_path.exists()
    data: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
    assert "twitter_donald_en" in data
    assert "linkedin_donald_en" in data
