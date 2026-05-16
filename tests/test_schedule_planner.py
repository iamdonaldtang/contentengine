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
