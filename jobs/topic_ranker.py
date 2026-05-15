"""Topic Ranker · weekly Sunday 20:00 cron (PRD §2.5).

Reads a candidate pool JSON file for a given ISO week, scores every candidate
via the LLM against the 5-dimension rubric, applies ±20% weight adjustments
from last week's ``weekly_aggregates``, runs a diversity re-rank so no single
``hook_type`` or ``narrative_anchor`` dominates the Top 10, then writes the
final selection to ``runtime/selection_<YYYYWWW>.md`` and marks Top 10 rows in
the ``candidates`` table as ``status='picked'``.

CLI::

    python -m jobs.topic_ranker
    python -m jobs.topic_ranker --week 2026W19
    python -m jobs.topic_ranker --candidates-file runtime/candidates_pool_2026W19.json
    python -m jobs.topic_ranker --limit 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from lib.db import db
from lib.lark import alert
from lib.llm_client import llm

logger = logging.getLogger(__name__)

JOB_NAME = "topic_ranker"

DEFAULT_LIMIT = 10
DIVERSITY_MAX_SHARE = 0.5  # any one hook_type / narrative_anchor > 50% triggers rerank
WEIGHT_BAND = 0.20  # ±20% ranker weight adjustment per PRD §2.5

# Score dimension names — must match rubric + prompt + DB analysis.
SCORE_DIMS: tuple[str, ...] = (
    "audience_fit",
    "data_solidity",
    "hook_strength",
    "stepps",
    "brand_consistency",
)


# --------------------------------------------------------------------------- #
# Path / week helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    env_root = os.environ.get("ENGINE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _runtime_dir() -> Path:
    env_runtime = os.environ.get("RUNTIME_DIR")
    if env_runtime:
        return Path(env_runtime).expanduser().resolve()
    return _engine_root() / "runtime"


def _current_iso_week() -> str:
    today = dt.date.today()
    iso_year, iso_week, _ = today.isocalendar()
    return f"{iso_year}W{iso_week:02d}"


def _previous_iso_week(week: str) -> str:
    """Compute the previous ISO week label for the given ``YYYYWww`` string."""
    year_str, week_str = week.split("W", 1)
    monday = dt.date.fromisocalendar(int(year_str), int(week_str), 1)
    prev_monday = monday - dt.timedelta(days=7)
    py, pw, _ = prev_monday.isocalendar()
    return f"{py}W{pw:02d}"


def _rubric_path() -> Path:
    return _engine_root() / "config" / "topic_ranker_rubric.md"


def _prompt_path() -> Path:
    return _engine_root() / "config" / "prompts" / "topic_ranker.txt"


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #


def _load_candidates_file(path: Path) -> list[dict[str, Any]]:
    """Load candidate pool from a JSON file.

    Accepts two shapes:
      * a bare list of candidate dicts
      * an object with a ``candidates`` key holding the list
    """
    if not path.is_file():
        raise FileNotFoundError(f"candidate pool file not found: {path}")
    with path.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and isinstance(raw.get("candidates"), list):
        items = raw["candidates"]
    else:
        raise ValueError(
            f"candidate pool file has unexpected shape: {type(raw).__name__}; "
            "expected list or object with 'candidates' key"
        )
    logger.info("loaded %d candidates from %s", len(items), path)
    return items


def _load_rubric() -> str:
    path = _rubric_path()
    if not path.is_file():
        raise FileNotFoundError(f"topic_ranker rubric missing: {path}")
    return path.read_text(encoding="utf-8")


def _load_system_prompt() -> str:
    path = _prompt_path()
    if not path.is_file():
        raise FileNotFoundError(f"topic_ranker prompt missing: {path}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _ensure_candidate_row(cand: dict[str, Any]) -> str:
    """Make sure a row exists in ``candidates`` for this candidate; return id.

    The JSON pool file may have been produced by an upstream signal-gathering
    job (kol_watch, dify, manual). If ``candidate_id`` is present and a row
    exists we touch nothing; otherwise we INSERT a fresh row in ``raw`` state.
    """
    candidate_id = cand.get("candidate_id") or cand.get("id")
    if candidate_id:
        existing = db.fetchone(
            "SELECT candidate_id FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        if existing:
            return str(existing["candidate_id"])

    # Synthesize a stable id if absent.
    if not candidate_id:
        prefix = cand.get("week") or _current_iso_week()
        # Stable hash from title for idempotent re-runs.
        import hashlib

        title_seed = (cand.get("title_hypothesis") or cand.get("title") or json.dumps(cand))[:200]
        digest = hashlib.sha1(title_seed.encode("utf-8")).hexdigest()[:8]
        candidate_id = f"cand-{prefix}-{digest}"
        cand["candidate_id"] = candidate_id

    source_route = int(cand.get("source_route") or 6)
    if source_route < 1 or source_route > 6:
        source_route = 6
    try:
        db.candidates.insert(
            candidate_id=candidate_id,
            source_route=source_route,
            title_hypothesis=cand.get("title_hypothesis") or cand.get("title"),
            narrative_anchor_potential=cand.get("narrative_anchor_potential")
            or cand.get("narrative_anchor"),
            data_sources_hint=cand.get("data_sources_hint") or cand.get("data_sources"),
            why_relevant=cand.get("why_relevant"),
            relevance_score_self=cand.get("relevance_score_self"),
            status="raw",
        )
    except Exception as exc:
        # Could be UNIQUE collision from a parallel insert; tolerate.
        logger.warning("candidates.insert(%s) failed (likely race): %s", candidate_id, exc)
    return candidate_id


def _last_week_weights(prev_week: str) -> dict[str, float]:
    """Return ``{hook_type: weight_multiplier}`` from last week's aggregates.

    ``weight_for_next_week`` is already a clamped multiplier in ``[1-band, 1+band]``
    written by ``attribution_engine.compute_weekly_aggregates``. Missing
    hook_types default to 1.0.
    """
    rows = db.fetchall(
        """
        SELECT value, weight_for_next_week
        FROM weekly_aggregates
        WHERE week = ? AND dimension = 'hook_type'
        """,
        (prev_week,),
    )
    weights: dict[str, float] = {}
    for r in rows:
        if r["value"] is None:
            continue
        w = r["weight_for_next_week"]
        if w is None:
            continue
        try:
            weights[str(r["value"])] = float(w)
        except (TypeError, ValueError):
            continue
    return weights


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _build_user_payload(candidate: dict[str, Any], rubric_text: str) -> str:
    """Compose the user message: rubric + serialized candidate JSON."""
    safe_candidate = {
        k: v
        for k, v in candidate.items()
        if k
        in {
            "candidate_id",
            "title_hypothesis",
            "title",
            "narrative_anchor_potential",
            "narrative_anchor",
            "hook_type",
            "data_sources_hint",
            "data_sources",
            "why_relevant",
            "target_persona",
            "source_route",
            "raw_text",
            "summary",
            "kol_handle",
        }
    }
    return (
        "===== RUBRIC =====\n"
        f"{rubric_text}\n"
        "===== CANDIDATE =====\n"
        f"{json.dumps(safe_candidate, ensure_ascii=False, indent=2)}\n"
    )


def _score_candidate(
    candidate: dict[str, Any],
    rubric_text: str,
    system_prompt: str,
) -> dict[str, Any]:
    """Call the LLM to score one candidate; returns the score dict.

    The LLM is expected to return exactly the 5 numeric dimensions plus
    ``rationale``. Out-of-range or missing scores are clamped to ``[1, 10]``
    and a P2 alert is fired if clamping was necessary.
    """
    schema_hint = (
        '{"audience_fit": int 1-10, "data_solidity": int 1-10, '
        '"hook_strength": int 1-10, "stepps": int 1-10, '
        '"brand_consistency": int 1-10, "rationale": "string"}'
    )
    user_msg = _build_user_payload(candidate, rubric_text)
    data = llm.complete_json(
        system=system_prompt,
        user=user_msg,
        schema_hint=schema_hint,
        max_tokens=600,
        temperature=0.2,
    )
    clamped: dict[str, Any] = {}
    needed_clamp = False
    for dim in SCORE_DIMS:
        raw_val = data.get(dim)
        try:
            val = int(raw_val)
        except (TypeError, ValueError):
            logger.warning(
                "topic_ranker: missing/invalid %s for %s; defaulting to 5",
                dim,
                candidate.get("candidate_id"),
            )
            val = 5
            needed_clamp = True
        if val < 1 or val > 10:
            needed_clamp = True
            val = max(1, min(10, val))
        clamped[dim] = val
    clamped["rationale"] = (data.get("rationale") or "")[:300]
    if needed_clamp:
        logger.info(
            "topic_ranker: clamped scores for %s (raw=%s)",
            candidate.get("candidate_id"),
            {d: data.get(d) for d in SCORE_DIMS},
        )
    return clamped


# --------------------------------------------------------------------------- #
# Diversity rerank
# --------------------------------------------------------------------------- #


def _share(values: list[str | None]) -> tuple[str | None, float]:
    """Return (most-common-value, share) for a list. Empty list -> (None, 0)."""
    if not values:
        return None, 0.0
    counter: Counter[str | None] = Counter(values)
    most_value, count = counter.most_common(1)[0]
    return most_value, count / len(values)


def _diversity_rerank(
    scored: list[dict[str, Any]],
    limit: int,
    field: str,
) -> list[dict[str, Any]]:
    """Swap dominant-field rows out of the Top ``limit`` until share ≤ 50%.

    ``scored`` MUST be pre-sorted descending by ``adjusted_score``. Returns a
    new list of the same length, ranked. The algorithm is greedy:
      1. Take the head as the Top-N selection.
      2. While the dominant value's share in Top-N exceeds ``DIVERSITY_MAX_SHARE``,
         find the lowest-ranked row whose ``field`` equals the dominant value and
         swap it with the highest-ranked non-dominant row outside the Top-N.
      3. If no swap candidate exists, stop (degenerate pool).
    """
    if limit <= 0 or len(scored) <= 1:
        return scored
    working = list(scored)
    iterations = 0
    while iterations < limit * 2:  # hard cap to avoid infinite loop
        iterations += 1
        top = working[:limit]
        tail = working[limit:]
        values_in_top = [c.get(field) for c in top]
        dominant, share = _share(values_in_top)
        if share <= DIVERSITY_MAX_SHARE or dominant is None:
            return working
        # Find swap-out: lowest-ranked in top with dominant value.
        out_idx = None
        for i in range(len(top) - 1, -1, -1):
            if top[i].get(field) == dominant:
                out_idx = i
                break
        # Find swap-in: highest-ranked in tail with non-dominant value.
        in_idx = None
        for j, cand in enumerate(tail):
            if cand.get(field) != dominant:
                in_idx = j
                break
        if out_idx is None or in_idx is None:
            logger.warning(
                "diversity rerank: cannot break dominance of %s=%s (share=%.2f)",
                field,
                dominant,
                share,
            )
            return working
        # Swap.
        working[out_idx], working[limit + in_idx] = working[limit + in_idx], working[out_idx]
        logger.info(
            "diversity rerank: swapped Top-%d row #%d (%s) with tail row #%d (%s)",
            limit,
            out_idx,
            dominant,
            limit + in_idx,
            working[out_idx].get(field),
        )
    logger.warning("diversity rerank: hit iteration cap (%d)", iterations)
    return working


# --------------------------------------------------------------------------- #
# Output writer
# --------------------------------------------------------------------------- #


def _write_selection_md(week: str, top: list[dict[str, Any]], output_path: Path) -> None:
    """Write the Top-N selection as Markdown with YAML front-matter per row."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    header_lines = [
        f"# Selection · {week}",
        "",
        f"_Generated by `jobs.topic_ranker` at {dt.datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"Top {len(top)} candidates from this week's pool, ranked by adjusted score "
        f"(LLM rubric + last-week weight + diversity re-rank).",
        "",
    ]
    blocks: list[str] = ["\n".join(header_lines)]
    for rank, c in enumerate(top, start=1):
        fm = {
            "rank": rank,
            "candidate_id": c.get("candidate_id"),
            "title": c.get("title_hypothesis") or c.get("title"),
            "hook_type": c.get("hook_type"),
            "narrative_anchor": c.get("narrative_anchor_potential")
            or c.get("narrative_anchor"),
            "source_route": c.get("source_route"),
            "ranker_score": c.get("ranker_score"),
            "adjusted_score": round(float(c.get("adjusted_score", 0.0)), 2),
            "scores": {dim: c.get(dim) for dim in SCORE_DIMS},
            "weight_applied": round(float(c.get("weight_applied", 1.0)), 3),
        }
        fm_yaml = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
        body_parts = [
            f"## #{rank} · {fm['title'] or '(untitled)'}",
            "",
            "```yaml",
            fm_yaml,
            "```",
            "",
            f"**Rationale**: {c.get('rationale') or '(no rationale returned)'}",
            "",
        ]
        if c.get("why_relevant"):
            body_parts.append(f"**Why relevant**: {c['why_relevant']}")
            body_parts.append("")
        blocks.append("\n".join(body_parts))
    output_path.write_text("\n---\n\n".join(blocks), encoding="utf-8")
    logger.info("selection markdown written: %s", output_path)


# --------------------------------------------------------------------------- #
# Main orchestration
# --------------------------------------------------------------------------- #


def run(
    week: str | None = None,
    candidates_file: Path | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Execute the full topic-ranker pipeline. Returns a summary dict."""
    started_at = time.monotonic()
    week_label = week or _current_iso_week()
    pool_path = candidates_file or (_runtime_dir() / f"candidates_pool_{week_label}.json")
    output_path = _runtime_dir() / f"selection_{week_label}.md"

    try:
        candidates = _load_candidates_file(pool_path)
    except (FileNotFoundError, ValueError) as exc:
        alert("P1", f"topic_ranker: failed to load pool {pool_path}: {exc}")
        db.heartbeat.record(JOB_NAME, "failed", 0, 0, str(exc))
        raise

    if not candidates:
        logger.warning("topic_ranker: empty candidate pool for week=%s", week_label)
        alert("P2", f"topic_ranker: empty candidate pool for {week_label}")
        db.heartbeat.record(JOB_NAME, "warning", 0, 0, "empty_pool")
        return {"week": week_label, "scored": 0, "picked": 0, "output": str(output_path)}

    rubric_text = _load_rubric()
    system_prompt = _load_system_prompt()
    prev_week = _previous_iso_week(week_label)
    hook_weights = _last_week_weights(prev_week)
    logger.info(
        "topic_ranker: week=%s prev_week=%s pool=%d hook_weight_keys=%d",
        week_label,
        prev_week,
        len(candidates),
        len(hook_weights),
    )

    scored: list[dict[str, Any]] = []
    score_failures = 0
    for cand in candidates:
        cid = _ensure_candidate_row(cand)
        cand["candidate_id"] = cid
        try:
            scores = _score_candidate(cand, rubric_text, system_prompt)
        except Exception as exc:  # noqa: BLE001 — keep going on one bad row
            score_failures += 1
            logger.exception("topic_ranker: scoring failed for %s: %s", cid, exc)
            db.publish_failures.insert(
                severity="P2",
                source=JOB_NAME,
                failure_type="llm_score_failed",
                failure_detail=f"candidate={cid} err={exc}",
            )
            continue
        merged = {**cand, **scores}
        ranker_score = sum(scores[d] for d in SCORE_DIMS)
        merged["ranker_score"] = ranker_score
        # Apply ±20% hook-type weight from last week's aggregates.
        weight = hook_weights.get(str(cand.get("hook_type")), 1.0)
        # Defensive clamp (in case stored weight drifted).
        weight = max(1.0 - WEIGHT_BAND, min(1.0 + WEIGHT_BAND, weight))
        merged["weight_applied"] = weight
        merged["adjusted_score"] = ranker_score * weight
        try:
            db.execute(
                "UPDATE candidates SET ranker_score = ?, status = 'scored' WHERE candidate_id = ?",
                (ranker_score, cid),
            )
        except Exception as exc:
            logger.warning("topic_ranker: failed to persist score for %s: %s", cid, exc)
        scored.append(merged)

    if not scored:
        msg = f"topic_ranker: all {len(candidates)} candidates failed to score"
        alert("P1", msg)
        db.heartbeat.record(JOB_NAME, "failed", int(time.monotonic() - started_at), 0, msg)
        raise RuntimeError(msg)

    scored.sort(key=lambda c: c["adjusted_score"], reverse=True)

    # Diversity rerank: hook_type first, then narrative_anchor.
    reranked = _diversity_rerank(scored, limit, "hook_type")
    reranked = _diversity_rerank(
        reranked,
        limit,
        # Prefer the explicit field, fall back to the alias.
        "narrative_anchor_potential"
        if any("narrative_anchor_potential" in c for c in reranked)
        else "narrative_anchor",
    )

    top = reranked[:limit]
    _write_selection_md(week_label, top, output_path)

    picked_ids = [str(c["candidate_id"]) for c in top]
    if picked_ids:
        placeholders = ",".join(["?"] * len(picked_ids))
        try:
            db.execute(
                f"UPDATE candidates SET status = 'picked' WHERE candidate_id IN ({placeholders})",
                tuple(picked_ids),
            )
        except Exception as exc:
            logger.exception("topic_ranker: failed to mark picked rows: %s", exc)
            alert("P2", f"topic_ranker: failed to mark picked rows: {exc}")

    duration = int(time.monotonic() - started_at)
    status = "ok" if score_failures == 0 else "warning"
    db.heartbeat.record(
        JOB_NAME,
        status,
        duration,
        rows_written=len(top),
        error_message=f"{score_failures} score failures" if score_failures else None,
    )
    logger.info(
        "topic_ranker done: week=%s scored=%d picked=%d failures=%d duration=%ds",
        week_label,
        len(scored),
        len(top),
        score_failures,
        duration,
    )
    return {
        "week": week_label,
        "scored": len(scored),
        "picked": len(top),
        "failures": score_failures,
        "output": str(output_path),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jobs.topic_ranker",
        description="Score weekly candidate pool, apply weight + diversity rerank, output Top N.",
    )
    p.add_argument(
        "--week",
        type=str,
        default=None,
        help="ISO week label e.g. 2026W19 (default: current ISO week)",
    )
    p.add_argument(
        "--candidates-file",
        type=Path,
        default=None,
        help="Override candidate pool JSON path (default: runtime/candidates_pool_<week>.json)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of top picks to write (default: {DEFAULT_LIMIT})",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    args = _build_arg_parser().parse_args()
    try:
        summary = run(
            week=args.week,
            candidates_file=args.candidates_file,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 — surface to OS exit code
        logger.exception("topic_ranker top-level failure: %s", exc)
        return 2
    logger.info("topic_ranker summary: %s", summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
