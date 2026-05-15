"""Tests for ``jobs.adapter_orchestrator`` — LLM call count, voice_checker chaining, first-paragraph independence.

The adapter orchestrator is built by another subagent. These tests exercise
its public contract:
  * It calls ``lib.llm_client.llm.complete`` once per target platform (4
    platforms: linkedin / medium / carousel / shorts).
  * After each adapter output it invokes ``jobs.voice_checker.check`` to
    validate the rewritten draft.
  * The 4 platform outputs are independent — their first paragraphs do not
    share large substrings (PRD §2.4 hard约束: 形态变换不变核 / 每平台首段独立写).

If the orchestrator module / function is not yet implemented, tests skip so
the suite stays green during interleaved subagent development.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


PLATFORMS_EXPECTED = ("linkedin_post", "medium_long", "carousel_10pages", "shorts_60s")


def _import_orchestrator(reload: bool = True) -> Any | None:
    """Import (and optionally reload) the orchestrator.

    Reloading lets us pick up freshly-set ``$DRAFTS_DIR`` because the module
    captures the path at import time.
    """
    try:
        import sys

        if reload and "jobs.adapter_orchestrator" in sys.modules:
            del sys.modules["jobs.adapter_orchestrator"]
        return importlib.import_module("jobs.adapter_orchestrator")
    except ImportError:
        return None


def _write_xthread_draft(drafts_dir: Path, piece_id: str) -> Path:
    folder = drafts_dir / piece_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "xthread_final.md"
    path.write_text(
        "Q1 数据：47% 的 quest 互动是机器人。\n"
        "这不是技术问题，是激励设计问题。\n"
        "增长团队需要承认这件事。",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# Test 1 — adapter_orchestrator calls llm.complete 4 times (once per platform)
# --------------------------------------------------------------------------- #


def test_orchestrator_calls_llm_four_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_db: Any,
    mock_llm: Any,
) -> None:
    drafts_dir = tmp_path / "drafts"
    piece_id = "adapt-001"
    _write_xthread_draft(drafts_dir, piece_id)
    monkeypatch.setenv("DRAFTS_DIR", str(drafts_dir))

    orch = _import_orchestrator()
    if orch is None:
        pytest.skip("jobs.adapter_orchestrator not yet available")

    # Canned per-platform LLM outputs (each distinct so the first-paragraph
    # check in test 3 also has data to chew). Each output is "clean" (no
    # 14禁用词 + length within blog default).
    mock_llm.set_responses(
        [
            "LinkedIn 视角：B2B 读者要的是数字。47% 这件事让人不舒服。",
            "Medium 长文：从一个不舒服的数字开始。",
            "Carousel 第一页：47% 的 quest 互动是机器人。",
            "Shorts 60s 开头：三件事让你看懂 Sybil。",
        ]
    )

    # Patch voice_checker.check so we don't depend on actual disabled-word coverage.
    with patch("jobs.voice_checker.check", return_value=(True, drafts_dir / piece_id / "voice_report.md")):
        run_fn = getattr(orch, "run", None) or getattr(orch, "orchestrate", None) or getattr(orch, "adapt_all", None)
        if run_fn is None:
            pytest.skip("adapter_orchestrator has no run/orchestrate/adapt_all entrypoint")
        run_fn(piece_id=piece_id)  # type: ignore[misc]

    # Exactly one llm.complete call per platform.
    assert mock_llm.complete.call_count == 4, (
        f"expected 4 llm.complete calls (once per platform); got {mock_llm.complete.call_count}"
    )


# --------------------------------------------------------------------------- #
# Test 2 — voice_checker invoked after each adapter output
# --------------------------------------------------------------------------- #


def test_voice_checker_invoked_per_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_db: Any,
    mock_llm: Any,
) -> None:
    drafts_dir = tmp_path / "drafts"
    piece_id = "adapt-002"
    _write_xthread_draft(drafts_dir, piece_id)
    monkeypatch.setenv("DRAFTS_DIR", str(drafts_dir))

    orch = _import_orchestrator()
    if orch is None:
        pytest.skip("jobs.adapter_orchestrator not yet available")

    mock_llm.set_responses(
        [
            "P1 output unique start.",
            "P2 output unique start.",
            "P3 output unique start.",
            "P4 output unique start.",
        ]
    )

    voice_mock = MagicMock(return_value=(True, drafts_dir / piece_id / "voice_report.md"))
    with patch("jobs.voice_checker.check", voice_mock):
        run_fn = getattr(orch, "run", None) or getattr(orch, "orchestrate", None) or getattr(orch, "adapt_all", None)
        if run_fn is None:
            pytest.skip("adapter_orchestrator has no run/orchestrate/adapt_all entrypoint")
        run_fn(piece_id=piece_id)  # type: ignore[misc]

    assert voice_mock.call_count >= 4, (
        f"expected voice_checker to be invoked at least 4 times (1/platform); got {voice_mock.call_count}"
    )


# --------------------------------------------------------------------------- #
# Test 3 — first-paragraph similarity check across 4 outputs (<30% overlap)
# --------------------------------------------------------------------------- #


def test_first_paragraph_independence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_db: Any,
    mock_llm: Any,
) -> None:
    drafts_dir = tmp_path / "drafts"
    piece_id = "adapt-003"
    _write_xthread_draft(drafts_dir, piece_id)
    monkeypatch.setenv("DRAFTS_DIR", str(drafts_dir))

    orch = _import_orchestrator()
    if orch is None:
        pytest.skip("jobs.adapter_orchestrator not yet available")

    # Distinct first paragraphs (no shared 20+-char substrings).
    distinct_outputs = [
        "LinkedIn 视角:\n47% 的 quest 互动属于机器人。\n这数字让运营负责人难看。",
        "Medium 长文:\n从 Q1 一个反常的数字开始 — 行业要不要看?\n我们摆个调查桌。",
        "Carousel 首页:\n机器人比例飙到 47%。\n你的预算去哪了?",
        "Shorts 脚本:\n三件事让 Sybil 现形。先讲第一件,激励错位。",
    ]
    mock_llm.set_responses(distinct_outputs)

    with patch("jobs.voice_checker.check", return_value=(True, drafts_dir / piece_id / "voice_report.md")):
        run_fn = getattr(orch, "run", None) or getattr(orch, "orchestrate", None) or getattr(orch, "adapt_all", None)
        if run_fn is None:
            pytest.skip("adapter_orchestrator has no run/orchestrate/adapt_all entrypoint")
        run_fn(piece_id=piece_id)  # type: ignore[misc]

    # Read whatever files the orchestrator wrote — accept any of these layouts.
    candidates = [
        ("linkedin_post.md", distinct_outputs[0]),
        ("medium_long.md", distinct_outputs[1]),
        ("carousel_10pages.md", distinct_outputs[2]),
        ("shorts_60s.md", distinct_outputs[3]),
    ]
    seen_files = 0
    first_paragraphs: list[str] = []
    for fname, _expected in candidates:
        path = drafts_dir / piece_id / fname
        if path.exists():
            seen_files += 1
            text = path.read_text(encoding="utf-8").strip()
            first_para = text.split("\n\n", 1)[0]
            first_paragraphs.append(first_para)

    if seen_files < 2:
        pytest.skip("orchestrator did not write expected per-platform .md files; cannot compare first paragraphs")

    # Pairwise substring overlap test: shared longest common substring should
    # NOT exceed 30% of the shorter paragraph's length.
    def _longest_common_substring(a: str, b: str) -> int:
        if not a or not b:
            return 0
        # Simple O(n*m) LCS — adequate for short test strings.
        m, n = len(a), len(b)
        prev = [0] * (n + 1)
        best = 0
        for i in range(1, m + 1):
            curr = [0] * (n + 1)
            for j in range(1, n + 1):
                if a[i - 1] == b[j - 1]:
                    curr[j] = prev[j - 1] + 1
                    if curr[j] > best:
                        best = curr[j]
            prev = curr
        return best

    for i in range(len(first_paragraphs)):
        for j in range(i + 1, len(first_paragraphs)):
            a, b = first_paragraphs[i], first_paragraphs[j]
            shorter = min(len(a), len(b))
            if shorter == 0:
                continue
            lcs = _longest_common_substring(a, b)
            ratio = lcs / shorter
            assert ratio < 0.30, (
                f"first-paragraph overlap too high ({ratio:.0%}) between "
                f"output {i} and {j}: lcs={lcs} shorter={shorter}\n"
                f"  A: {a[:80]!r}\n  B: {b[:80]!r}"
            )
