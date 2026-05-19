"""jobs/linkedin_engagement_alert.py · B3 §4 杠杆 1 · LinkedIn 30-min nudge.

WHY THIS EXISTS
---------------
B3 §4 杠杆 1 (作者主动回复评论 > 10x 权重):

  LinkedIn 算法 2024 起把 ``作者主动回复评论`` 视为高质量讨论信号,
  权重比受众单纯互动高 10x. Donald 每发 LinkedIn Post / Carousel 后
  30min 内必须回 ≥5 条评论. 这是 LinkedIn 算法借力最便宜的杠杆.

The engine cannot enforce the回复 step (LinkedIn has no public API for
reading comments on third-party posts), so this job only sends a **nudge**.
Donald sees the Lark P2 card and acts. Verification is out of scope.

WHAT IT DOES
------------
Every cron tick (every 10 min via supercronic):

  1. Selects all ``publishings`` rows where:
       - platform LIKE 'linkedin%' (covers linkedin_post + linkedin_carousel)
       - scheduled_at falls in the 2-min firing window
         (now - 31 min, now - 29 min)
       - engagement_alert_sent IS NULL  (idempotency lock)

  2. For each row, fires a Lark P2 alert reminding Donald to回 ≥5 评论
     and stamps ``engagement_alert_sent = CURRENT_TIMESTAMP``.

  3. Records one heartbeat row with counts.

WHY 30 MIN NOT EARLIER
----------------------
LinkedIn's algorithm window for "is this post worth pushing" is ~60 min.
Replying in the first 30 min hits the sweet spot — late enough that some
real comments exist to reply to, early enough that the boost still affects
distribution. B3 §4 杠杆 1 explicitly specifies 30min.

FAILURE SEMANTICS
-----------------
* Lark webhook itself swallows exceptions (lib.lark.alert returns False on
  failure); never crashes us.
* DB UPDATE failure on a single row → log + counts['errors'] + continue.
  We still record the heartbeat so monitors see we ran.
* Empty result set is the common case → heartbeat status='ok', rows=0.

RED LINES (do not weaken)
-------------------------
* Never auto-comment on Donald's behalf — engine never touches LinkedIn's
  write surface. This is a NUDGE-ONLY workflow (B1 §6 红线).
* Never fire for non-LinkedIn rows — the `LIKE 'linkedin%'` filter is the
  guard; if a future platform key starts with "linkedin" but isn't a real
  LinkedIn post, add an exclusion here.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from lib.db import db
from lib.lark import alert as lark_alert

logger = logging.getLogger("linkedin_engagement_alert")

JOB_NAME = "linkedin_engagement_alert"

# Firing window: [now - 31 min, now - 29 min]. Cron ticks every 10 min, so
# any post crossing 30 min lands in exactly one window. A 2-min window
# tolerates ±1 min cron drift (supercronic + Docker time skew). Wider would
# risk double-firing on the same row if the cron is delayed; narrower would
# risk missing the window entirely on a slow tick.
WINDOW_START_MINUTES = 31
WINDOW_END_MINUTES = 29


def _build_post_url(row: dict) -> str:
    """Best-effort LinkedIn URL for the alert card.

    publishings rarely has the canonical LinkedIn URL — Postiz holds it in
    its own DB and metrics_collector only backfills aggregate metrics, not
    the URL. Falls back to ``postiz_post_id`` for traceability.
    """
    if row.get("external_post_id"):
        return f"https://www.linkedin.com/feed/update/{row['external_post_id']}"
    if row.get("postiz_post_id"):
        return f"(Postiz post id: {row['postiz_post_id']})"
    return "(no public URL yet)"


def run() -> dict[str, int]:
    """Single firing tick. Returns counts dict for the heartbeat."""
    started = time.monotonic()
    counts: dict[str, int] = {"checked": 0, "alerted": 0, "errors": 0}

    rows = db.fetchall(
        f"""
        SELECT id, piece_id, platform, postiz_post_id, external_post_id,
               scheduled_at
        FROM publishings
        WHERE platform LIKE 'linkedin%'
          AND scheduled_at IS NOT NULL
          AND engagement_alert_sent IS NULL
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
            url = _build_post_url(dict(row))
            lark_alert(
                "P2",
                f"LinkedIn 发了 30min · 请回 ≥5 条评论 (B3 §4 杠杆 1)",
                {
                    "piece_id": row["piece_id"],
                    "platform": row["platform"],
                    "scheduled_at": row["scheduled_at"],
                    "url": url,
                },
            )
            # Stamp the sent timestamp regardless of Lark's HTTP outcome —
            # lark.alert never raises (returns False on transport failure)
            # and we don't want to re-nudge an hour later just because Lark
            # was briefly down. The publish_failures audit log inside
            # lib.lark already captures the attempt.
            db.execute(
                "UPDATE publishings SET engagement_alert_sent = CURRENT_TIMESTAMP "
                "WHERE id = ? AND engagement_alert_sent IS NULL",
                (row["id"],),
            )
            counts["alerted"] += 1
            logger.info(
                "linkedin_engagement_alert sent piece=%s row_id=%s",
                row["piece_id"], row["id"],
            )
        except Exception:
            counts["errors"] += 1
            logger.exception(
                "linkedin_engagement_alert row crashed row_id=%s", row["id"],
            )

    status = "ok" if counts["errors"] == 0 else "warning"
    _record_heartbeat(status, started, counts)
    logger.info("linkedin_engagement_alert done %s", counts)
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
    parser = argparse.ArgumentParser(description="LinkedIn 30-min engagement nudge (B3 §4 杠杆 1).")
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
