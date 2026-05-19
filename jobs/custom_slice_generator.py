"""jobs/custom_slice_generator.py · B3 §1.3 模型 4 · Custom Slice 触发器.

WHY THIS EXISTS
---------------
B3 §1.3 模型 4 (Custom Slice · 每条 ①②⑤ 内容触发 1-3 KOL):

  每条赛道深度文 / 项目体检 / 方法论触发 1-3 个 KOL DM. 兼职女生根据
  当条内容 + KOL watchlist, 按 KOL 视角切 1 张数据图. Donald 发布后
  1 小时内 DM 给 KOL.

The engine does the LLM-heavy part of step 1 (drafting + slicing) so
Donald can hand-DM in minutes, not 20 min/KOL. Output is always
markdown + JSON drafts; engine NEVER auto-DMs (B1 §6 红线 — KOL outreach
is Donald-handed only).

WHAT IT DOES
------------
For one piece_id (CLI or in-process call from adapter_orchestrator):

  1. Loads selection_card.yaml + kol_watchlist.yaml.
  2. Filters: only fires for content_type in ALLOWED_CONTENT_TYPES (matches
     B3 ①②⑤ — thread / long / methodology / case_study / data_insight).
     ``--force`` overrides for ad-hoc runs.
  3. Matches top N KOLs by keyword overlap between the card's
     (narrative_anchor + hook_type + key_data_points) and each KOL's
     (focus + angle). Ties broken by tier (A > B). Default N = 3.
  4. For each match, calls llm.complete_json() with the Custom Slice prompt
     → DM markdown + Canva JSON spec.
  5. Writes ``runtime/drafts/<piece>/custom_slice_<handle>.md`` and
     ``runtime/drafts/<piece>/custom_slice_<handle>.canva.json``.
  6. Records heartbeat with counts (matched / generated / errors).

INDEPENDENCE
------------
This job is KOL-domain (B1 §6 + 2026-05-18 boundary): it must NEVER
block the main publish chain. Failures are caught and recorded — the
piece still publishes normally. Cron is OPTIONAL (manual trigger by
default); only wire it up after a piece has gone through adapter.

CALLING CONVENTIONS
-------------------
Standalone CLI:
    python -m jobs.custom_slice_generator --piece-id 2026W19-thread01
    python -m jobs.custom_slice_generator --piece-id 2026W19-thread01 --top-n 3
    python -m jobs.custom_slice_generator --piece-id 2026W19-thread01 --force

In-process (e.g. from adapter_orchestrator's tail):
    from jobs.custom_slice_generator import generate_for_piece
    generate_for_piece(piece_id, top_n=3)

RED LINES (do not weaken)
-------------------------
* Never auto-DM. Output is markdown + JSON only; Donald hand-delivers.
* Never call Anthropic — MiniMaxi-only per user_llm_cost_constraint memory.
  llm.complete_json() already routes correctly.
* Never put real URLs in dm_text — the prompt forbids it; if the model
  slips one in anyway, the served markdown will be sent as-is and Donald
  will catch it on review (audit log shows the file path).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv(override=False)

# Make `python jobs/custom_slice_generator.py` work as well as `-m jobs.custom_slice_generator`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert as lark_alert  # noqa: E402
from lib.llm_client import llm, LLMClientError  # noqa: E402

logger = logging.getLogger("custom_slice_generator")

JOB_NAME = "custom_slice_generator"

# Default N — B3 §1.3 says "1-3 KOL per piece"; 3 is the playbook ceiling.
DEFAULT_TOP_N = 3

# content_type values that match B3 ①②⑤ (赛道深度 / 项目体检 / 方法论).
# Selection cards use ``content_type``; missing field = fall through to skip
# unless ``--force`` is set. Add new types here as the editorial taxonomy
# settles.
ALLOWED_CONTENT_TYPES: set[str] = {
    "thread",          # B3 ② 项目体检 Thread (most common)
    "long",            # B3 ① 赛道深度长文
    "methodology",     # B3 ⑤ 方法论
    "case_study",      # B3 ① 案例(算深度)
    "data_insight",    # B3 ② 数据洞察 Thread
    "playbook",        # B3 ⑤ 方法论 alias
}

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR") or (_ENGINE_ROOT / "runtime" / "drafts"))
_CONFIG_DIR = _ENGINE_ROOT / "config"
_KOL_WATCHLIST_YAML = _CONFIG_DIR / "kol_watchlist.yaml"
_PROMPT_PATH = _CONFIG_DIR / "prompts" / "custom_slice.txt"


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #


def _load_selection_card(piece_id: str) -> dict[str, Any]:
    path = _DRAFTS_DIR / piece_id / "selection_card.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"selection_card not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        card = yaml.safe_load(f) or {}
    if not isinstance(card, dict):
        raise ValueError(f"selection_card.yaml is not a mapping: {path}")
    return card


def _load_kol_watchlist() -> list[dict[str, Any]]:
    """Return a flat list of KOL dicts (Tier A + Tier B merged)."""
    if not _KOL_WATCHLIST_YAML.is_file():
        raise FileNotFoundError(f"kol_watchlist.yaml not found: {_KOL_WATCHLIST_YAML}")
    with _KOL_WATCHLIST_YAML.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    out: list[dict[str, Any]] = []
    for key in ("pre_read_8", "observe_22"):
        for entry in doc.get(key) or []:
            if isinstance(entry, dict) and entry.get("handle"):
                out.append(entry)
    return out


def _load_prompt() -> str:
    if not _PROMPT_PATH.is_file():
        raise FileNotFoundError(f"custom_slice prompt not found: {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


_WORD_SPLIT = re.compile(r"[\s,/·。、;:()\[\]\-—_]+", re.UNICODE)


def _tokens(text: str | None) -> set[str]:
    """Lower-case word tokens from a free-text blob. Cheap matching primitive.

    Not jieba-based — KOL ``focus`` / ``angle`` strings are short and
    space-delimited (mostly English keywords like "DeFi data / Sybil"),
    and the card's ``narrative_anchor`` / ``hook_type`` use compact tokens
    (``47pct_bot`` / ``假用户代价``). Substring overlap covers both cases.
    """
    if not text:
        return set()
    return {t.strip().lower() for t in _WORD_SPLIT.split(text) if t.strip()}


def _piece_signal_tokens(card: dict[str, Any]) -> set[str]:
    """Build the token bag the matcher uses to rank KOLs against a piece."""
    parts: list[str] = []
    for k in ("narrative_anchor", "hook_type", "title", "ammo_reference", "target_persona"):
        v = card.get(k)
        if isinstance(v, str):
            parts.append(v)
    for k in ("key_data_points", "data_sources_required", "related_anchors"):
        v = card.get(k)
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x)
    return _tokens(" ".join(parts))


def _kol_signal_tokens(kol: dict[str, Any]) -> set[str]:
    return _tokens(" ".join(str(kol.get(k, "")) for k in ("focus", "angle")))


_TIER_ORDER = {"A": 0, "B": 1, "C": 2}


def _rank_kols(
    card: dict[str, Any],
    watchlist: list[dict[str, Any]],
    top_n: int,
) -> list[tuple[int, dict[str, Any]]]:
    """Return ``[(score, kol), ...]`` sorted by score desc, tier asc, top-N sliced.

    Score = size of token-overlap between piece signal bag and KOL signal
    bag. Ties broken first by tier (A < B < C) then by alphabetical handle
    for determinism.
    """
    piece_bag = _piece_signal_tokens(card)
    if not piece_bag:
        logger.warning("custom_slice: piece has no signal tokens (empty narrative_anchor + hook_type)")
        return []
    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    for kol in watchlist:
        kol_bag = _kol_signal_tokens(kol)
        overlap = len(piece_bag & kol_bag)
        if overlap == 0:
            continue
        tier = _TIER_ORDER.get(str(kol.get("tier", "B")).upper(), 3)
        ranked.append((overlap, tier, str(kol.get("handle", "")), kol))
    # sort: overlap desc, tier asc, handle asc
    ranked.sort(key=lambda r: (-r[0], r[1], r[2]))
    return [(r[0], r[3]) for r in ranked[:top_n]]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def _build_user_payload(
    piece_id: str,
    card: dict[str, Any],
    kol: dict[str, Any],
) -> str:
    main_data_points = card.get("key_data_points") or []
    payload = {
        "piece_id": piece_id,
        "selection_card": {
            "title": card.get("title"),
            "hook_type": card.get("hook_type"),
            "narrative_anchor": card.get("narrative_anchor"),
            "target_persona": card.get("target_persona"),
            "ammo_reference": card.get("ammo_reference"),
            "key_data_points": main_data_points,
            "data_sources_required": card.get("data_sources_required"),
        },
        "kol": {
            "handle": kol.get("handle"),
            "focus": kol.get("focus"),
            "angle": kol.get("angle"),
            "tier": kol.get("tier"),
            "last_interaction": kol.get("last_interaction"),
        },
        "main_data_points": main_data_points,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


_SCHEMA_HINT = (
    '{"kol_handle": str, "dm_text": str, '
    '"canva_slice": {"title": str, "subtitle": str, '
    '"data_points": [{"label": str, "value": str}, ...], '
    '"color_scheme": str, "logo_position": str}, '
    '"kol_angle_rationale": str}'
)


def _generate_one(
    piece_id: str,
    card: dict[str, Any],
    kol: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    user_payload = _build_user_payload(piece_id, card, kol)
    result = llm.complete_json(
        system=prompt,
        user=user_payload,
        schema_hint=_SCHEMA_HINT,
        max_tokens=1200,
        temperature=0.6,
    )
    if not isinstance(result, dict):
        raise LLMClientError(f"expected dict, got {type(result).__name__}")
    # Defensive: scrub any URL the model may have slipped into dm_text
    # despite the explicit prompt rule. We don't reject the output (still
    # useful to Donald) but we replace the URL with a marker so it doesn't
    # get hand-sent verbatim.
    dm = result.get("dm_text")
    if isinstance(dm, str):
        scrubbed = re.sub(r"https?://\S+", "[URL-removed-by-engine]", dm)
        if scrubbed != dm:
            logger.warning(
                "custom_slice: LLM emitted URL in dm_text for kol=%s — scrubbed",
                kol.get("handle"),
            )
            result["dm_text"] = scrubbed
    return result


def _write_outputs(
    piece_dir: Path,
    kol_handle: str,
    result: dict[str, Any],
) -> tuple[Path, Path]:
    safe_handle = kol_handle.lstrip("@").strip() or "kol"
    safe_handle = re.sub(r"[^A-Za-z0-9_\-]", "_", safe_handle)

    md_path = piece_dir / f"custom_slice_{safe_handle}.md"
    canva_path = piece_dir / f"custom_slice_{safe_handle}.canva.json"

    dm_text = str(result.get("dm_text") or "").strip()
    rationale = str(result.get("kol_angle_rationale") or "").strip()
    canva = result.get("canva_slice") or {}

    md_body = (
        f"# Custom Slice DM · {kol_handle}\n\n"
        f"> 由 engine 自动生成 · Donald 手发前请校对 · 不要直接复制粘贴\n"
        f"> 切片角度: {rationale}\n\n"
        f"---\n\n"
        f"{dm_text}\n\n"
        f"---\n\n"
        f"## Canva 改图参数 (兼职女生用)\n"
        f"见同目录 `custom_slice_{safe_handle}.canva.json`\n"
    )
    md_path.write_text(md_body, encoding="utf-8")
    canva_path.write_text(
        json.dumps(canva, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return md_path, canva_path


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def generate_for_piece(
    piece_id: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    force: bool = False,
) -> dict[str, Any]:
    """Generate Custom Slice DM drafts for one piece's top-N matched KOLs.

    Args:
        piece_id: Folder under ``runtime/drafts/``.
        top_n: How many KOLs to draft for (default 3 per B3 §1.3).
        force: When True, bypass the ``content_type`` filter. Useful for
            ad-hoc runs against legacy pieces that didn't tag content_type.

    Returns:
        Summary dict with counts + per-KOL results. Never raises on individual
        KOL failures — those are caught and recorded in ``results``.
    """
    started = time.monotonic()
    counts: dict[str, int] = {
        "matched": 0,
        "generated": 0,
        "errors": 0,
        "skipped_filter": 0,
    }
    results: list[dict[str, Any]] = []

    piece_dir = _DRAFTS_DIR / piece_id
    if not piece_dir.is_dir():
        raise FileNotFoundError(f"piece dir not found: {piece_dir}")

    card = _load_selection_card(piece_id)
    content_type = str(card.get("content_type") or "").strip().lower()
    if not force and content_type not in ALLOWED_CONTENT_TYPES:
        counts["skipped_filter"] += 1
        logger.info(
            "custom_slice: skip piece=%s content_type=%r not in ALLOWED_CONTENT_TYPES "
            "(use --force to override)",
            piece_id, content_type,
        )
        _record_heartbeat("ok", started, counts)
        return {"piece_id": piece_id, "counts": counts, "results": results,
                "skipped_reason": f"content_type={content_type!r} not eligible"}

    watchlist = _load_kol_watchlist()
    matches = _rank_kols(card, watchlist, top_n)
    counts["matched"] = len(matches)

    if not matches:
        logger.warning(
            "custom_slice: piece=%s · no KOLs matched on keyword overlap "
            "(card tokens: %s)",
            piece_id,
            sorted(_piece_signal_tokens(card))[:10],
        )
        _record_heartbeat("ok", started, counts)
        return {"piece_id": piece_id, "counts": counts, "results": results}

    prompt = _load_prompt()

    for score, kol in matches:
        handle = str(kol.get("handle", ""))
        try:
            result = _generate_one(piece_id, card, kol, prompt)
            md_path, canva_path = _write_outputs(piece_dir, handle, result)
            counts["generated"] += 1
            results.append({
                "kol_handle": handle,
                "overlap_score": score,
                "status": "ok",
                "md_path": str(md_path),
                "canva_path": str(canva_path),
            })
            logger.info(
                "custom_slice: piece=%s kol=%s overlap=%d -> %s",
                piece_id, handle, score, md_path.name,
            )
        except LLMClientError as exc:
            counts["errors"] += 1
            results.append({
                "kol_handle": handle,
                "overlap_score": score,
                "status": "llm_error",
                "error": str(exc)[:300],
            })
            logger.warning("custom_slice: LLM failed for kol=%s · %s", handle, exc)
        except Exception as exc:  # noqa: BLE001 — never block downstream
            counts["errors"] += 1
            results.append({
                "kol_handle": handle,
                "overlap_score": score,
                "status": "error",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            })
            logger.exception("custom_slice: unexpected error for kol=%s", handle)

    if counts["errors"] and counts["generated"] == 0:
        status = "failed"
        try:
            lark_alert(
                "P1",
                f"custom_slice_generator: 0 generated for piece={piece_id}, {counts['errors']} errors",
                {"piece_id": piece_id, "matched": counts["matched"]},
            )
        except Exception:
            logger.exception("lark alert failed")
    elif counts["errors"]:
        status = "warning"
        try:
            lark_alert(
                "P2",
                f"custom_slice_generator: partial success piece={piece_id}, "
                f"{counts['generated']}/{counts['matched']} ok",
                {"piece_id": piece_id, "errors": counts["errors"]},
            )
        except Exception:
            logger.exception("lark alert failed")
    else:
        status = "ok"

    _record_heartbeat(status, started, counts)
    return {"piece_id": piece_id, "counts": counts, "results": results}


def _record_heartbeat(status: str, started: float, counts: dict[str, int]) -> None:
    try:
        db.heartbeat.record(
            job_name=JOB_NAME,
            status=status,
            duration_seconds=int(time.monotonic() - started),
            rows_written=counts.get("generated", 0),
            error_message=None,
        )
    except Exception:
        logger.exception("heartbeat record failed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="B3 §1.3 模型 4 · KOL Custom Slice DM 草稿生成器 "
                    "(engine 起草,Donald 手发,never auto-DM)."
    )
    parser.add_argument("--piece-id", required=True, help="Folder under runtime/drafts/")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"Max KOLs to draft for (default {DEFAULT_TOP_N})")
    parser.add_argument("--force", action="store_true",
                        help="Bypass content_type filter")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    try:
        summary = generate_for_piece(
            piece_id=args.piece_id,
            top_n=args.top_n,
            force=args.force,
        )
    except FileNotFoundError as exc:
        logger.error("custom_slice file error: %s", exc)
        return 2
    except Exception:
        logger.exception("custom_slice unexpected error")
        return 1

    logger.info("custom_slice done: %s", summary["counts"])
    if summary.get("skipped_reason"):
        return 0
    return 0 if summary["counts"]["errors"] == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
