"""Tests for ``jobs.voice_checker`` — disabled-word detection, length, CTA, jieba mixing."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _write_draft(drafts_dir: Path, piece_id: str, platform: str, content: str) -> Path:
    """Write a draft to <drafts_dir>/<piece_id>/<platform>_final.md and return the path."""
    folder = drafts_dir / piece_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{platform}_final.md"
    path.write_text(content, encoding="utf-8")
    return path


def _run_check(
    monkeypatch: pytest.MonkeyPatch,
    drafts_dir: Path,
    piece_id: str,
    platform: str,
) -> tuple[bool, Path]:
    """Run voice_checker.check after rebinding $DRAFTS_DIR."""
    monkeypatch.setenv("DRAFTS_DIR", str(drafts_dir))
    # Reload so the module re-reads $DRAFTS_DIR on import.
    import importlib
    import jobs.voice_checker as vc

    importlib.reload(vc)
    return vc.check(piece_id=piece_id, platform=platform)


# --------------------------------------------------------------------------- #
# Test 1 — 5 samples with known violations → 100% detect rate
# --------------------------------------------------------------------------- #


VIOLATION_SAMPLES: list[tuple[str, str, str]] = [
    # (piece_id, platform, content)
    (
        "viol-01",
        "x_thread",
        "我们提供全方位的增长方案。\n形成完整闭环。\n颠覆传统 quest 模式。",
    ),
    (
        "viol-02",
        "linkedin_post",
        "本方案的价值赋能正是核心抓手，配合生态闭环实现数字化转型。" * 2,
    ),
    (
        "viol-03",
        "yt_shorts",
        "干货满满！史上最强 quest 玩法，一招搞定。",
    ),
    (
        "viol-04",
        "x_short",
        "Let's dive into the alpha. 立刻点击链接。",
    ),
    (
        "viol-05",
        "blog",
        "在当今快速发展的 Web3 行业，本方案显著提升用户增长，综上所述效果惊人。",
    ),
]


@pytest.mark.parametrize("piece_id,platform,content", VIOLATION_SAMPLES)
def test_violation_samples_all_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    piece_id: str,
    platform: str,
    content: str,
) -> None:
    drafts_dir = tmp_path / "drafts"
    _write_draft(drafts_dir, piece_id, platform, content)
    passed, report = _run_check(monkeypatch, drafts_dir, piece_id, platform)

    assert passed is False, f"{piece_id} ({platform}) unexpectedly passed: {report.read_text(encoding='utf-8')[:400]}"
    text = report.read_text(encoding="utf-8")
    assert "FAIL" in text
    assert "Disabled Words" in text


# --------------------------------------------------------------------------- #
# Test 2 — 5 clean samples → 0 false positives
# --------------------------------------------------------------------------- #


CLEAN_SAMPLES: list[tuple[str, str, str]] = [
    (
        "clean-01",
        "x_thread",
        "今天看到一个数字：47% 的 quest 预算被机器人吃掉。\n这不是技术问题，是激励设计问题。",
    ),
    (
        "clean-02",
        "linkedin_post",
        "Q1 数据：30 家 Perps DEX 平均机器人占比 38%。\n大家心照不宣，但很少有人摆到桌面上。\n这值得聊。",
    ),
    (
        "clean-03",
        "yt_shorts",
        "三件事让你看懂 Sybil。第一，付费拉新和拉羊毛长得一样。",
    ),
    (
        "clean-04",
        "x_short",
        "47% 这个数字会让人不舒服，但不舒服的数字往往是真的。",
    ),
    (
        "clean-05",
        "blog",
        "把机器人剔除之后，D7 留存只剩 15%。\n这数字读起来不光鲜，但更接近现实。\n增长团队需要的是更接近现实的版本，不是更光鲜的版本。",
    ),
]


@pytest.mark.parametrize("piece_id,platform,content", CLEAN_SAMPLES)
def test_clean_samples_no_false_positives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    piece_id: str,
    platform: str,
    content: str,
) -> None:
    drafts_dir = tmp_path / "drafts"
    _write_draft(drafts_dir, piece_id, platform, content)
    passed, report = _run_check(monkeypatch, drafts_dir, piece_id, platform)

    text = report.read_text(encoding="utf-8")
    assert passed is True, (
        f"{piece_id} ({platform}) clean sample unexpectedly failed: {text[:400]}"
    )
    assert "PASS" in text


# --------------------------------------------------------------------------- #
# Test 3 — Length over limit fails the check
# --------------------------------------------------------------------------- #


def test_length_over_limit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drafts_dir = tmp_path / "drafts"
    # x_short has length_max=280; build a clean (no禁用词) but too-long string.
    content = "一" * 350  # 350 chars > 280
    _write_draft(drafts_dir, "len-over-01", "x_short", content)

    passed, report = _run_check(monkeypatch, drafts_dir, "len-over-01", "x_short")
    text = report.read_text(encoding="utf-8")
    assert passed is False
    assert "Length: FAIL" in text


# --------------------------------------------------------------------------- #
# Test 4 — CTA ratio over limit fails
# --------------------------------------------------------------------------- #


def test_cta_ratio_over_limit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drafts_dir = tmp_path / "drafts"
    # x_short has cta_ratio_max=0.10. Cram CTA patterns into a short post —
    # "立即" (2) + "点击" (2) + "Click here" (10) = 14 / ~40 ≈ 35%.
    # No length-disqualifier on this short text. We avoid v1_universal "立即"
    # — wait, "立即" is NOT in v1_universal ("立刻" is), so it's CTA-only.
    content = "立即点击 Click here Sign up Learn more"  # mostly CTA
    _write_draft(drafts_dir, "cta-over-01", "x_short", content)
    passed, report = _run_check(monkeypatch, drafts_dir, "cta-over-01", "x_short")
    text = report.read_text(encoding="utf-8")
    assert passed is False
    assert "CTA Ratio: FAIL" in text


# --------------------------------------------------------------------------- #
# Test 5 — jieba tokenizer handles mixed CJK + English without crashing
# --------------------------------------------------------------------------- #


def test_jieba_mixed_cjk_english(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drafts_dir = tmp_path / "drafts"
    # Heavy mixing — jieba.cut must not raise. Contains a known violation
    # ("赋能") to make sure detection still works through the mixing.
    content = (
        "Q1 链上数据 shows that 47% of activity 是 bot driven —— TaskOn 的 anti-Sybil "
        "module 在这场博弈里是诚实观察者，不是赋能 vendor。"
    )
    _write_draft(drafts_dir, "mixed-01", "x_thread", content)
    passed, report = _run_check(monkeypatch, drafts_dir, "mixed-01", "x_thread")

    text = report.read_text(encoding="utf-8")
    assert passed is False  # "赋能" should still be caught
    assert "赋能" in text
