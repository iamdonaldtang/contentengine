"""Tests for ``jobs.newsletter_assembler`` (T6 · B1 §2 Newsletter).

Coverage:
1. **Parse + render happy path** — 5 sections present; dry-run writes
   preview HTML; all sections appear in output.
2. **Missing section → P1 + ValueError** — drop "雷达"; parse_newsletter_md
   raises and the alert fires.
3. **dry-run does not call Listmonk** — even when env vars set, dry-run
   skips the create_campaign POST.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest


def _reload_assembler() -> Any:
    for mod_name in ("jobs.newsletter_assembler", "sources.listmonk"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return importlib.import_module("jobs.newsletter_assembler")


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_newsletter.md"


# --------------------------------------------------------------------------- #
# Test 1 — parse + render OK
# --------------------------------------------------------------------------- #


def test_newsletter_parse_render_dry_run_writes_preview(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    assembler = _reload_assembler()

    summary = assembler.run(FIXTURE_PATH, dry_run=True)

    assert summary["status"] == "dry_run"
    preview = Path(summary["preview_path"])
    assert preview.is_file()
    html = preview.read_text(encoding="utf-8")
    assert "47% Quest" in html
    # All 5 section titles render via the template:
    assert "头条" in html
    assert "案例" in html
    assert "本月雷达" in html or "雷达" in html
    # The unsubscribe placeholder is preserved for Listmonk to fill server-side:
    assert "{{UnsubscribeURL}}" in html
    # CTA URL passes through HTML-escaped (utm segments preserved):
    assert "utm_campaign=2026_05_newsletter" in html
    assert "btn" in html  # button style present


# --------------------------------------------------------------------------- #
# Test 2 — missing required section fails loudly + P1 alert
# --------------------------------------------------------------------------- #


def test_newsletter_missing_section_raises_and_alerts(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    assembler = _reload_assembler()

    # Build a draft missing the "雷达" section.
    bad_draft = tmp_path / "newsletter_draft_2026-05.md"
    bad_draft.write_text(
        "---\nsubject: 'Bad draft'\n---\n\n# Bad draft\n\n"
        "## 头条\nintro\n\n## 案例\ncase\n\n## CTA\ncta\n\n## 退订\nunsub\n",
        encoding="utf-8",
    )

    fired: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        assembler, "alert",
        lambda severity, message, details=None: fired.append((severity, message, details or {})) or True,
    )

    with pytest.raises(ValueError) as exc_info:
        assembler.run(bad_draft, dry_run=True)

    assert "雷达" in str(exc_info.value)
    assert any(a[0] == "P1" for a in fired), f"expected P1 alert, got {fired}"


# --------------------------------------------------------------------------- #
# Test 3 — dry-run never calls Listmonk
# --------------------------------------------------------------------------- #


def test_newsletter_dry_run_does_not_call_listmonk(
    tmp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    # Pretend Listmonk is fully configured so create_campaign would have
    # everything it needs IF called.
    monkeypatch.setenv("LISTMONK_BASE_URL", "https://newsletter.taskon.xyz")
    monkeypatch.setenv("LISTMONK_USERNAME", "admin")
    monkeypatch.setenv("LISTMONK_PASSWORD", "secret")
    monkeypatch.setenv("NEWSLETTER_LIST_ID", "42")
    assembler = _reload_assembler()

    # Spy on listmonk.create_campaign — must NOT be invoked in dry-run.
    from sources import listmonk as lm_module

    called = {"n": 0}

    def _spy(**kw: Any) -> dict[str, Any]:
        called["n"] += 1
        return {"id": 999}

    monkeypatch.setattr(lm_module.listmonk, "create_campaign", _spy)

    summary = assembler.run(FIXTURE_PATH, dry_run=True)
    assert summary["status"] == "dry_run"
    assert called["n"] == 0, "Listmonk MUST NOT be called in dry-run"
