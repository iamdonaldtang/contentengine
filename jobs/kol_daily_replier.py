"""KOL Daily Reply Candidates · daily 08:30 cron (B1 §6 + marketing/CLAUDE.md).

Fetch each watchlist KOL's top-3 high-engagement tweets from the last 24 h,
hand each tweet to the LLM with the strict ``config/prompts/kol_reply.txt``
system prompt, and emit 5-8 reply CANDIDATES to a markdown file that
Donald reviews and hand-picks during his 30 min/day window.

Critical: this job **never auto-posts** to X (B1 §6 红线 — KOL Reply
final selection is Donald-only). It only stages candidates.

Output: ``runtime/kol_reply_candidates_<YYYY-MM-DD>.md``
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from lib.llm_client import LLMClientError, llm  # noqa: E402
from sources.twitter_x import TwitterXError, twitter_x  # noqa: E402

logger = logging.getLogger(__name__)

JOB_NAME = "kol_daily_replier"

# Production targets per B1 §6: 5-8 candidates. <5 → P2 (signal-of-life
# degraded). 0 → P1.
MIN_CANDIDATES_TARGET = 5
MAX_CANDIDATES_TARGET = 8

# Per-KOL tweet fan-in.
TWEETS_PER_KOL = 3
WINDOW_HOURS = 24

# Banned phrases — server-side enforcement of the prompt's hard discipline.
# Drafts containing any of these are dropped (logged WARNING) so the
# markdown emitted to Donald never shows AI slop.
BANNED_PHRASES: tuple[str, ...] = (
    "全方位", "革命性", "颠覆", "赋能", "闭环", "抓手", "价值赋能",
    "显著", "全栈", "一站式", "无缝",
    "dive into", "let's explore", "综上所述", "在当今快速发展的",
    "🚀", "🔥", "⚡",
)


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    return Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)


def _runtime_dir() -> Path:
    return Path(os.environ.get("RUNTIME_DIR") or (_engine_root() / "runtime"))


def _watchlist_path() -> Path:
    return _engine_root() / "config" / "kol_watchlist.yaml"


def _prompt_path() -> Path:
    return _engine_root() / "config" / "prompts" / "kol_reply.txt"


# --------------------------------------------------------------------------- #
# Watchlist + prompt
# --------------------------------------------------------------------------- #


def _load_watchlist() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return ``(handles, ammo_mapping)`` from kol_watchlist.yaml.

    ``handles`` is a list of dicts {handle, tier, focus, angle}, ordered
    Tier A first, then Tier B (Tier A's reply has higher priority and is
    Donald's monthly DM-relationship target — never bury them in the
    candidate file's tail).
    """
    path = _watchlist_path()
    if not path.is_file():
        raise FileNotFoundError(f"watchlist missing: {path}")
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}

    handles: list[dict[str, Any]] = []
    for key in ("pre_read_8", "observe_22"):
        entries = data.get(key) or []
        for e in entries:
            if not isinstance(e, dict) or not e.get("handle"):
                continue
            handles.append({
                "handle": str(e["handle"]).lstrip("@").strip(),
                "tier": e.get("tier") or ("A" if key == "pre_read_8" else "B"),
                "focus": e.get("focus"),
                "angle": e.get("angle"),
            })
    ammo = data.get("ammo_mapping") or {}
    if not isinstance(ammo, dict):
        ammo = {}
    return handles, {str(k): str(v) for k, v in ammo.items()}


def _load_prompt() -> str:
    path = _prompt_path()
    if not path.is_file():
        raise FileNotFoundError(f"kol_reply prompt missing: {path}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Tweet fetching
# --------------------------------------------------------------------------- #


def _tweet_engagement(tweet: dict[str, Any]) -> int:
    pm = tweet.get("public_metrics") or {}
    return int(
        (pm.get("like_count") or 0)
        + (pm.get("reply_count") or 0)
        + (pm.get("quote_count") or 0)
        + (pm.get("retweet_count") or 0)
    )


def _is_within_window(tweet: dt.datetime | str | None, hours: int) -> bool:
    if not tweet:
        return False
    if isinstance(tweet, str):
        try:
            tweet_dt = dt.datetime.fromisoformat(tweet.replace("Z", "+00:00"))
        except ValueError:
            return True  # tolerate missing parse — let LLM decide relevance
    else:
        tweet_dt = tweet
    if tweet_dt.tzinfo is None:
        tweet_dt = tweet_dt.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - tweet_dt
    return age <= dt.timedelta(hours=hours)


def _fetch_kol_tweets(handles: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return ``{handle: [top-3 tweets in last 24h]}``.

    On per-KOL X API failure (quota, deleted account, etc.), the handle is
    silently dropped from the result — we never block the whole job on one
    KOL's outage. The final candidate count is what matters.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in handles:
        handle = entry["handle"]
        try:
            tweets = twitter_x.get_user_tweets(handle, days_back=1, max_results=30)
        except TwitterXError as exc:
            logger.warning("kol_daily_replier: @%s X API error: %s", handle, exc)
            db.publish_failures.insert(
                severity="P2",
                source=JOB_NAME,
                failure_type="x_api_failed",
                failure_detail=f"@{handle} err={exc}",
            )
            continue
        # Filter to last 24h then rank by engagement, take top-3.
        recent = [t for t in tweets if _is_within_window(t.get("created_at"), WINDOW_HOURS)]
        ranked = sorted(recent, key=_tweet_engagement, reverse=True)[:TWEETS_PER_KOL]
        if ranked:
            out[handle] = ranked
    return out


# --------------------------------------------------------------------------- #
# LLM call + sanitiser
# --------------------------------------------------------------------------- #


def _is_clean_draft(draft: str) -> tuple[bool, str | None]:
    """Return (ok, banned_phrase or None) — server-side ban-list enforcement."""
    if not draft or len(draft) > 280:
        return False, f"length_violation len={len(draft)}"
    lower = draft.lower()
    for phrase in BANNED_PHRASES:
        if phrase.lower() in lower:
            return False, phrase
    return True, None


def _generate_one_reply(
    *,
    handle: str,
    tier: str,
    tweet: dict[str, Any],
    ammo_mapping: dict[str, str],
    system_prompt: str,
) -> dict[str, Any] | None:
    """Single LLM round-trip. Returns None on banned-phrase / parse failure."""
    tweet_id = str(tweet.get("id") or "")
    tweet_url = f"https://twitter.com/{handle}/status/{tweet_id}" if tweet_id else ""
    text = (tweet.get("text") or "").strip()

    # Pick the most likely ammo by keyword match (cheap; LLM can override).
    text_lower = text.lower()
    matched_ammo = None
    for key, ammo_file in ammo_mapping.items():
        if key.lower() in text_lower:
            matched_ammo = ammo_file
            break

    user_payload = {
        "kol_handle": f"@{handle}",
        "kol_tier": tier,
        "tweet_url": tweet_url,
        "tweet_text": text[:500],  # cap to keep prompt token-bounded
        "engagement": _tweet_engagement(tweet),
        "ammo_hint": matched_ammo,
    }
    user_json = json.dumps(user_payload, ensure_ascii=False, indent=2)

    try:
        result = llm.complete_json(
            system=system_prompt,
            user=user_json,
            schema_hint=(
                '{"tweet_url": str, "kol_handle": str, "kol_tier": "A"|"B", '
                '"original_tweet_excerpt": str, "matched_ammo": str, '
                '"reply_draft": str (≤240 chars), "why_pickable": str (≤60 chars), '
                '"risk_note": str (≤40 chars)}'
            ),
        )
    except LLMClientError as exc:
        logger.warning("LLM JSON failed for @%s tweet=%s: %s", handle, tweet_id, exc)
        return None

    draft = (result.get("reply_draft") or "").strip()
    ok, bad_phrase = _is_clean_draft(draft)
    if not ok:
        logger.warning(
            "drop reply for @%s tweet=%s due to %s: %s",
            handle, tweet_id, bad_phrase, draft[:80],
        )
        return None

    # Force-fill source-of-truth fields (LLM may hallucinate them).
    result["tweet_url"] = tweet_url
    result["kol_handle"] = f"@{handle}"
    result["kol_tier"] = tier
    if not result.get("matched_ammo") and matched_ammo:
        result["matched_ammo"] = matched_ammo
    return result


# --------------------------------------------------------------------------- #
# Markdown emitter
# --------------------------------------------------------------------------- #


def _render_markdown(date_str: str, candidates: list[dict[str, Any]]) -> str:
    """Format candidates for human pick. Tier A first."""
    candidates_sorted = sorted(
        candidates,
        key=lambda c: (0 if c.get("kol_tier") == "A" else 1, c.get("kol_handle", "")),
    )
    lines: list[str] = []
    lines.append(f"# KOL Daily Reply Candidates · {date_str}")
    lines.append("")
    lines.append(
        f"> 自动生成 by `jobs/kol_daily_replier`. "
        f"Donald 30 min 自审,人工挑 1-3 条发,**绝不自动发推**(B1 §6 红线)."
    )
    lines.append("")
    lines.append(f"候选数: **{len(candidates_sorted)}** "
                 f"(目标 {MIN_CANDIDATES_TARGET}-{MAX_CANDIDATES_TARGET})")
    lines.append("")
    for i, c in enumerate(candidates_sorted, start=1):
        lines.append(f"## {i}. {c.get('kol_handle')} ({c.get('kol_tier')})")
        if c.get("tweet_url"):
            lines.append(f"**Tweet**: {c['tweet_url']}")
        if c.get("original_tweet_excerpt"):
            lines.append(f"**原推摘录**: {c['original_tweet_excerpt']}")
        if c.get("matched_ammo"):
            lines.append(f"**弹药**: `{c['matched_ammo']}`")
        lines.append("")
        lines.append("**Reply 草稿**:")
        lines.append("")
        lines.append(f"> {c.get('reply_draft', '').strip()}")
        lines.append("")
        if c.get("why_pickable"):
            lines.append(f"**为何可选**: {c['why_pickable']}")
        if c.get("risk_note"):
            lines.append(f"**风险**: {c['risk_note']}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(date: str | None = None) -> dict[str, Any]:
    """Run the daily candidate generation."""
    started_at = time.monotonic()
    date_str = date or dt.date.today().isoformat()

    handles, ammo_mapping = _load_watchlist()
    system_prompt = _load_prompt()

    tweets_by_kol = _fetch_kol_tweets(handles)
    if not tweets_by_kol:
        msg = "kol_daily_replier: no tweets fetched from any KOL"
        logger.warning(msg)
        alert("P1", msg)
        _heartbeat("failed", started_at, error_message="no_tweets", rows=0)
        return {"date": date_str, "candidates": 0, "status": "failed", "reason": "no_tweets"}

    # Build tier index from watchlist for the LLM call.
    tier_by_handle = {h["handle"]: h["tier"] for h in handles}

    candidates: list[dict[str, Any]] = []
    for handle, tweet_list in tweets_by_kol.items():
        tier = tier_by_handle.get(handle, "B")
        for tweet in tweet_list:
            if len(candidates) >= MAX_CANDIDATES_TARGET:
                break
            cand = _generate_one_reply(
                handle=handle,
                tier=tier,
                tweet=tweet,
                ammo_mapping=ammo_mapping,
                system_prompt=system_prompt,
            )
            if cand:
                candidates.append(cand)
        if len(candidates) >= MAX_CANDIDATES_TARGET:
            break

    md = _render_markdown(date_str, candidates)
    out_path = _runtime_dir() / f"kol_reply_candidates_{date_str}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    logger.info("kol_daily_replier wrote %s (%d candidates)", out_path, len(candidates))

    status = "ok"
    if len(candidates) == 0:
        status = "failed"
        alert("P1", f"kol_daily_replier: 0 candidates on {date_str}", {})
    elif len(candidates) < MIN_CANDIDATES_TARGET:
        status = "warning"
        try:
            alert(
                "P2",
                f"kol_daily_replier: only {len(candidates)} candidates on {date_str} "
                f"(target {MIN_CANDIDATES_TARGET}+)",
                {"path": str(out_path)},
            )
        except Exception:
            logger.exception("P2 alert emission failed")

    _heartbeat(status, started_at, rows=len(candidates))
    return {
        "date": date_str,
        "candidates": len(candidates),
        "status": status,
        "path": str(out_path),
        "tier_a_count": sum(1 for c in candidates if c.get("kol_tier") == "A"),
    }


def _heartbeat(status: str, started_at: float, *, rows: int, error_message: str | None = None) -> None:
    duration = int(time.monotonic() - started_at)
    try:
        db.heartbeat.record(JOB_NAME, status, duration, rows_written=rows, error_message=error_message)
    except Exception:
        logger.exception("heartbeat write failed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m jobs.kol_daily_replier",
        description="Stage 5-8 KOL reply CANDIDATES for Donald's daily 30 min review.",
    )
    p.add_argument("--date", default=None, help="ISO YYYY-MM-DD (default today)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    try:
        summary = run(date=args.date)
    except FileNotFoundError as exc:
        logger.error(exc)
        return 2
    except Exception:  # noqa: BLE001
        logger.exception("kol_daily_replier top-level failure")
        return 2
    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
