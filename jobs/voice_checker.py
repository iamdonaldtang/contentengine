"""Voice Checker · PRD §2.2.

Mechanical pre-publish gate that flags 14-word v1 禁用词 + 长度 + CTA 占比 on
every platform-specific draft. Runs both as a standalone CLI and as an
in-process call from ``jobs.adapter_orchestrator``.

Input  : ``runtime/drafts/<piece_id>/<platform>_final.md``
Output : ``runtime/drafts/<piece_id>/voice_report.md``

Design (Prompt_AI系统化编程_v1.md §7):
  * No silent failures — every branch logs or raises.
  * No hard-coded paths — runtime root resolved from ``$DRAFTS_DIR`` (env)
    falling back to ``engine/runtime/drafts``.
  * Full type hints (PEP 604 ``X | None``).
  * Pure ``logging``; no ``print``.
  * SQLite access (when needed) via ``lib.db.db`` only.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jieba
import yaml
from dotenv import load_dotenv

load_dotenv(override=False)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR") or (_ENGINE_ROOT / "runtime" / "drafts"))
_CONFIG_DIR = _ENGINE_ROOT / "config"
_DISABLED_WORDS_YAML = _CONFIG_DIR / "voice_disabled_words.yaml"
_CONFIG_YAML = _ENGINE_ROOT / "config.yaml"

# Adapter-emitted platform key  →  canonical key in config.yaml `voice_checker.platforms`.
# Bug fix 2026-05-14: adapter_orchestrator names its outputs after the file
# stem (e.g. ``medium_long.md`` → platform=``medium_long``), but the limits
# table in ``config.yaml`` uses the algorithm-anchored names (B1 §2).
# Without this map every adapter output fell through to "blog" defaults
# (length_max=3000), silently passing carousel/shorts overlength runs.
_PLATFORM_ALIAS: dict[str, str] = {
    "medium_long": "blog",
    "carousel_10pages": "linkedin_carousel",
    "shorts_60s": "yt_shorts",
    # Pass-through aliases for safety; also catches a few variants the
    # adapter (or hand-written tests) might emit.
    "xthread": "x_thread",
    "x_thread_final": "x_thread",
    "linkedin": "linkedin_post",
    "youtube_shorts": "yt_shorts",
    "youtube_long": "yt_long",
}


def _canonical_platform(platform: str) -> str:
    """Resolve adapter-emitted platform key → canonical config.yaml key.

    Returns the input unchanged when no alias is registered, so users who
    already pass canonical names (e.g. ``"x_thread"``) are unaffected.
    """
    return _PLATFORM_ALIAS.get(platform, platform)


@dataclass
class VoiceConfig:
    universal: list[str]
    b2b_extension: list[str]
    youtube_extension: list[str]
    ai_smell_phrases: list[str]
    cta_patterns: list[str]
    platform_limits: dict[str, dict[str, float]]

    def disabled_words_for(self, platform: str) -> list[str]:
        """Return the merged禁用词 list for ``platform`` (case-preserving)."""
        words = list(self.universal) + list(self.ai_smell_phrases)
        if platform.startswith("linkedin") or platform == "blog" or platform == "medium_long":
            words.extend(self.b2b_extension)
        if platform.startswith("yt") or platform == "youtube" or platform == "shorts_60s":
            words.extend(self.youtube_extension)
        # de-dupe preserving order
        seen: set[str] = set()
        out: list[str] = []
        for w in words:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out


def _load_voice_config() -> VoiceConfig:
    """Load disabled words + platform limits. Raise if config files missing."""
    if not _DISABLED_WORDS_YAML.is_file():
        raise FileNotFoundError(f"voice_disabled_words.yaml not found at {_DISABLED_WORDS_YAML}")
    if not _CONFIG_YAML.is_file():
        raise FileNotFoundError(f"config.yaml not found at {_CONFIG_YAML}")

    with _DISABLED_WORDS_YAML.open("r", encoding="utf-8") as f:
        words_doc: dict[str, Any] = yaml.safe_load(f) or {}
    with _CONFIG_YAML.open("r", encoding="utf-8") as f:
        main_doc: dict[str, Any] = yaml.safe_load(f) or {}

    platforms_limits = (
        main_doc.get("voice_checker", {}).get("platforms", {}) if isinstance(main_doc, dict) else {}
    )
    if not platforms_limits:
        logger.warning("voice_checker.platforms missing in config.yaml; using empty limits")

    return VoiceConfig(
        universal=list(words_doc.get("v1_universal") or []),
        b2b_extension=list(words_doc.get("b2b_extension") or []),
        youtube_extension=list(words_doc.get("youtube_extension") or []),
        ai_smell_phrases=list(words_doc.get("ai_smell_phrases") or []),
        cta_patterns=list(words_doc.get("cta_patterns") or []),
        platform_limits=platforms_limits,
    )


# --------------------------------------------------------------------------- #
# Result dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class WordHit:
    word: str
    line_no: int
    snippet: str


@dataclass
class CheckResult:
    piece_id: str
    platform: str
    text_length: int
    length_max: int | None
    length_pass: bool
    cta_ratio: float
    cta_ratio_max: float | None
    cta_pass: bool
    disabled_hits: list[WordHit] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.disabled_hits and self.length_pass and self.cta_pass

    @property
    def violation_count(self) -> int:
        v = len(self.disabled_hits)
        if not self.length_pass:
            v += 1
        if not self.cta_pass:
            v += 1
        return v


# --------------------------------------------------------------------------- #
# Core analysis
# --------------------------------------------------------------------------- #


def _strip_markdown(text: str) -> str:
    """Coarse strip of markdown markers so length / CTA reflect human text.

    We keep this conservative: drop code fences, image alt syntax, bracket
    decorations, and frontmatter blocks. Headings keep their text.
    """
    if not text:
        return ""
    # Remove fenced code blocks ```...```
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # Remove YAML frontmatter
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    # Strip image syntax ![alt](url) → alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Strip link syntax [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Drop heading hashes / blockquote markers / list bullets at line start
    text = re.sub(r"^[ \t]*(#{1,6}\s+|>\s+|[-*+]\s+|\d+\.\s+)", "", text, flags=re.MULTILINE)
    return text


def _find_disabled_hits(lines: list[str], words: list[str]) -> list[WordHit]:
    """Locate every disabled-word occurrence across ``lines``.

    Strategy: per line, jieba-tokenize the Chinese portion AND do
    case-insensitive substring search for multi-char English phrases. We
    deliberately do both because some禁用词 are short Chinese words that may
    be subsumed by jieba into longer compounds.
    """
    hits: list[WordHit] = []
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        line_lower = raw.lower()
        # jieba tokenization — informational; primary detection is substring.
        try:
            tokens = list(jieba.cut(raw, cut_all=False))
        except Exception:
            logger.exception("jieba.cut failed on line %s; falling back to substring", line_no)
            tokens = []
        token_set = {t for t in tokens if t.strip()}

        for word in words:
            if not word:
                continue
            word_lower = word.lower()
            occurs = (word in raw) or (word_lower in line_lower) or (word in token_set)
            if occurs:
                snippet = raw.strip()
                if len(snippet) > 80:
                    # center the snippet around the match if possible
                    idx = line_lower.find(word_lower)
                    if idx >= 0:
                        start = max(0, idx - 20)
                        end = min(len(snippet), idx + len(word) + 40)
                        snippet = ("…" if start > 0 else "") + snippet[start:end] + (
                            "…" if end < len(raw.strip()) else ""
                        )
                    else:
                        snippet = snippet[:80] + "…"
                hits.append(WordHit(word=word, line_no=line_no, snippet=snippet))
    return hits


def _compute_cta_ratio(text: str, cta_patterns: list[str]) -> float:
    """Return CTA占比 = sum(len(pattern_match)) / max(total_chars, 1).

    Each match counts its full length once per occurrence. Patterns are
    case-insensitive.
    """
    total = max(len(text), 1)
    if not cta_patterns:
        return 0.0
    text_lower = text.lower()
    matched_chars = 0
    for pat in cta_patterns:
        if not pat:
            continue
        pat_lower = pat.lower()
        start = 0
        while True:
            idx = text_lower.find(pat_lower, start)
            if idx < 0:
                break
            matched_chars += len(pat)
            start = idx + len(pat)
    return matched_chars / total


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def check(piece_id: str, platform: str, file_path: Path | str | None = None) -> tuple[bool, Path]:
    """Run the voice check and write ``voice_report_<platform>.md``.

    Args:
        piece_id: Folder under ``$DRAFTS_DIR`` containing the draft files.
        platform: Logical platform key (must match ``config.yaml``
            ``voice_checker.platforms``). E.g. ``"x_thread"`` /
            ``"linkedin_post"`` / ``"yt_shorts"``.
        file_path: Override the auto-resolved input path. Accepts ``Path``,
            ``str``, or ``None``. When ``None``, looks for
            ``<DRAFTS_DIR>/<piece_id>/<platform>_final.md``.

    Returns:
        ``(passed, report_path)``.

    Raises:
        FileNotFoundError: input or config file missing.
        ValueError: unknown platform (not in ``voice_checker.platforms``).
    """
    cfg = _load_voice_config()

    if file_path is None:
        file_path = _DRAFTS_DIR / piece_id / f"{platform}_final.md"
    else:
        # Coerce str → Path so callers can pass either form (bug fix
        # 2026-05-14: orchestrator and external callers were tripping
        # `'str' object has no attribute 'is_file'` here).
        file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"voice_checker input not found: {file_path}")

    canonical = _canonical_platform(platform)
    platform_limits = cfg.platform_limits.get(canonical)
    if platform_limits is None:
        # Loose mode for platforms that have no entry in config.yaml AND no
        # alias mapping (e.g. an experimental new format). Use blog defaults
        # but log a WARNING so it's visible.
        logger.warning(
            "platform %s (canonical=%s) not in voice_checker.platforms; using blog defaults",
            platform, canonical,
        )
        platform_limits = cfg.platform_limits.get("blog", {"length_max": 3000, "cta_ratio_max": 0.15})
    elif canonical != platform:
        logger.info(
            "voice_checker platform alias: %s → %s (limits length_max=%s)",
            platform, canonical, platform_limits.get("length_max"),
        )

    length_max = int(platform_limits.get("length_max", 0) or 0)
    cta_ratio_max = float(platform_limits.get("cta_ratio_max", 0.15) or 0.15)

    raw_text = file_path.read_text(encoding="utf-8")
    cleaned = _strip_markdown(raw_text)
    text_length = len(cleaned)

    lines = raw_text.splitlines()
    disabled_words = cfg.disabled_words_for(platform)
    disabled_hits = _find_disabled_hits(lines, disabled_words)

    cta_ratio = _compute_cta_ratio(cleaned, cfg.cta_patterns)

    length_pass = length_max == 0 or text_length <= length_max
    cta_pass = cta_ratio <= cta_ratio_max

    result = CheckResult(
        piece_id=piece_id,
        platform=platform,
        text_length=text_length,
        length_max=length_max or None,
        length_pass=length_pass,
        cta_ratio=cta_ratio,
        cta_ratio_max=cta_ratio_max,
        cta_pass=cta_pass,
        disabled_hits=disabled_hits,
    )

    # Bug fix 2026-05-14: previous code wrote a single `voice_report.md`
    # which got overwritten by each platform's run when adapter_orchestrator
    # iterated through 4 targets. Write a platform-scoped filename so all 4
    # reports coexist; the unscoped legacy filename is kept (best-effort) as
    # an alias of the last-written report for backward compatibility with
    # any caller still reading `voice_report.md`.
    report_path = file_path.parent / f"voice_report_{platform}.md"
    rendered = _render_report(result)
    report_path.write_text(rendered, encoding="utf-8")
    try:
        (file_path.parent / "voice_report.md").write_text(rendered, encoding="utf-8")
    except OSError:
        # Don't fail the check if the legacy alias can't be written; the
        # platform-scoped report is the source of truth.
        logger.debug("could not write legacy voice_report.md alias (non-fatal)")

    logger.info(
        "voice_checker piece=%s platform=%s passed=%s violations=%s report=%s",
        piece_id,
        platform,
        result.passed,
        result.violation_count,
        report_path,
    )
    return result.passed, report_path


def _render_report(r: CheckResult) -> str:
    status = "PASS" if r.passed else "FAIL"
    lines: list[str] = [
        f"# Voice Report: {r.piece_id}",
        f"Platform: {r.platform}",
        f"Status: {status}  ({r.violation_count} violations)",
        "",
        f"## Disabled Words ({len(r.disabled_hits)} occurrences)",
    ]
    if r.disabled_hits:
        for hit in r.disabled_hits:
            lines.append(f"- `{hit.word}` (line {hit.line_no}): {hit.snippet}")
    else:
        lines.append("_none_")
    lines.append("")
    lines.append(
        f"## Length: {'PASS' if r.length_pass else 'FAIL'} "
        f"({r.text_length} / {r.length_max if r.length_max else '∞'})"
    )
    lines.append("")
    pct = r.cta_ratio * 100
    limit_pct = (r.cta_ratio_max or 0) * 100
    lines.append(
        f"## CTA Ratio: {'PASS' if r.cta_pass else 'FAIL'} "
        f"({pct:.1f}% / {limit_pct:.1f}% limit)"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn Voice Checker (PRD §2.2)")
    p.add_argument("--piece-id", required=True, help="piece_id folder under $DRAFTS_DIR")
    p.add_argument("--platform", required=True, help="platform key in voice_checker.platforms")
    p.add_argument("--file", default=None, help="explicit input file path (overrides auto-resolve)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        passed, report = check(
            piece_id=args.piece_id,
            platform=args.platform,
            file_path=Path(args.file) if args.file else None,
        )
    except FileNotFoundError as exc:
        logger.error("voice_checker file error: %s", exc)
        return 2
    except Exception:
        logger.exception("voice_checker unexpected error")
        return 1
    logger.info("report written to %s (passed=%s)", report, passed)
    return 0 if passed else 3  # non-zero so CI / orchestrator can detect fails


if __name__ == "__main__":
    sys.exit(main())
