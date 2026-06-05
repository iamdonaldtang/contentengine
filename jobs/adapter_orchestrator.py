"""Multi-platform Adapter Orchestrator · PRD §2.4.

Reads the X Thread终稿 + selection card, calls the LLM portal once per target
platform, writes 4 adapted drafts, and runs the voice checker on each. Records
voice failures into ``publish_failures`` (severity P2 — soft failure, Donald
reviews manually).

Hard constraints from PRD §2.4 B1 §3 (encoded in the per-platform system
prompts in ``config/multiplatform_rules.yaml`` and the prompt templates under
``config/prompts/adapter_*.txt``):
  1. 形态变换不变核（no copy of X paragraph order）
  2. 每平台首段独立写（first paragraph rewritten per platform）
  3. 跨平台错峰 24h+ — handled at the Postiz scheduling layer, not here.

Contract for ``lib.llm_client`` (subagent A):
    llm.complete(system: str, user: str, max_tokens: int = 4000) -> str
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from lib.db import db

load_dotenv(override=False)

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR") or (_ENGINE_ROOT / "runtime" / "drafts"))
_CONFIG_DIR = _ENGINE_ROOT / "config"
_PROMPTS_DIR = _CONFIG_DIR / "prompts"
_RULES_YAML = _CONFIG_DIR / "multiplatform_rules.yaml"

# Map each rule key in multiplatform_rules.yaml → output filename + voice
# checker platform key.
@dataclass(frozen=True)
class _TargetSpec:
    rule_key: str
    out_filename: str
    voice_platform: str


_TARGETS: list[_TargetSpec] = [
    _TargetSpec("linkedin_post", "linkedin_post.md", "linkedin_post"),
    _TargetSpec("medium_long", "medium_long.md", "blog"),
    _TargetSpec("carousel_10pages", "carousel_10pages.md", "linkedin_carousel"),
    _TargetSpec("shorts_60s", "shorts_60s.md", "yt_shorts"),
    # 2026-06-05: PR 投稿文章。voice_platform 复用 "blog"。不走自动发布（手动 pitch 媒体），
    # schedule_planner 无 pr_article slot/integration，生成后仅落 runtime/drafts 供人工取用。
    _TargetSpec("pr_article", "pr_article.md", "blog"),
]


# --------------------------------------------------------------------------- #
# Config + input loading
# --------------------------------------------------------------------------- #


def _load_rules() -> dict[str, dict[str, Any]]:
    if not _RULES_YAML.is_file():
        raise FileNotFoundError(f"multiplatform_rules.yaml not found at {_RULES_YAML}")
    with _RULES_YAML.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError("multiplatform_rules.yaml must be a mapping")
    return doc


def _load_prompt_template(rules: dict[str, dict[str, Any]], rule_key: str) -> str:
    rule = rules.get(rule_key) or {}
    prompt_name = rule.get("prompt_file") or f"adapter_{rule_key}.txt"
    path = _PROMPTS_DIR / prompt_name
    if not path.is_file():
        raise FileNotFoundError(f"prompt template missing for {rule_key}: {path}")
    return path.read_text(encoding="utf-8")


def _load_selection_card(piece_id: str) -> dict[str, Any]:
    """Try the DB first, then a YAML fallback in the drafts dir."""
    piece = db.pieces.get(piece_id)
    if piece is not None:
        card = piece.card()
        if isinstance(card, dict) and card:
            return card

    fallback = _DRAFTS_DIR / piece_id / "selection_card.yaml"
    if fallback.is_file():
        with fallback.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data
    logger.warning(
        "selection card not found for piece=%s; using empty defaults", piece_id
    )
    return {}


def _load_xthread(piece_id: str) -> str:
    path = _DRAFTS_DIR / piece_id / "xthread_final.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"xthread_final.md not found for piece={piece_id} at {path}"
        )
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Adaptation
# --------------------------------------------------------------------------- #


def _format_user_prompt(
    template: str,
    *,
    xthread_content: str,
    piece_id: str,
    card: dict[str, Any],
) -> str:
    """Substitute ``{xthread_content}``, ``{piece_id}`` and any selection-card
    variables using ``str.format_map`` with a safe default.
    """
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:  # type: ignore[override]
            return ""

    safe_vars: dict[str, Any] = {
        "xthread_content": xthread_content,
        "piece_id": piece_id,
    }
    for k, v in (card or {}).items():
        if isinstance(k, str):
            safe_vars[k] = "" if v is None else str(v)

    # Use a non-format-string replacement to avoid choking on stray { } in the
    # xthread content. We pre-substitute the well-known tokens, then defer the
    # rest to format_map.
    rendered = template
    for token, value in safe_vars.items():
        placeholder = "{" + token + "}"
        if placeholder in rendered:
            rendered = rendered.replace(placeholder, str(value))
    return rendered


def _adapt_one(
    *,
    spec: _TargetSpec,
    rule: dict[str, Any],
    template: str,
    xthread_content: str,
    piece_id: str,
    card: dict[str, Any],
    max_tokens: int,
) -> str:
    """Run a single LLM call for one target platform."""
    from lib.llm_client import llm  # lazy import

    system_prompt = rule.get("system_prompt") or ""
    if not system_prompt:
        raise ValueError(f"system_prompt missing for {spec.rule_key} in multiplatform_rules.yaml")

    user_prompt = _format_user_prompt(
        template,
        xthread_content=xthread_content,
        piece_id=piece_id,
        card=card,
    )

    started = time.monotonic()
    response = llm.complete(system=system_prompt, user=user_prompt, max_tokens=max_tokens)
    elapsed = time.monotonic() - started
    logger.info(
        "adapter llm.complete done · piece=%s target=%s elapsed=%.1fs out_chars=%s",
        piece_id,
        spec.rule_key,
        elapsed,
        len(response or ""),
    )
    if not response or not response.strip():
        raise RuntimeError(f"LLM returned empty content for {spec.rule_key}")
    return response


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(piece_id: str, max_tokens: int = 4000) -> dict[str, dict[str, Any]]:
    """Adapt one X Thread终稿 into 4 platform-specific drafts.

    Returns a per-platform summary dict::

        {
          "linkedin_post":    {"path": "...", "voice_pass": True,  "voice_report": "..."},
          "medium_long":      {"path": "...", "voice_pass": False, "voice_report": "..."},
          ...
        }

    Side effects:
        * Writes 4 ``.md`` files into ``$DRAFTS_DIR/<piece_id>/``.
        * Runs ``jobs.voice_checker.check`` on each; failures are recorded in
          ``publish_failures`` (severity P2) but the run continues.
        * Records a single ``heartbeat`` row at the end.
    """
    job_started = time.monotonic()
    rules = _load_rules()
    card = _load_selection_card(piece_id)
    xthread_content = _load_xthread(piece_id)

    drafts_root = _DRAFTS_DIR / piece_id
    drafts_root.mkdir(parents=True, exist_ok=True)

    # Local import keeps voice_checker optional if someone wants raw drafts.
    from jobs.voice_checker import check as voice_check

    results: dict[str, dict[str, Any]] = {}
    failures = 0

    for spec in _TARGETS:
        rule = rules.get(spec.rule_key) or {}
        try:
            template = _load_prompt_template(rules, spec.rule_key)
        except FileNotFoundError as exc:
            logger.error("skipping %s: %s", spec.rule_key, exc)
            failures += 1
            db.publish_failures.insert(
                severity="P1",
                source="adapter_orchestrator",
                failure_type="missing_prompt",
                failure_detail=f"{piece_id}/{spec.rule_key}: {exc}",
            )
            continue

        try:
            adapted = _adapt_one(
                spec=spec,
                rule=rule,
                template=template,
                xthread_content=xthread_content,
                piece_id=piece_id,
                card=card,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            logger.exception("LLM adaptation failed for %s", spec.rule_key)
            failures += 1
            db.publish_failures.insert(
                severity="P1",
                source="adapter_orchestrator",
                failure_type="llm_error",
                failure_detail=f"{piece_id}/{spec.rule_key}: {type(exc).__name__}: {exc}",
            )
            continue

        out_path = drafts_root / spec.out_filename
        out_path.write_text(adapted, encoding="utf-8")
        logger.info("wrote adapted draft: %s", out_path)

        # Voice check the freshly written file. The check writes a single
        # voice_report.md that gets overwritten across targets — we capture
        # the per-target pass/fail in the return dict.
        voice_pass: bool = False
        voice_report_path: str = ""
        try:
            voice_pass, voice_report = voice_check(
                piece_id=piece_id,
                platform=spec.voice_platform,
                file_path=out_path,
            )
            voice_report_path = str(voice_report)
        except Exception as exc:
            logger.exception("voice_checker errored on %s", spec.rule_key)
            db.publish_failures.insert(
                severity="P2",
                source="adapter_orchestrator",
                failure_type="voice_check_error",
                failure_detail=f"{piece_id}/{spec.rule_key}: {type(exc).__name__}: {exc}",
            )

        if not voice_pass:
            db.publish_failures.insert(
                severity="P2",
                source="adapter_orchestrator",
                failure_type="voice_check_failed",
                failure_detail=f"{piece_id}/{spec.rule_key}: see {voice_report_path}",
            )

        results[spec.rule_key] = {
            "path": str(out_path),
            "voice_pass": voice_pass,
            "voice_report": voice_report_path,
        }

    duration = int(time.monotonic() - job_started)
    status = "ok" if failures == 0 else ("warning" if failures < len(_TARGETS) else "failed")
    db.heartbeat.record(
        job_name="adapter_orchestrator",
        status=status,
        duration_seconds=duration,
        rows_written=len(results),
        error_message=None if failures == 0 else f"{failures} target(s) failed",
    )
    logger.info(
        "adapter_orchestrator done · piece=%s targets_ok=%s failures=%s duration=%ss",
        piece_id,
        len(results),
        failures,
        duration,
    )
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn Adapter Orchestrator (PRD §2.4)")
    p.add_argument("--piece-id", required=True)
    p.add_argument("--max-tokens", type=int, default=4000)
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        out = run(piece_id=args.piece_id, max_tokens=args.max_tokens)
    except FileNotFoundError as exc:
        logger.error("adapter_orchestrator input missing: %s", exc)
        return 2
    except Exception:
        logger.exception("adapter_orchestrator failed")
        return 1
    failed = sum(1 for r in out.values() if not r.get("voice_pass"))
    logger.info(
        "adapter_orchestrator CLI done · %s ok / %s voice-fail", len(out), failed
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
