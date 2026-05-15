"""Metrics Collector · daily 20:00 cron (PRD §2.6, Metrics doc §2).

Pulls metrics from 5 sources at 3 snapshot points (30m / 24h / 7d) for every
``publishings`` row whose ``published_at`` is within the last 7 days, plus a
daily GA4 landing-page report, Listmonk active campaign stats, and TaskOn
touchpoint stats.

Each source block is wrapped in its own try/except so that a single source
outage cannot stop the rest of the pipeline. Failures are logged as P1 and
recorded into ``publish_failures``. The job always writes a ``heartbeat`` row
at the end.

CLI::

    python -m jobs.metrics_collector
    python -m jobs.metrics_collector --date 2026-05-12
    python -m jobs.metrics_collector --skip-source postiz,ga4
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import time
from typing import Any

from lib.db import db
from lib.lark import alert
from sources.btouch import btouch, BTouchError
from sources.ga4 import ga4, GA4Error
from sources.listmonk import listmonk, ListmonkError
from sources.postiz import postiz, PostizError
from sources.twitter_x import twitter_x, TwitterXError

logger = logging.getLogger(__name__)

JOB_NAME = "metrics_collector"

# Snapshot windows (hours from publish time).
SNAPSHOT_WINDOWS: dict[str, tuple[float, float]] = {
    "30m": (0.0, 2.0),       # 0-2h after publish → take a 30m snapshot
    "24h": (22.0, 26.0),     # 22-26h after publish → take a 24h snapshot
    "7d": (6.5 * 24, 7.5 * 24),  # ~7d window
}

LANDING_PAGES_DEFAULT: tuple[str, ...] = (
    "/benchmark-report",
    "/free-diagnostic",
    "/growth-playbook",
)

ALL_SOURCES: tuple[str, ...] = ("publishings", "ga4", "listmonk", "btouch")


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #


def _parse_iso(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        # Try a few common forms
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            logger.warning("metrics_collector: unparseable timestamp %r", ts)
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _snapshot_type_for(
    published_at: dt.datetime, now: dt.datetime
) -> str | None:
    """Decide which snapshot label to capture for a publish time, or None.

    Returns ``"30m"`` / ``"24h"`` / ``"7d"`` if ``now`` falls inside the
    corresponding window, else ``None`` (no snapshot due yet, or already past
    the 7d window).
    """
    elapsed_h = (now - published_at).total_seconds() / 3600.0
    if elapsed_h < 0:
        return None
    for label, (lo, hi) in SNAPSHOT_WINDOWS.items():
        if lo <= elapsed_h <= hi:
            return label
    return None


# --------------------------------------------------------------------------- #
# Source 1+2: publishings (Postiz + X) per-row snapshots
# --------------------------------------------------------------------------- #


def _existing_snapshot_types(publishing_id: int) -> set[str]:
    rows = db.fetchall(
        "SELECT snapshot_type FROM metrics_daily WHERE publishing_id = ?",
        (publishing_id,),
    )
    return {str(r["snapshot_type"]) for r in rows if r["snapshot_type"]}


def _merge_metrics(
    postiz_blob: dict[str, Any] | None,
    x_blob: dict[str, Any] | None,
) -> dict[str, Any]:
    """Coalesce postiz analytics + X per-tweet metrics into ``metrics_daily`` cols.

    Postiz gives "totals" across all channels; X gives finer per-tweet metrics
    including organic 30-min counts. For twitter rows we prefer X for fields it
    knows about; fall back to Postiz otherwise.
    """
    out: dict[str, Any] = {
        "impressions": None,
        "likes": None,
        "replies": None,
        "quotes": None,
        "retweets": None,
        "shares": None,
        "bookmarks": None,
        "profile_clicks": None,
        "link_clicks": None,
        "impressions_30m": None,
        "engagement_count_30m": None,
    }

    if postiz_blob:
        eng = postiz_blob.get("engagements") or {}
        out["impressions"] = postiz_blob.get("impressions")
        out["likes"] = eng.get("likes")
        out["replies"] = eng.get("replies")
        out["quotes"] = eng.get("quotes")
        out["retweets"] = eng.get("retweets")
        out["shares"] = eng.get("shares")
        out["link_clicks"] = postiz_blob.get("clicks")

    if x_blob:
        pub = x_blob.get("public_metrics") or {}
        non_pub = x_blob.get("non_public_metrics") or {}
        organic = x_blob.get("organic_metrics") or {}
        # Prefer X for the fields it owns.
        if pub.get("impression_count") is not None:
            out["impressions"] = pub.get("impression_count")
        for k_x, k_out in (
            ("like_count", "likes"),
            ("reply_count", "replies"),
            ("quote_count", "quotes"),
            ("retweet_count", "retweets"),
            ("bookmark_count", "bookmarks"),
        ):
            if pub.get(k_x) is not None:
                out[k_out] = pub.get(k_x)
        if non_pub.get("url_link_clicks") is not None:
            out["link_clicks"] = non_pub.get("url_link_clicks")
        if non_pub.get("user_profile_clicks") is not None:
            out["profile_clicks"] = non_pub.get("user_profile_clicks")
        out["impressions_30m"] = organic.get("impression_count_30m")
        out["engagement_count_30m"] = organic.get("engagement_count_30m")

    return out


def _collect_publishing_snapshots(now: dt.datetime) -> int:
    """Iterate publishings in the last 7 days, write missing snapshots.

    Returns the number of metrics_daily rows inserted.
    """
    cutoff = (now - dt.timedelta(days=7, hours=2)).isoformat()
    rows = db.fetchall(
        """
        SELECT id, piece_id, platform, external_post_id, postiz_post_id, published_at
        FROM publishings
        WHERE published_at IS NOT NULL
          AND published_at >= ?
        ORDER BY published_at
        """,
        (cutoff,),
    )
    written = 0
    logger.info("metrics_collector: %d publishings within 7-day window", len(rows))
    for row in rows:
        publishing_id = int(row["id"])
        platform = (row["platform"] or "").lower()
        pub_dt = _parse_iso(row["published_at"])
        if pub_dt is None:
            logger.warning(
                "skip publishing_id=%s: unparseable published_at=%r",
                publishing_id,
                row["published_at"],
            )
            continue
        snap_type = _snapshot_type_for(pub_dt, now)
        if snap_type is None:
            continue
        if snap_type in _existing_snapshot_types(publishing_id):
            continue

        postiz_blob: dict[str, Any] | None = None
        x_blob: dict[str, Any] | None = None
        postiz_id = row["postiz_post_id"]
        external_id = row["external_post_id"]

        # Postiz analytics — for every platform.
        if postiz_id:
            try:
                postiz_blob = postiz.get_analytics(str(postiz_id))
            except PostizError as exc:
                logger.warning(
                    "postiz analytics failed for publishing_id=%s postiz_id=%s: %s",
                    publishing_id,
                    postiz_id,
                    exc,
                )
                db.publish_failures.insert(
                    severity="P1",
                    source="postiz",
                    failure_type="get_analytics_failed",
                    failure_detail=f"publishing_id={publishing_id} err={exc}",
                )

        # Twitter-native metrics.
        if platform == "twitter" and external_id:
            try:
                x_blob = twitter_x.get_tweet_metrics(str(external_id))
            except TwitterXError as exc:
                logger.warning(
                    "twitter_x metrics failed for publishing_id=%s tweet=%s: %s",
                    publishing_id,
                    external_id,
                    exc,
                )
                db.publish_failures.insert(
                    severity="P1",
                    source="twitter_x",
                    failure_type="get_tweet_metrics_failed",
                    failure_detail=f"publishing_id={publishing_id} err={exc}",
                )

        if not postiz_blob and not x_blob:
            logger.warning(
                "no metric data captured for publishing_id=%s (%s); skip insert",
                publishing_id,
                platform,
            )
            continue

        merged = _merge_metrics(postiz_blob, x_blob)
        raw_payload = json.dumps(
            {"postiz": postiz_blob, "twitter_x": x_blob},
            ensure_ascii=False,
            default=str,
        )
        try:
            db.metrics_daily.insert(
                publishing_id=publishing_id,
                snapshot_type=snap_type,
                **merged,
                raw_json=raw_payload,
            )
            written += 1
            logger.info(
                "metrics_daily inserted: publishing_id=%s type=%s impressions=%s",
                publishing_id,
                snap_type,
                merged["impressions"],
            )
        except Exception as exc:
            logger.exception(
                "metrics_daily insert failed for publishing_id=%s: %s",
                publishing_id,
                exc,
            )
            db.publish_failures.insert(
                severity="P1",
                source="metrics_daily",
                failure_type="insert_failed",
                failure_detail=f"publishing_id={publishing_id} err={exc}",
            )
    return written


# --------------------------------------------------------------------------- #
# Source 3: GA4 landing pages
# --------------------------------------------------------------------------- #


def _landing_pages_from_config() -> list[str]:
    try:
        import yaml
        from pathlib import Path

        cfg_path = (
            Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
            / "config.yaml"
        )
        if cfg_path.is_file():
            with cfg_path.open("r", encoding="utf-8") as fp:
                cfg = yaml.safe_load(fp) or {}
            pages = (cfg.get("ga4") or {}).get("landing_pages_whitelist") or []
            if isinstance(pages, list) and pages:
                return [str(p) for p in pages]
    except Exception as exc:
        logger.warning("metrics_collector: could not load landing pages from config: %s", exc)
    return list(LANDING_PAGES_DEFAULT)


def _collect_ga4_landing(date_iso: str) -> int:
    pages = _landing_pages_from_config()
    rows = ga4.run_landing_report(date=date_iso, page_paths=pages)
    written = 0
    for r in rows:
        try:
            db.landing_metrics.insert(
                snapshot_date=date_iso,
                page_path=r.get("pagePath"),
                utm_source=r.get("sessionSource"),
                utm_medium=r.get("sessionMedium"),
                utm_campaign=r.get("sessionCampaignName"),
                utm_content=r.get("sessionManualAdContent"),
                utm_term=r.get("sessionManualTerm"),
                sessions=r.get("sessions"),
                users=r.get("totalUsers"),
                pageviews=r.get("screenPageViews"),
                avg_duration_seconds=int(r.get("averageSessionDuration") or 0)
                if r.get("averageSessionDuration") is not None
                else None,
                conversions=r.get("conversions"),
            )
            written += 1
        except Exception as exc:
            logger.exception("landing_metrics insert failed: %s", exc)
            db.publish_failures.insert(
                severity="P1",
                source="ga4",
                failure_type="insert_failed",
                failure_detail=str(exc),
            )
    logger.info("ga4 landing rows inserted: %d for %s", written, date_iso)
    return written


# --------------------------------------------------------------------------- #
# Source 4: Listmonk campaigns
# --------------------------------------------------------------------------- #


def _upsert_newsletter_campaign(campaign: dict[str, Any]) -> int:
    """Idempotent upsert into newsletter_campaigns by ``campaign_id``."""
    cid = str(campaign.get("id") or "")
    if not cid:
        return 0
    to_send = campaign.get("to_send") or 0
    sent = campaign.get("sent") or 0
    opens = campaign.get("views") or 0
    clicks = campaign.get("clicks") or 0
    bounces = campaign.get("bounces") or 0
    open_rate = (opens / sent) if sent else None
    click_rate = (clicks / sent) if sent else None

    existing = db.fetchone(
        "SELECT id FROM newsletter_campaigns WHERE campaign_id = ?", (cid,)
    )
    if existing:
        db.execute(
            """
            UPDATE newsletter_campaigns SET
                send_time = ?, subject_a = ?,
                emails_sent = ?, opens = ?, open_rate = ?,
                clicks = ?, click_rate = ?, hard_bounces = ?,
                fetched_at = CURRENT_TIMESTAMP
            WHERE campaign_id = ?
            """,
            (
                campaign.get("send_at"),
                campaign.get("subject"),
                sent,
                opens,
                open_rate,
                clicks,
                click_rate,
                bounces,
                cid,
            ),
        )
        return 0  # update, not insert
    db.newsletter_campaigns.insert(
        campaign_id=cid,
        send_time=campaign.get("send_at"),
        subject_a=campaign.get("subject"),
        emails_sent=sent,
        opens=opens,
        open_rate=open_rate,
        clicks=clicks,
        click_rate=click_rate,
        hard_bounces=bounces,
    )
    return 1


def _collect_listmonk() -> int:
    """Pull stats for running + recently-finished campaigns. Returns rows written."""
    written = 0
    candidates: list[dict[str, Any]] = []
    for status in ("running", "finished"):
        try:
            page = listmonk.list_campaigns(status=status)
        except ListmonkError as exc:
            logger.warning("listmonk.list_campaigns(%s) failed: %s", status, exc)
            db.publish_failures.insert(
                severity="P1",
                source="listmonk",
                failure_type="list_campaigns_failed",
                failure_detail=f"status={status} err={exc}",
            )
            continue
        candidates.extend(page)

    for c in candidates:
        cid = c.get("id")
        if cid is None:
            continue
        try:
            full = listmonk.get_campaign(int(cid))
        except ListmonkError as exc:
            logger.warning("listmonk.get_campaign(%s) failed: %s", cid, exc)
            db.publish_failures.insert(
                severity="P1",
                source="listmonk",
                failure_type="get_campaign_failed",
                failure_detail=f"campaign_id={cid} err={exc}",
            )
            continue
        try:
            written += _upsert_newsletter_campaign(full)
        except Exception as exc:
            logger.exception("newsletter_campaigns upsert failed for %s: %s", cid, exc)
            db.publish_failures.insert(
                severity="P1",
                source="listmonk",
                failure_type="upsert_failed",
                failure_detail=f"campaign_id={cid} err={exc}",
            )
            continue

        # Per-link click stats.
        try:
            link_rows = listmonk.get_campaign_link_stats(int(cid))
        except ListmonkError as exc:
            logger.warning("listmonk.get_campaign_link_stats(%s) failed: %s", cid, exc)
            continue
        # Wipe + reinsert (Listmonk gives cumulative counts; idempotent reset).
        db.execute(
            "DELETE FROM newsletter_link_clicks WHERE campaign_id = ?", (str(cid),)
        )
        for lr in link_rows:
            try:
                db.newsletter_link_clicks.insert(
                    campaign_id=str(cid),
                    url=lr.get("url"),
                    clicks=lr.get("count") or lr.get("clicks"),
                    unique_clicks=lr.get("unique_clicks") or lr.get("uniques"),
                )
            except Exception as exc:
                logger.warning(
                    "newsletter_link_clicks insert failed for campaign %s: %s",
                    cid,
                    exc,
                )
    logger.info("listmonk: %d campaign rows upserted", written)
    return written


# --------------------------------------------------------------------------- #
# Source 5: btouch
# --------------------------------------------------------------------------- #


def _collect_btouch(date_iso: str) -> int:
    rows = btouch.get_touchpoint_stats(date_from=date_iso, date_to=date_iso)
    written = 0
    for r in rows:
        try:
            db.btouch_daily.insert(
                snapshot_date=date_iso,
                touchpoint_id=r.get("touchpoint_id"),
                touchpoint_name=r.get("touchpoint_name"),
                impressions=r.get("impressions"),
                clicks=r.get("clicks"),
                ctr=r.get("ctr"),
            )
            written += 1
        except Exception as exc:
            logger.exception("btouch_daily insert failed: %s", exc)
            db.publish_failures.insert(
                severity="P1",
                source="btouch",
                failure_type="insert_failed",
                failure_detail=str(exc),
            )
    logger.info("btouch: %d rows inserted for %s", written, date_iso)
    return written


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    date_iso: str | None = None,
    skip_sources: set[str] | None = None,
) -> dict[str, Any]:
    """Execute every source block; never aborts on a single-source failure."""
    started_at = time.monotonic()
    skip = {s.strip().lower() for s in (skip_sources or set())}
    now = dt.datetime.now(dt.timezone.utc)
    target_date = date_iso or (now.date() - dt.timedelta(days=1)).isoformat()
    counts: dict[str, int] = {s: 0 for s in ALL_SOURCES}
    failures: dict[str, str] = {}

    # 1+2. Publishing snapshots (Postiz + X).
    if "publishings" in skip:
        logger.info("skip: publishings (postiz + twitter_x)")
    else:
        try:
            counts["publishings"] = _collect_publishing_snapshots(now)
        except Exception as exc:  # noqa: BLE001
            logger.exception("publishing snapshots failed: %s", exc)
            failures["publishings"] = str(exc)
            alert("P1", f"metrics_collector: publishing snapshots failed: {exc}")
            db.publish_failures.insert(
                severity="P1",
                source="publishings",
                failure_type="block_failed",
                failure_detail=str(exc),
            )

    # 3. GA4 landing.
    if "ga4" in skip:
        logger.info("skip: ga4")
    else:
        try:
            counts["ga4"] = _collect_ga4_landing(target_date)
        except GA4Error as exc:
            logger.exception("ga4 block failed: %s", exc)
            failures["ga4"] = str(exc)
            alert("P1", f"metrics_collector: ga4 failed: {exc}")
            db.publish_failures.insert(
                severity="P1",
                source="ga4",
                failure_type="block_failed",
                failure_detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("ga4 block failed: %s", exc)
            failures["ga4"] = str(exc)
            alert("P1", f"metrics_collector: ga4 failed: {exc}")
            db.publish_failures.insert(
                severity="P1",
                source="ga4",
                failure_type="block_failed",
                failure_detail=str(exc),
            )

    # 4. Listmonk.
    if "listmonk" in skip:
        logger.info("skip: listmonk")
    else:
        try:
            counts["listmonk"] = _collect_listmonk()
        except Exception as exc:  # noqa: BLE001
            logger.exception("listmonk block failed: %s", exc)
            failures["listmonk"] = str(exc)
            alert("P1", f"metrics_collector: listmonk failed: {exc}")
            db.publish_failures.insert(
                severity="P1",
                source="listmonk",
                failure_type="block_failed",
                failure_detail=str(exc),
            )

    # 5. btouch.
    if "btouch" in skip:
        logger.info("skip: btouch")
    else:
        try:
            counts["btouch"] = _collect_btouch(target_date)
        except BTouchError as exc:
            logger.exception("btouch block failed: %s", exc)
            failures["btouch"] = str(exc)
            alert("P1", f"metrics_collector: btouch failed: {exc}")
            db.publish_failures.insert(
                severity="P1",
                source="btouch",
                failure_type="block_failed",
                failure_detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("btouch block failed: %s", exc)
            failures["btouch"] = str(exc)
            alert("P1", f"metrics_collector: btouch failed: {exc}")
            db.publish_failures.insert(
                severity="P1",
                source="btouch",
                failure_type="block_failed",
                failure_detail=str(exc),
            )

    duration = int(time.monotonic() - started_at)
    total_rows = sum(counts.values())
    if not failures:
        status = "ok"
        error_msg = None
    elif len(failures) >= len(ALL_SOURCES):
        status = "failed"
        error_msg = ", ".join(f"{k}: {v[:120]}" for k, v in failures.items())
    else:
        status = "warning"
        error_msg = ", ".join(f"{k}: {v[:120]}" for k, v in failures.items())
    db.heartbeat.record(
        JOB_NAME,
        status,
        duration,
        rows_written=total_rows,
        error_message=error_msg,
    )
    logger.info(
        "metrics_collector done: status=%s duration=%ds counts=%s failures=%s",
        status,
        duration,
        counts,
        list(failures),
    )
    return {
        "date": target_date,
        "duration_seconds": duration,
        "counts": counts,
        "failures": failures,
        "status": status,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jobs.metrics_collector",
        description="Pull metrics from Postiz + X + GA4 + Listmonk + TaskOn touchpoints.",
    )
    p.add_argument(
        "--date",
        type=str,
        default=None,
        help="ISO YYYY-MM-DD for GA4/btouch (default: yesterday UTC)",
    )
    p.add_argument(
        "--skip-source",
        type=str,
        default="",
        help=f"Comma-separated source(s) to skip. Choices: {','.join(ALL_SOURCES)}",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    args = _build_arg_parser().parse_args()
    skip = {s for s in (args.skip_source or "").split(",") if s.strip()}
    try:
        summary = run(date_iso=args.date, skip_sources=skip)
    except Exception as exc:  # noqa: BLE001
        logger.exception("metrics_collector top-level failure: %s", exc)
        return 2
    logger.info("metrics_collector summary: %s", summary)
    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
