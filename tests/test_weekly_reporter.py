"""Tests for ``jobs.weekly_reporter`` (T7 · LLM fallback path).

Coverage:
1. **LLM happy path** — mock llm.complete returns a normal report → file
   written to ``runtime/weekly_report_<week>.md`` (no _bare suffix).
2. **LLM all-providers exhausted** — mock raises ``LLMClientError`` →
   fallback writes ``weekly_report_<week>_bare.md`` with structured
   data tables, P1 alert fires, heartbeat status = 'warning'.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest


def _reload_reporter() -> Any:
    for mod_name in ("jobs.weekly_reporter", "jobs.attribution_engine", "lib.llm_client"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("jobs.weekly_reporter")


def _seed_minimal(tmp_db: Any, week: str) -> None:
    """Insert a minimal piece + publishing + metrics row so collect_weekly_data
    finds at least one row to dump in both happy + bare paths."""
    # Inside the iso week 2026W19 = 2026-05-04 (Mon) .. 2026-05-10 (Sun)
    iso_in_week = "2026-05-06 10:00:00"

    tmp_db.pieces.create(
        f"weekly-{week}-001",
        '{"piece_id":"weekly-001","hook_type":"47pct_bot","narrative_anchor":"trust"}',
        actor="test",
    )
    tmp_db.pieces.update_state(f"weekly-{week}-001", "published", actor="test")
    pub_id = tmp_db.publishings.upsert(
        piece_id=f"weekly-{week}-001",
        platform="twitter",
        external_post_id="ext-001",
        utm_campaign=f"weekly_{week.lower()}_001",
        utm_content="donald_en",
        utm_term="47pct_bot",
        published_at=iso_in_week,
    )
    tmp_db.metrics_daily.insert(
        publishing_id=pub_id,
        snapshot_type="7d",
        impressions=1000,
        likes=20,
        link_clicks=5,
        fetched_at=iso_in_week,
    )
    tmp_db.leads.insert(
        email="weekly-lead-1@example.com",
        email_hash="hash-1",
        first_seen_at=iso_in_week,
        first_landing_page="/benchmark-report",
        first_utm_campaign=f"weekly_{week.lower()}_001",
    )


# --------------------------------------------------------------------------- #
# Test 1 — LLM happy path
# --------------------------------------------------------------------------- #


def test_weekly_reporter_llm_happy_path(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    # Re-import so weekly_reporter picks up the new RUNTIME_DIR.
    reporter = _reload_reporter()
    monkeypatch.setattr(reporter, "_RUNTIME_DIR", tmp_path / "runtime")

    week = "2026W19"
    _seed_minimal(tmp_db, week)

    monkeypatch.setattr(
        reporter.llm, "complete",
        lambda system, user, max_tokens=8000, **kw: "# Weekly Report\n\nAll systems green.",
    )

    out_path = reporter.run(week=week)
    assert out_path.is_file()
    assert out_path.name == f"weekly_report_{week}.md", out_path
    body = out_path.read_text(encoding="utf-8")
    assert "All systems green" in body

    hb = tmp_db.fetchall(
        "SELECT status FROM heartbeat WHERE job_name = ? ORDER BY id DESC LIMIT 1",
        ("weekly_reporter",),
    )
    assert hb and hb[0]["status"] == "ok"


# --------------------------------------------------------------------------- #
# Test 2 — LLM exhausted → fallback _bare report
# --------------------------------------------------------------------------- #


def test_weekly_reporter_llm_failure_falls_back_to_bare(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    reporter = _reload_reporter()
    monkeypatch.setattr(reporter, "_RUNTIME_DIR", tmp_path / "runtime")

    week = "2026W19"
    _seed_minimal(tmp_db, week)

    def _boom(system: str, user: str, max_tokens: int = 8000, **kw: Any) -> str:
        raise reporter.LLMClientError("simulated MiniMaxi + Anthropic outage")

    monkeypatch.setattr(reporter.llm, "complete", _boom)

    fired: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        reporter, "alert",
        lambda severity, message, details=None: fired.append((severity, message, details or {})) or True,
    )

    out_path = reporter.run(week=week)

    # Filename suffix _bare must be present so Donald spots the fallback at a glance.
    assert out_path.name == f"weekly_report_{week}_bare.md", out_path
    assert out_path.is_file()
    body = out_path.read_text(encoding="utf-8")
    assert "LLM 不可达" in body or "bare 数据回退" in body
    # The metrics table is present and references the seeded data.
    assert "weekly_aggregates" in body or "新增 leads" in body or "publish_failures" in body

    # P1 alert must fire (so on-call knows LLM is down).
    p1 = [a for a in fired if a[0] == "P1"]
    assert p1, f"expected P1 alert; got {fired}"
    assert "LLM" in p1[0][1] or "fallback" in p1[0][1].lower()

    # Heartbeat status = 'warning' (NOT 'failed' — degraded delivery is the goal).
    hb = tmp_db.fetchall(
        "SELECT status, error_message FROM heartbeat WHERE job_name = ? ORDER BY id DESC LIMIT 1",
        ("weekly_reporter",),
    )
    assert hb and hb[0]["status"] == "warning"
    assert "LLM" in (hb[0]["error_message"] or "")
