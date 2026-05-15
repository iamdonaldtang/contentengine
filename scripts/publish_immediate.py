"""publish_immediate.py · 绕过 schedule_planner 错峰，立即发布到 Postiz。

WHY THIS EXISTS
---------------
schedule_planner 默认按 B1 §3.2 错峰把 scheduled_at 排到下周一/二/四。这对
正常 SOP 是对的，但首次跑通流程 / 5h 闭环验证 / 应急发布场景下，我们需要让
publishings 表里 3 个平台的 scheduled_at = now + N 分钟，Postiz 5 分钟后真发。

设计 — Monkey-patch compute_slot_datetime
--------------------------------------------
本脚本动手修改 schedule_planner.compute_slot_datetime 让所有平台 scheduled_at
等于 now + offset_minutes。然后调 schedule_planner.run() 复用其完整逻辑（dry-run /
真排程 / yt_metadata 派生 / publishings 写表 / heartbeat / 告警）。仅在脚本运行
时生效，不影响正常 cron。

USAGE
-----
docker compose exec engine python -m scripts.publish_immediate \\
    --piece-id 2026W20-thread01 \\
    --platforms linkedin_post,yt_shorts \\
    --offset-minutes 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from jobs import schedule_planner
from jobs.schedule_planner import run as _real_run

logger = logging.getLogger("publish_immediate")


def _build_patched_compute(immediate_dt: dt.datetime):
    """Return a compute_slot_datetime replacement that always yields immediate_dt
    while preserving the timezone of the original slot (ET)."""

    def _patched(base_monday, weekday, hour, tz):  # noqa: D401 - drop-in
        # Convert immediate_dt to the platform's tz (typically ET) so downstream
        # logging looks coherent.
        return immediate_dt.astimezone(tz)

    return _patched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Immediate publish — bypass schedule_planner staggering."
    )
    parser.add_argument(
        "--piece-id", required=True,
        help="Folder under runtime/drafts/ (e.g. 2026W20-thread01)",
    )
    parser.add_argument(
        "--platforms",
        default="linkedin_post,yt_shorts",
        help="Comma-separated platform keys. Default: linkedin_post,yt_shorts. "
             "Other platforms in build_schedule still appear in publishings "
             "but will be filtered out below.",
    )
    parser.add_argument(
        "--offset-minutes", type=int, default=5,
        help="scheduled_at = now + offset_minutes (default 5)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't call Postiz, print plan only.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    target_plats = {p.strip() for p in args.platforms.split(",") if p.strip()}
    if not target_plats:
        parser.error("--platforms must list at least one platform key")

    immediate_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        minutes=args.offset_minutes
    )
    logger.info(
        "publish_immediate :: piece=%s platforms=%s offset=%dmin → scheduled_at=%s",
        args.piece_id, sorted(target_plats), args.offset_minutes,
        immediate_dt.isoformat(),
    )

    # --- Monkey-patch compute_slot_datetime --- #
    original_compute = schedule_planner.compute_slot_datetime
    schedule_planner.compute_slot_datetime = _build_patched_compute(immediate_dt)

    # --- Monkey-patch build_schedule to filter platforms before publish --- #
    original_build = schedule_planner.build_schedule

    def _filtered_build(piece_id, **kwargs):
        plans = original_build(piece_id, **kwargs)
        filtered = []
        for plan in plans:
            if plan["platform"] not in target_plats:
                # Mark as skipped so it's still logged but not posted.
                plan = {**plan, "skip_reason": f"not in --platforms ({plan['platform']})"}
            filtered.append(plan)
        return filtered

    schedule_planner.build_schedule = _filtered_build

    try:
        summary = _real_run(
            piece_id=args.piece_id,
            dry_run=args.dry_run,
        )
    finally:
        # Restore in case anything else in the same process needs the originals.
        schedule_planner.compute_slot_datetime = original_compute
        schedule_planner.build_schedule = original_build

    logger.info("publish_immediate done :: %s", {
        k: v for k, v in summary.items() if k != "results"
    })

    # Exit code: 0 on ok/warning, 1 on failed.
    return 0 if summary.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
