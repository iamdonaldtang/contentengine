"""jobs/reply_density_alert.py · B3 §2 杠杆 1 · X 30-min reply-density nudge.

WHY THIS EXISTS
---------------
B3 §2 杠杆 1 (前 30 分钟 Reply 密度战):

  X 算法把 Reply 视为"真互动", Quote 当噪音. 主推发布后 30min 内,
  自有矩阵 5 人 (Donald + 4 BD) 必须 Reply ≥ 1 条, 每条带新观点
  (字数 ≥ 30, 非 "+1"). 5 人 angle 分配预先约定 (B3 §5.3).
  达 5 条才算"过线".

The job is a NUDGE: it pings the 5-person team to show up on the
thread. Engine never auto-replies (B1 §6 红线 — Donald hands every
public utterance).

WHAT IT DOES
------------
Every cron tick (every 10 min via supercronic):

  1. Selects all ``publishings`` rows where:
       - platform LIKE 'x_%' (covers x_thread + x_short)
       - scheduled_at falls in the 2-min firing window
         (now - 31 min, now - 29 min)
       - reply_alert_sent IS NULL  (idempotency lock)

  2. For each row, fires a Lark P2 nudge to the team and stamps
     ``reply_alert_sent = CURRENT_TIMESTAMP``.

  3. Records one heartbeat row with counts.

MVP SCOPE — NO REPLY-COUNT CHECK (yet)
--------------------------------------
B3 §2 杠杆 1 says "30min reply 数 < 5 → 提醒". The proper conditional
form needs the platform-native tweet_id to call
``twitter_x.get_replies(tweet_id)``. That id lives in
``publishings.external_post_id`` — but ``metrics_collector`` only
backfills it at the daily 20:00 cron tick, far past the 30-min nudge
window. Resolving it at alert time would mean either:

  * adding a ``postiz.get_published_post(postiz_post_id)`` lookup +
    parsing platform URL → tweet_id at every tick, OR
  * scraping Donald's recent timeline for matching scheduled_at.

Both add real surface area. For W22 the MVP fires the nudge
unconditionally — at the current state (5-人 Reply 队伍 not yet
trained), a constant ping is signal, not noise. When the team
internalizes the habit and the constant nudge becomes annoying, add the
conditional check (see TODO in ``_should_skip_for_replies``).

FAILURE SEMANTICS
-----------------
* Lark webhook itself swallows exceptions (lib.lark.alert returns False on
  failure); never crashes us.
* DB UPDATE failure on a single row → log + counts['errors'] + continue.
* Empty result set is the common case → heartbeat status='ok', rows=0.

RED LINES (do not weaken)
-------------------------
* Never auto-reply on Donald's behalf — engine never touches X's
  write surface. NUDGE-ONLY (B1 §6 红线).
* Never coordinate 27-人 Quote (B3 §2 杠杆 1 explicit prohibition — X
  algorithm flags as institutional shill).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from lib.db import db
from lib.lark import alert as lark_alert

logger = logging.getLogger("reply_density_alert")

JOB_NAME = "reply_density_alert"

# Firing window: see jobs/linkedin_engagement_alert.py for rationale.
# Same 2-min window so the two crons can share their cron tick budget.
WINDOW_START_MINUTES = 31
WINDOW_END_MINUTES = 29


def _build_post_url(row: dict[str, Any]) -> str:
    """Best-effort X URL for the alert card.

    publishings rarely has the canonical X URL at 30 min — Postiz holds it
    and metrics_collector only backfills it later. Falls back to the
    Postiz post id for traceability.
    """
    if row.get("external_post_id"):
        # X's status URLs work with any username slot; "/i/" is the canonical
        # API form.
        return f"https://x.com/i/status/{row['external_post_id']}"
    if row.get("postiz_post_id"):
        return f"(Postiz post id: {row['postiz_post_id']})"
    return "(no public URL yet)"


def _should_skip_for_replies(row: dict[str, Any]) -> bool:
    """Reserved for the future conditional form (B3 §2 杠杆 1 reply<5 check).

    TODO: when ``external_post_id`` is reliably set within 30 min of publish
    (requires a Postiz tweet_id resolution path), call
    ``twitter_x.get_replies(tweet_id)`` and return True (skip nudge) if
    len(replies) >= 5. For now always returns False — fire the nudge
    unconditionally.
    """
    return False


def run() -> dict[str, int]:
    """Single firing tick. Returns counts dict for the heartbeat."""
    started = time.monotonic()
    counts: dict[str, int] = {"checked": 0, "alerted": 0, "skipped": 0, "errors": 0}

    rows = db.fetchall(
        f"""
        SELECT id, piece_id, platform, postiz_post_id, external_post_id,
               scheduled_at
        FROM publishings
        WHERE platform LIKE 'x_%'
          AND scheduled_at IS NOT NULL
          AND reply_alert_sent IS NULL
          AND scheduled_at BETWEEN datetime('now', '-{WINDOW_START_MINUTES} minutes')
                              AND datetime('now', '-{WINDOW_END_MINUTES} minutes')
        ORDER BY scheduled_at
        """
    )
    counts["checked"] = len(rows)
    if not rows:
        _record_heartbeat("ok", started, counts)
        return counts

    for row in rows:
        try:
            row_dict = dict(row)
            if _should_skip_for_replies(row_dict):
                counts["skipped"] += 1
                # Still stamp the sentinel so we don't re-evaluate next tick.
                db.execute(
                    "UPDATE publishings SET reply_alert_sent = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND reply_alert_sent IS NULL",
                    (row["id"],),
                )
                continue

            url = _build_post_url(row_dict)
            lark_alert(
                "P2",
                "X 主推发了 30min · 5-人 Reply 队伍 请上场 (B3 §2 杠杆 1)",
                {
                    "piece_id": row["piece_id"],
                    "platform": row["platform"],
                    "scheduled_at": row["scheduled_at"],
                    "url": url,
                    "target": "5 reply · 每条 ≥30 字带新观点 · 不允许纯 +1 / Retweet",
                },
            )
            db.execute(
                "UPDATE publishings SET reply_alert_sent = CURRENT_TIMESTAMP "
                "WHERE id = ? AND reply_alert_sent IS NULL",
                (row["id"],),
            )
            counts["alerted"] += 1
            logger.info(
                "reply_density_alert sent piece=%s row_id=%s",
                row["piece_id"], row["id"],
            )
        except Exception:
            counts["errors"] += 1
            logger.exception(
                "reply_density_alert row crashed row_id=%s", row["id"],
            )

    status = "ok" if counts["errors"] == 0 else "warning"
    _record_heartbeat(status, started, counts)
    logger.info("reply_density_alert done %s", counts)
    return counts


def _record_heartbeat(status: str, started: float, counts: dict[str, int]) -> None:
    try:
        db.heartbeat.record(
            job_name=JOB_NAME,
            status=status,
            duration_seconds=int(time.monotonic() - started),
            rows_written=counts["alerted"],
            error_message=None,
        )
    except Exception:
        logger.exception("heartbeat record failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X 30-min reply-density nudge (B3 §2 杠杆 1).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    counts = run()
    return 0 if counts["errors"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
