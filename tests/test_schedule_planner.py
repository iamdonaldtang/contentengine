"""Tests for ``jobs.schedule_planner`` (B1 §3.2 错峰 24h+).

Covers four contract guarantees:

1. **Full-platform schedule** — given complete drafts + utm_links + integration
   IDs, every platform gets a plan with the correct ET slot.
2. **Missing utm_links rejects ALL platforms** — UTM is mandatory (B1 §4).
3. **Timezone math** — ET slots correctly convert to UTC under DST.
4. **dry-run** — never calls Postiz, never writes publishings.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _reload_planner() -> Any:
    """Reload jobs.schedule_planner so it binds to the freshly-built tmp_db."""
    for mod_name in ("jobs.schedule_planner",):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("jobs.schedule_planner")


def _seed_piece_drafts(
    drafts_root: Path,
    piece_id: str,
    *,
    with_utm_links: bool = True,
    drop_files: tuple[str, ...] = (),
) -> Path:
    """Create a piece folder with all 5 expected drafts + optional utm_links.json.

    Returns the piece folder path. ``drop_files`` lets tests omit specific
    drafts to exercise the skip-on-missing-draft path.
    """
    piece_dir = drafts_root / piece_id
    piece_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "xthread_final.md": "Hook tweet 1\n\nTweet 2\n\nTweet 3 with link",
        "linkedin_post.md": "LinkedIn long-form text here — 1500-2500 chars.",
        "carousel_10pages.md": "Page 1: hook\nPage 2: data\nPage 10: CTA",
        "medium_long.md": "# Headline\n\nLong-form blog body...",
        "shorts_60s.md": "0:00 hook\n0:10 reveal data\n0:50 CTA",
        # ★ selection_card.yaml: required by jobs.schedule_planner._ensure_piece_in_db
        # bootstrap path (commit 9c8a146). Without this, real-mode run() in tests
        # would raise "piece not in DB and no selection_card.yaml" before reaching
        # the code path under test. Tests that pre-insert the piece via
        # tmp_db.pieces.create still pass — bootstrap is a fallback, not required.
        "selection_card.yaml": f"piece_id: {piece_id}\nhook_type: 47pct_bot\n",
    }
    for fname, body in files.items():
        if fname in drop_files:
            continue
        (piece_dir / fname).write_text(body, encoding="utf-8")
    # ★ shorts_60s.mp4 dummy: schedule_planner now fails yt_shorts/tiktok if
    # the mp4 is missing (Postiz would otherwise throw "TypeError: Invalid URL"
    # at publish time). A 1-byte placeholder is enough — the planner only
    # checks ``Path.is_file()`` to decide whether to sign a media URL.
    # Tests can pass drop_files=("shorts_60s.mp4",) to exercise the
    # missing-media failure path.
    if "shorts_60s.mp4" not in drop_files:
        (piece_dir / "shorts_60s.mp4").write_bytes(b"\x00")
    if with_utm_links:
        utm = {
            "twitter": {"campaign": "2026w19_thread01", "content": "donald_en", "term": "47pct_bot"},
            "linkedin": {"campaign": "2026w19_thread01", "content": "taskon_official", "term": "47pct_bot"},
            "medium":   {"campaign": "2026w19_thread01", "content": "donald_en", "term": "47pct_bot"},
            "youtube":  {"campaign": "2026w19_thread01", "content": "donald_en", "term": "47pct_bot"},
            "tiktok":   {"campaign": "2026w19_thread01", "content": "donald_en", "term": "47pct_bot"},
        }
        (piece_dir / "utm_links.json").write_text(
            json.dumps(utm, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return piece_dir


def _fill_integration_ids(planner_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch _load_config so every platform has a non-empty integration UUID."""
    real_load = planner_mod._load_config

    def _patched_load() -> dict[str, Any]:
        cfg = real_load()
        # Don't clobber user-edited values; fill empties only.
        integrations = (cfg.setdefault("postiz", {}).setdefault("integrations", {}))
        for k in (
            "x_thread", "linkedin_post", "linkedin_carousel",
            "medium_long", "yt_shorts", "tiktok",
        ):
            if not integrations.get(k):
                integrations[k] = f"int-{k}-uuid"
        return cfg

    monkeypatch.setattr(planner_mod, "_load_config", _patched_load)


# --------------------------------------------------------------------------- #
# Test 1 — happy path: all platforms scheduled
# --------------------------------------------------------------------------- #


def test_schedule_planner_dry_run_full_plan(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 6 platforms get a plan; dry-run never calls Postiz; no DB writes."""
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))

    planner = _reload_planner()
    _fill_integration_ids(planner, monkeypatch)

    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    _seed_piece_drafts(drafts, "2026W19-thread01")

    # Spy on postiz.create_post — must NOT be called in dry-run.
    called = {"n": 0}

    def _spy(**_kw: Any) -> dict[str, Any]:
        called["n"] += 1
        return {"posts": [{"id": "should-not-be-used"}]}

    monkeypatch.setattr(planner.postiz, "create_post", _spy)

    summary = planner.run(
        "2026W19-thread01",
        dry_run=True,
        base_monday=dt.date(2026, 5, 18),  # Mon 18 May 2026 anchor
    )

    assert called["n"] == 0, "postiz.create_post must not be called in dry_run"
    # 6 slots in config.yaml — all should be planned, none skipped.
    assert summary["planned"] >= 6
    assert summary["scheduled"] == summary["planned"], (
        f"expected every platform scheduled in dry-run, got {summary}"
    )
    assert summary["skipped"] == 0
    assert summary["failures"] == 0

    # Each result has an ISO scheduled_at and a utm_campaign.
    for r in summary["results"]:
        assert r["status"] == "dry_run"
        assert "scheduled_at" in r and r["scheduled_at"]
        assert r.get("utm_campaign") == "2026w19_thread01"

    # No publishings written in dry-run.
    rows = tmp_db.fetchall("SELECT * FROM publishings WHERE piece_id = ?", ("2026W19-thread01",))
    assert len(rows) == 0


# --------------------------------------------------------------------------- #
# Test 2 — missing utm_links.json → every platform skipped
# --------------------------------------------------------------------------- #


def test_schedule_planner_missing_utm_links_skips_all(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """utm_links.json absent ⇒ every platform gets skip_reason mentioning UTM."""
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    planner = _reload_planner()
    _fill_integration_ids(planner, monkeypatch)

    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    _seed_piece_drafts(drafts, "no-utm-piece", with_utm_links=False)

    plans = planner.build_schedule(
        "no-utm-piece", base_monday=dt.date(2026, 5, 18)
    )
    assert plans, "config should produce at least one slot"
    assert all(p["skip_reason"] and "utm" in p["skip_reason"].lower() for p in plans), (
        f"every plan should skip with utm reason, got {[p['skip_reason'] for p in plans]}"
    )

    # Full run still completes (warning), never calls Postiz.
    called = {"n": 0}
    monkeypatch.setattr(
        planner.postiz, "create_post",
        lambda **_kw: called.__setitem__("n", called["n"] + 1) or {},
    )
    summary = planner.run("no-utm-piece", base_monday=dt.date(2026, 5, 18))
    assert called["n"] == 0
    assert summary["scheduled"] == 0
    assert summary["skipped"] >= 6
    assert summary["status"] == "warning"


# --------------------------------------------------------------------------- #
# Test 3 — ET → UTC math
# --------------------------------------------------------------------------- #


def test_compute_slot_datetime_et_to_utc_dst() -> None:
    """May (EDT, UTC-4) and December (EST, UTC-5) both serialise correctly."""
    planner = _reload_planner()
    ET = ZoneInfo("America/New_York")

    # 2026-05-18 (DST in effect → UTC-4); Tue 09:00 ET = 13:00 UTC.
    tue_slot = planner.compute_slot_datetime(dt.date(2026, 5, 18), weekday=1, hour=9, tz=ET)
    assert tue_slot.year == 2026 and tue_slot.month == 5 and tue_slot.day == 19
    assert tue_slot.hour == 9
    assert tue_slot.astimezone(dt.timezone.utc).hour == 13

    # 2026-12-14 (EST → UTC-5); Tue 09:00 ET = 14:00 UTC.
    tue_winter = planner.compute_slot_datetime(dt.date(2026, 12, 14), weekday=1, hour=9, tz=ET)
    assert tue_winter.astimezone(dt.timezone.utc).hour == 14


# --------------------------------------------------------------------------- #
# Test 4 — real schedule call writes publishings + state_events
# --------------------------------------------------------------------------- #


def test_schedule_planner_writes_publishings_on_create_post(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dry-run: each successful create_post writes publishings + state_events."""
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))

    planner = _reload_planner()
    _fill_integration_ids(planner, monkeypatch)

    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    _seed_piece_drafts(drafts, "happy-piece")

    # Seed a piece row so state_events has a valid FK target.
    tmp_db.pieces.create(
        "happy-piece",
        '{"piece_id":"happy-piece","hook_type":"47pct_bot"}',
        actor="test",
    )
    tmp_db.pieces.update_state("happy-piece", "reviewed", actor="test")

    # Stub Postiz to return a deterministic post_id per call.
    counter = {"i": 0}

    def _fake_create_post(**kw: Any) -> dict[str, Any]:
        counter["i"] += 1
        # Verify the wrapper passes UTC ISO with milliseconds.
        sched = kw["scheduled_at"]
        assert sched.tzinfo is not None, "scheduled_at must be timezone-aware"
        return {"posts": [{"id": f"postiz-post-{counter['i']:03d}"}]}

    monkeypatch.setattr(planner.postiz, "create_post", _fake_create_post)

    summary = planner.run(
        "happy-piece",
        dry_run=False,
        base_monday=dt.date(2026, 5, 18),
    )

    assert summary["status"] == "ok", summary
    assert summary["scheduled"] >= 6
    assert summary["failures"] == 0
    assert counter["i"] == summary["scheduled"]

    pubs = tmp_db.fetchall(
        "SELECT platform, postiz_post_id, utm_campaign FROM publishings WHERE piece_id = ?",
        ("happy-piece",),
    )
    assert len(pubs) == summary["scheduled"]
    platforms = {r["platform"] for r in pubs}
    assert {"x_thread", "linkedin_post", "linkedin_carousel", "medium_long", "yt_shorts", "tiktok"} <= platforms
    for r in pubs:
        assert r["postiz_post_id"] is not None
        assert r["utm_campaign"] == "2026w19_thread01"

    # state_events records the reviewed → scheduled transition.
    events = tmp_db.fetchall(
        "SELECT to_state FROM state_events WHERE piece_id = ? AND actor = ?",
        ("happy-piece", "schedule_planner"),
    )
    assert events, "state_events should have schedule_planner rows"
    assert all(e["to_state"] == "scheduled" for e in events)


# --------------------------------------------------------------------------- #
# Test 5 — CTA URL placeholder injection (2026-05-16 attribution fix)
# --------------------------------------------------------------------------- #


def test_schedule_planner_injects_cta_url_from_placeholder(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """schedule_planner replaces ``{{CTA_URL}}`` in content + yt description with
    the per-(platform, account) URL from utm_links.json — production-shape
    ``{network}_{account}: {long_url, short_url, utm_query}``."""
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    planner = _reload_planner()
    _fill_integration_ids(planner, monkeypatch)
    # Force account map + cta_kind via config patch.
    real_load = planner._load_config

    def _patched_load() -> dict[str, Any]:
        cfg = real_load()
        postiz = cfg.setdefault("postiz", {})
        postiz["accounts"] = {
            "x_thread": "donald_en", "x_short": "donald_en",
            "linkedin_post": "donald_en", "linkedin_carousel": "donald_en",
            "medium_long": "donald_en", "yt_shorts": "donald_en",
            "yt_long": "donald_en", "tiktok": "donald_en",
        }
        postiz["cta_url_kind"] = "long_url"
        return cfg
    monkeypatch.setattr(planner, "_load_config", _patched_load)

    drafts = tmp_path / "drafts"
    piece_dir = drafts / "cta-piece"
    piece_dir.mkdir(parents=True, exist_ok=True)
    # Each platform draft has the placeholder.
    (piece_dir / "xthread_final.md").write_text(
        "Hook tweet 1\n\nTweet 2\n\nFinal CTA -> {{CTA_URL}}", encoding="utf-8"
    )
    (piece_dir / "linkedin_post.md").write_text(
        "LinkedIn body... 评论告诉我 -> {{CTA_URL}}", encoding="utf-8"
    )
    (piece_dir / "carousel_10pages.md").write_text(
        "Page 1\nPage 10 CTA {{CTA_URL}}", encoding="utf-8"
    )
    (piece_dir / "medium_long.md").write_text(
        "# Long-form\n\nBody...\n\nGet diagnostic -> {{CTA_URL}}", encoding="utf-8"
    )
    (piece_dir / "shorts_60s.md").write_text("script narration", encoding="utf-8")
    (piece_dir / "shorts_60s.mp4").write_bytes(b"\x00")
    (piece_dir / "selection_card.yaml").write_text(
        "piece_id: cta-piece\nhook_type: 47pct_bot\n", encoding="utf-8"
    )
    # Production-shape utm_links.json: keys are {network}_{account}.
    utm = {}
    for network in ("twitter", "linkedin", "youtube", "tiktok", "medium"):
        utm[f"{network}_donald_en"] = {
            "long_url": f"https://taskon.xyz/free-diagnostic?utm_source={network}&utm_campaign=cta-piece&utm_content=donald_en",
            "short_url": f"http://l.taskon.xyz/cta-piece-{network}-1",
            "utm_query": f"?utm_source={network}&utm_campaign=cta-piece&utm_content=donald_en",
        }
    (piece_dir / "utm_links.json").write_text(json.dumps(utm), encoding="utf-8")

    tmp_db.pieces.create("cta-piece", '{"piece_id":"cta-piece"}', actor="test")
    tmp_db.pieces.update_state("cta-piece", "reviewed", actor="test")

    # Capture Postiz calls; integration_ids vary (some are real config UUIDs,
    # others are int-<k>-uuid for unset platforms), so we identify each call
    # by the unique content prefix written above.
    captured: list[dict[str, Any]] = []

    def _capture(**kw: Any) -> dict[str, Any]:
        captured.append(kw)
        return {"posts": [{"id": "stub-post-id"}]}
    monkeypatch.setattr(planner.postiz, "create_post", _capture)

    # Stub yt_metadata so we control description shape (with placeholder).
    fake_yt_meta = type("M", (), {
        "source": "test",
        "title": "test title",
        "description": "Hook line\n\nCTA -> {{CTA_URL}}",
        "to_postiz_settings": lambda self: {
            "title": "test title",
            "description": "Hook line\n\nCTA -> {{CTA_URL}}",
            "type": "public",
            "tags": [{"value": "t", "label": "t"}],
            "category": 22,
            "notMadeForKids": True,
        },
    })()
    monkeypatch.setattr(planner, "load_or_derive", lambda *_a, **_kw: fake_yt_meta)

    summary = planner.run("cta-piece", base_monday=dt.date(2026, 5, 18))
    assert summary["scheduled"] >= 6, summary

    def _find_by_content_prefix(prefix: str) -> dict[str, Any]:
        for c in captured:
            if (c.get("content") or "").startswith(prefix):
                return c
        raise AssertionError(f"no Postiz call with content starting with {prefix!r}")

    # LinkedIn content: placeholder replaced with linkedin_donald_en long_url.
    li_call = _find_by_content_prefix("LinkedIn body")
    assert "{{CTA_URL}}" not in li_call["content"]
    assert "utm_source=linkedin" in li_call["content"]
    assert "utm_content=donald_en" in li_call["content"]

    # YouTube extra_settings.description: placeholder replaced with youtube URL.
    yt_call = next(
        c for c in captured
        if (c.get("extra_settings") or {}).get("title") == "test title"
    )
    yt_desc = (yt_call.get("extra_settings") or {}).get("description", "")
    assert "{{CTA_URL}}" not in yt_desc
    assert "utm_source=youtube" in yt_desc

    # Cross-platform: each platform got its OWN network URL (no leakage).
    medium_call = _find_by_content_prefix("# Long-form")
    assert "utm_source=medium" in medium_call["content"]
    assert "utm_source=youtube" not in medium_call["content"]


def test_schedule_planner_cta_fallback_appends_when_no_placeholder(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy markdown without placeholder gets the URL appended (warn-only)."""
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    planner = _reload_planner()
    _fill_integration_ids(planner, monkeypatch)

    real_load = planner._load_config

    def _patched_load() -> dict[str, Any]:
        cfg = real_load()
        cfg.setdefault("postiz", {})["accounts"] = {"linkedin_post": "donald_en"}
        return cfg
    monkeypatch.setattr(planner, "_load_config", _patched_load)

    drafts = tmp_path / "drafts"
    piece_dir = drafts / "legacy-piece"
    piece_dir.mkdir(parents=True, exist_ok=True)
    (piece_dir / "linkedin_post.md").write_text(
        "Legacy content without any placeholder.", encoding="utf-8"
    )
    # Minimum required other files (build_schedule iterates all platforms).
    for f in ("xthread_final.md", "carousel_10pages.md", "medium_long.md", "shorts_60s.md"):
        (piece_dir / f).write_text("stub", encoding="utf-8")
    (piece_dir / "shorts_60s.mp4").write_bytes(b"\x00")
    (piece_dir / "selection_card.yaml").write_text(
        "piece_id: legacy-piece\n", encoding="utf-8"
    )
    (piece_dir / "utm_links.json").write_text(json.dumps({
        "linkedin_donald_en": {
            "long_url": "https://taskon.xyz/x?utm_source=linkedin&utm_campaign=legacy",
            "short_url": "http://l.t/x",
            "utm_query": "?utm_source=linkedin",
        }
    }), encoding="utf-8")

    tmp_db.pieces.create("legacy-piece", '{"piece_id":"legacy-piece"}', actor="test")
    tmp_db.pieces.update_state("legacy-piece", "reviewed", actor="test")

    captured: list[dict[str, Any]] = []

    def _capture(**kw: Any) -> dict[str, Any]:
        captured.append(kw)
        return {"posts": [{"id": "x"}]}
    monkeypatch.setattr(planner.postiz, "create_post", _capture)
    # Don't need yt_metadata for this test path; stub to avoid LLM calls.
    monkeypatch.setattr(planner, "load_or_derive", lambda *_a, **_kw: type("M", (), {
        "source": "test",
        "title": "t",
        "description": "d",
        "to_postiz_settings": lambda self: {"title": "t", "description": "d"},
    })())

    planner.run("legacy-piece", base_monday=dt.date(2026, 5, 18))
    # Find the linkedin_post call by its unique content prefix (other platforms
    # use stub content).
    li_calls = [c for c in captured if (c.get("content") or "").startswith("Legacy content")]
    assert li_calls, f"linkedin post should have fired; got {len(captured)} calls"
    content = li_calls[0]["content"]
    # Fallback appends URL on its own block at the end.
    assert content.startswith("Legacy content without any placeholder.")
    assert "utm_source=linkedin" in content
    assert "utm_campaign=legacy" in content
