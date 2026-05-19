"""jobs/kol_relation_tracker.py · B3 §1.4 · KOL relationship state machine.

WHY THIS EXISTS
---------------
B3 §1.3 produces KOL touchpoints (Daily Reply, Custom Slice, Pre-Read DM).
B3 §1.4 says we should observe whether those touchpoints land:

  KOL 第 1 次回 Reply              → tier B → 候选升级 A
  KOL 主动 Quote 1 次              → 月度个性化数据切片
  KOL 主动 Quote 3 次 (90 天内)    → 邀联名 Twitter Space
  KOL 公开背书                     → 联名 Benchmark Co-Author
  KOL 把客户引荐过来               → Dashboard 独家访问权

The engine cannot make the final business decisions (those are Donald's),
but it can mechanically observe the early signals:
  1. Donald logs a DM/Reply via the ``log-dm`` CLI subcommand
  2. The ``scan`` cron polls X API daily and finds KOL replies in-thread
  3. Quote counts roll up; tier upgrades fire at 3 Quote-tweets / 90 days.

WHAT IT DOES
------------
Two subcommands:

  * ``log-dm``  · Manual entry. Donald (via Cowork) runs this immediately
    after hand-sending a DM/Reply so the tracker has something to follow up.
    Inserts a kol_dm_log row.

  * ``scan``    · Daily cron entry (01 minute past 09:00 — runs after
    kol_daily_replier 08:30, before main publish chain). For each unreplied
    log row < 7 days old that has a donald_tweet_id, polls X API for replies
    in that conversation and looks for one authored by the KOL handle.
    On finding: stamps kol_replied_at; updates kol_watchlist row (creating
    one if absent); records Quote-count via separate X search; on tier
    threshold, auto-promotes B → A.

INDEPENDENCE / RED LINES (B1 §6 + 2026-05-18 boundary)
------------------------------------------------------
* KOL side-chain: NEVER imported by adapter_orchestrator / schedule_planner.
* Cron line is OPT-IN (commented in docker/crontab); a wedged twitter_x is
  a P2 (KOL signal is best-effort), never a P0/P1.
* Engine NEVER reaches out to KOLs — this module only OBSERVES public
  X replies. No auto-DM, no auto-Quote, no auto-Reply.

CLI
---
    # Donald logs that he just hand-sent a Reply to @hildobby
    python -m jobs.kol_relation_tracker log-dm \\
        --kol @hildobby \\
        --kind reply \\
        --tweet-url https://x.com/DonaldTang/status/123456 \\
        --piece-id 2026W19-thread01 \\
        --notes "responded to @hildobby's Dune dashboard thread"

    # Daily cron (or manual scan)
    python -m jobs.kol_relation_tracker scan
    python -m jobs.kol_relation_tracker scan --max-age-days 7 --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

# Make `python jobs/kol_relation_tracker.py` work as well as `-m jobs.kol_relation_tracker`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert as lark_alert  # noqa: E402

logger = logging.getLogger("kol_relation_tracker")

JOB_NAME = "kol_relation_tracker"

# Limit per scan tick — defensive against runaway state. X API premium plan
# is 100K reads/mo, so 50 lookups/day is well within budget.
DEFAULT_BATCH_LIMIT = int(os.environ.get("KOL_TRACKER_BATCH_LIMIT", "50"))
DEFAULT_MAX_AGE_DAYS = 7

# B3 §1.4 thresholds.
QUOTE_COUNT_WINDOW_DAYS = 90
QUOTE_COUNT_TIER_UPGRADE = 3   # 3 Quote-tweets in 90 days → tier B → A

# Permitted kinds (mirrors migration 012 CHECK constraint).
_VALID_KINDS = {"reply", "dm", "quote", "custom_slice"}


# --------------------------------------------------------------------------- #
# URL → tweet_id parsing
# --------------------------------------------------------------------------- #


_TWEET_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter|x)\.com/[^/]+/status(?:es)?/(\d+)",
    re.IGNORECASE,
)


def _parse_tweet_id(url: str | None) -> str | None:
    if not url:
        return None
    m = _TWEET_URL_RE.search(url)
    return m.group(1) if m else None


def _normalize_handle(handle: str) -> str:
    """Return canonical ``@handle`` form. Lowercases since X is case-insensitive."""
    h = handle.strip().lower()
    if not h.startswith("@"):
        h = "@" + h
    return h


# --------------------------------------------------------------------------- #
# log-dm subcommand
# --------------------------------------------------------------------------- #


def log_dm(
    *,
    kol_handle: str,
    kind: str,
    tweet_url: str | None = None,
    piece_id: str | None = None,
    notes: str | None = None,
) -> int:
    """Insert a kol_dm_log row recording that Donald just sent something.

    Returns the new row id. Raises ValueError on validation.
    """
    handle = _normalize_handle(kol_handle)
    kind = kind.strip().lower()
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}; got {kind!r}")
    tweet_id = _parse_tweet_id(tweet_url) if tweet_url else None
    if tweet_url and not tweet_id:
        logger.warning(
            "log-dm: tweet_url %r did not parse — storing URL but tracker won't be able to poll replies",
            tweet_url,
        )
    cur = db.execute(
        """
        INSERT INTO kol_dm_log (kol_handle, piece_id, kind,
                                donald_tweet_url, donald_tweet_id, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (handle, piece_id, kind, tweet_url, tweet_id, notes),
    )
    row_id = cur.lastrowid or 0
    logger.info(
        "log-dm: row=%d kol=%s kind=%s tweet_id=%s piece=%s",
        row_id, handle, kind, tweet_id or "(none)", piece_id or "(none)",
    )
    # Also stamp the kol_watchlist row's last_dm_date so static curation stays
    # roughly in sync (best-effort; we don't fail log-dm if the row is missing).
    try:
        _touch_watchlist_last_dm(handle, notes)
    except Exception:
        logger.exception("log-dm: watchlist last_dm_date update failed (non-fatal)")
    return row_id


def _touch_watchlist_last_dm(handle: str, notes: str | None) -> None:
    """Upsert kol_watchlist.last_dm_date for ``handle``.

    Uses a tolerant upsert so log-dm can record Donald's manual touch even
    when the watchlist hasn't been seeded from kol_watchlist.yaml.
    """
    today = dt.date.today().isoformat()
    row = db.fetchone(
        "SELECT handle FROM kol_watchlist WHERE handle = ?",
        (handle,),
    )
    if row is None:
        db.execute(
            """
            INSERT INTO kol_watchlist (handle, tier, last_dm_date, last_dm_content,
                                       relationship_status, updated_at)
            VALUES (?, 'B', ?, ?, 'sent_first_dm', CURRENT_TIMESTAMP)
            """,
            (handle, today, notes),
        )
    else:
        db.execute(
            """
            UPDATE kol_watchlist
            SET last_dm_date = ?, last_dm_content = COALESCE(?, last_dm_content),
                updated_at = CURRENT_TIMESTAMP
            WHERE handle = ?
            """,
            (today, notes, handle),
        )


# --------------------------------------------------------------------------- #
# scan subcommand
# --------------------------------------------------------------------------- #


def _twitter_x():
    """Lazy import so log-dm doesn't need twitter_x configured."""
    from sources.twitter_x import twitter_x  # local import to keep dep optional
    return twitter_x


def _find_kol_reply_in_conversation(
    donald_tweet_id: str,
    kol_handle: str,
) -> dict[str, Any] | None:
    """Return the first reply tweet authored by ``kol_handle`` in
    ``donald_tweet_id``'s conversation, or None.

    The X API's conversation search returns ``data: [{id, text, author_id,
    created_at, public_metrics}]``. We resolve KOL's user id once and match
    on author_id (cheaper + accurate vs string-matching @-mention).
    """
    tx = _twitter_x()
    replies = tx.get_replies(donald_tweet_id, max_results=100)
    if not replies:
        return None
    # Resolve KOL handle → user_id.
    handle_clean = kol_handle.lstrip("@").strip()
    try:
        # Re-using internal _get; sources/twitter_x exposes get_user_tweets
        # which does the same lookup as step 1. We just want the id, so cheap.
        lookup = tx._get(f"/users/by/username/{handle_clean}")  # noqa: SLF001
    except Exception:
        logger.exception("kol lookup failed for %s", kol_handle)
        return None
    kol_user_id: str | None = None
    if isinstance(lookup, dict) and isinstance(lookup.get("data"), dict):
        kol_user_id = lookup["data"].get("id")
    if not kol_user_id:
        return None
    for r in replies:
        if str(r.get("author_id")) == str(kol_user_id):
            return r
    return None


def _quote_count_window(kol_handle: str, since_days: int = QUOTE_COUNT_WINDOW_DAYS) -> int:
    """Count distinct Quote-tweets the KOL made of Donald in the window.

    Best-effort: counts ``kol_dm_log`` rows where ``kind='quote'`` and
    ``kol_handle`` matches. Donald (or the tracker) logs these via the same
    log-dm CLI. We don't try to scrape X for retroactive Quote discovery —
    that's a much harder API problem and B3 says Donald should be observing
    relationships hands-on anyway.
    """
    since_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")
    row = db.fetchone(
        """
        SELECT COUNT(*) AS c
        FROM kol_dm_log
        WHERE kol_handle = ? AND kind = 'quote' AND sent_at > ?
        """,
        (kol_handle, since_iso),
    )
    return int(row["c"]) if row else 0


def _maybe_promote_tier(kol_handle: str, quote_count: int) -> bool:
    """Promote kol_watchlist.tier from B → A when threshold met. Returns True
    if a tier change actually happened."""
    if quote_count < QUOTE_COUNT_TIER_UPGRADE:
        return False
    row = db.fetchone(
        "SELECT tier FROM kol_watchlist WHERE handle = ?",
        (kol_handle,),
    )
    if row is None:
        # No watchlist row — create one at A directly.
        db.execute(
            """
            INSERT INTO kol_watchlist (handle, tier, relationship_status, notes, updated_at)
            VALUES (?, 'A', 'auto_promoted_3plus_quotes', ?, CURRENT_TIMESTAMP)
            """,
            (kol_handle, f"auto-promoted by kol_relation_tracker · quote_count={quote_count}"),
        )
        return True
    current_tier = row["tier"] or "B"
    if current_tier == "A":
        return False
    db.execute(
        """
        UPDATE kol_watchlist
        SET tier = 'A',
            relationship_status = COALESCE(relationship_status, '') || ' · auto_promoted',
            notes = COALESCE(notes || '\n', '') ||
                    'auto-promoted by kol_relation_tracker on ' ||
                    date('now') || ' · quote_count=' || ? ,
            updated_at = CURRENT_TIMESTAMP
        WHERE handle = ?
        """,
        (str(quote_count), kol_handle),
    )
    return True


def _mark_replied(
    row_id: int,
    reply_tweet_id: str,
    replied_at: str | None,
) -> None:
    """Idempotent stamp of kol_replied_at + kol_reply_tweet_id."""
    db.execute(
        """
        UPDATE kol_dm_log
        SET kol_replied_at = COALESCE(?, CURRENT_TIMESTAMP),
            kol_reply_tweet_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND kol_replied_at IS NULL
        """,
        (replied_at, reply_tweet_id, row_id),
    )


def _touch_last_checked(row_id: int) -> None:
    db.execute(
        "UPDATE kol_dm_log SET last_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
        (row_id,),
    )


def scan(
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
    dry_run: bool = False,
) -> dict[str, int]:
    """Daily scan tick. Returns counts dict for the heartbeat."""
    started = time.monotonic()
    counts: dict[str, int] = {
        "checked": 0,
        "replies_found": 0,
        "tier_upgrades": 0,
        "x_errors": 0,
        "skipped_no_tweet_id": 0,
    }

    rows = db.fetchall(
        f"""
        SELECT id, kol_handle, kind, donald_tweet_id, donald_tweet_url, sent_at
        FROM kol_dm_log
        WHERE kol_replied_at IS NULL
          AND sent_at > datetime('now', '-{int(max_age_days)} days')
        ORDER BY sent_at
        LIMIT {int(batch_limit)}
        """
    )
    counts["checked"] = len(rows)
    if not rows:
        _record_heartbeat("ok", started, counts)
        return counts

    for row in rows:
        donald_tweet_id = row["donald_tweet_id"]
        if not donald_tweet_id:
            counts["skipped_no_tweet_id"] += 1
            continue
        kol_handle = row["kol_handle"]
        try:
            reply = _find_kol_reply_in_conversation(donald_tweet_id, kol_handle)
        except Exception:
            counts["x_errors"] += 1
            logger.exception(
                "scan: X API failure for row=%d kol=%s tweet_id=%s",
                row["id"], kol_handle, donald_tweet_id,
            )
            # Touch last_checked_at so the next run still sees this row but
            # doesn't tight-loop on it if X is wedged.
            try:
                _touch_last_checked(row["id"])
            except Exception:
                pass
            continue
        if reply is None:
            _touch_last_checked(row["id"])
            continue
        reply_tweet_id = str(reply.get("id") or "")
        replied_at = reply.get("created_at")
        if dry_run:
            logger.info(
                "DRY-RUN scan: would mark row=%d kol=%s replied (reply_id=%s)",
                row["id"], kol_handle, reply_tweet_id,
            )
            counts["replies_found"] += 1
            continue
        _mark_replied(row["id"], reply_tweet_id, replied_at)
        counts["replies_found"] += 1
        logger.info(
            "scan: row=%d kol=%s replied · reply_id=%s",
            row["id"], kol_handle, reply_tweet_id,
        )
        # Tier-upgrade check: not gated on this reply being a quote, but the
        # quote_count is sticky enough to evaluate every time we touch the row.
        try:
            qc = _quote_count_window(kol_handle)
            if _maybe_promote_tier(kol_handle, qc):
                counts["tier_upgrades"] += 1
                logger.info(
                    "scan: tier upgrade kol=%s quote_count=%d → A",
                    kol_handle, qc,
                )
                try:
                    lark_alert(
                        "P2",
                        f"KOL 关系升级: {kol_handle} 已主动 Quote ≥ {QUOTE_COUNT_TIER_UPGRADE} 次/{QUOTE_COUNT_WINDOW_DAYS}天 → tier A",
                        {"kol": kol_handle, "quote_count": qc},
                    )
                except Exception:
                    logger.exception("lark alert dispatch failed (non-fatal)")
        except Exception:
            logger.exception(
                "scan: tier-upgrade evaluation failed for kol=%s", kol_handle,
            )

    status = "ok" if counts["x_errors"] == 0 else "warning"
    _record_heartbeat(status, started, counts)
    logger.info("scan done %s", counts)
    return counts


def _record_heartbeat(status: str, started: float, counts: dict[str, int]) -> None:
    rows_written = counts.get("replies_found", 0) + counts.get("tier_upgrades", 0)
    try:
        db.heartbeat.record(
            job_name=JOB_NAME,
            status=status,
            duration_seconds=int(time.monotonic() - started),
            rows_written=rows_written,
            error_message=None,
        )
    except Exception:
        logger.exception("heartbeat record failed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="KOL relationship state machine (B3 §1.4). Two modes: "
                    "``log-dm`` to record a Donald touch; ``scan`` to find KOL replies."
    )
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="mode", required=True)

    p_log = sub.add_parser("log-dm", help="Record a Donald DM/Reply/Quote.")
    p_log.add_argument("--kol", required=True, help="@handle (case-insensitive)")
    p_log.add_argument("--kind", required=True, choices=sorted(_VALID_KINDS))
    p_log.add_argument("--tweet-url", default=None,
                       help="Donald's public X tweet URL when applicable (Reply/Quote).")
    p_log.add_argument("--piece-id", default=None,
                       help="The piece this DM is about (matches runtime/drafts/<id>/).")
    p_log.add_argument("--notes", default=None, help="Free-text context.")

    p_scan = sub.add_parser("scan", help="Daily X-API poll for KOL replies.")
    p_scan.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"Don't poll rows older than this (default {DEFAULT_MAX_AGE_DAYS}).")
    p_scan.add_argument("--batch-limit", type=int, default=DEFAULT_BATCH_LIMIT,
                        help=f"Max rows touched per tick (default {DEFAULT_BATCH_LIMIT}).")
    p_scan.add_argument("--dry-run", action="store_true",
                        help="Log what would happen without writing DB updates.")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    try:
        if args.mode == "log-dm":
            row_id = log_dm(
                kol_handle=args.kol,
                kind=args.kind,
                tweet_url=args.tweet_url,
                piece_id=args.piece_id,
                notes=args.notes,
            )
            logger.info("logged kol_dm_log row %d", row_id)
            return 0
        if args.mode == "scan":
            counts = scan(
                max_age_days=args.max_age_days,
                batch_limit=args.batch_limit,
                dry_run=args.dry_run,
            )
            return 0 if counts["x_errors"] == 0 else 0
    except ValueError as exc:
        logger.error("kol_relation_tracker validation error: %s", exc)
        return 2
    except Exception:
        logger.exception("kol_relation_tracker unexpected error")
        return 1
    return 1  # pragma: no cover · argparse 'required=True' guarantees mode set


if __name__ == "__main__":
    sys.exit(main())
