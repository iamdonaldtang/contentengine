"""Attribution Engine · daily 21:00 cron (PRD §2.7, Metrics doc §4).

Three attribution models run in parallel:

  * ``last_touch`` (MVP) — credit to the most-recent touchpoint in the journey
    window. Source-of-truth for ``leads.first_utm_*`` (the column names are
    legacy; today they carry last-touch values per Metrics doc §4.3).
  * ``first_touch`` (V1, M2) — credit to the EARLIEST touchpoint. Persisted
    in ``leads.first_touch_piece_id`` (migration 002).
  * ``linear`` (V2, M2) — equal ``1/N`` credit to every touchpoint in the
    journey. Persisted as a JSON ``{piece_id: weight}`` map in
    ``leads.linear_weights_json`` (migration 002).

Weekly aggregates are now stratified by ``attribution_model`` (migration 003),
so the same (week, dimension, value) tuple carries one row per model.

CLI::

    python -m jobs.attribution_engine
    python -m jobs.attribution_engine --lead-id 42
    python -m jobs.attribution_engine --model first_touch
    python -m jobs.attribution_engine --model linear
    python -m jobs.attribution_engine --model all
    python -m jobs.attribution_engine --bd-override lead_id=42,content_id=2026W19-thread01,by=alex
    python -m jobs.attribution_engine --compute-weekly-aggregates 2026W19
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import time
from typing import Any, Literal

from lib.db import db
from lib.lark import alert

logger = logging.getLogger(__name__)

JOB_NAME = "attribution_engine"

# ±20% weight band per PRD §2.5. Mirrors topic_ranker.WEIGHT_BAND.
WEIGHT_BAND = 0.20
JOURNEY_WINDOW_DAYS = 7

# Attribution models supported (parallel; not mutually exclusive).
AttributionModel = Literal["last_touch", "first_touch", "linear"]
ALL_MODELS: tuple[AttributionModel, ...] = ("last_touch", "first_touch", "linear")

# Dimensions used by compute_weekly_aggregates. Each entry =
# (dimension_label, SQL expression for GROUP BY value).
AGG_DIMENSIONS: list[tuple[str, str]] = [
    ("hook_type", "json_extract(pi.selection_card_yaml, '$.hook_type')"),
    ("narrative_anchor", "json_extract(pi.selection_card_yaml, '$.narrative_anchor')"),
    ("time_slot", "strftime('%H', pu.published_at)"),
    ("format", "pu.platform"),
    # vs_baseline is a synthetic dimension: compares avg_impressions to the
    # rolling-baseline. We emit a single row with value="vs_baseline" capturing
    # the ratio so the topic_ranker can read it consistently.
    ("vs_baseline", "'overall'"),
]


# --------------------------------------------------------------------------- #
# Cookie / email stitching (B1 §4.3, migration 007)
# --------------------------------------------------------------------------- #

# How many distinct cookies to union per email_hash when reconstructing the
# user journey. Most users see < 3 (device + cookie clear + private window);
# higher counts usually indicate a data-quality problem rather than legit
# stitching opportunity — capping protects against runaway IN-list growth.
COOKIE_STITCH_LIMIT = 10


def lookup_cookies_for_email_hash(email_hash: str) -> list[str]:
    """Return every cookie_id seen for ``email_hash``, newest-first.

    The list is bounded by :data:`COOKIE_STITCH_LIMIT`. Returns an empty list
    when the email has never been stitched, when the migration hasn't been
    applied (table missing), or on any query failure — callers always treat
    cookie stitching as best-effort enrichment, never as a hard requirement.
    """
    if not email_hash:
        return []
    try:
        rows = db.fetchall(
            """
            SELECT cookie_id
            FROM cookie_email_map
            WHERE email_hash = ?
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (email_hash, COOKIE_STITCH_LIMIT),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cookie_email_map lookup failed for email_hash=%s: %s",
            email_hash[:12],
            exc,
        )
        return []
    return [str(r["cookie_id"]) for r in rows if r["cookie_id"]]


def lookup_cookie_for_email_hash(email_hash: str) -> str | None:
    """Resolve ``email_hash`` -> single most-recent ``cookie_id``.

    Convenience wrapper around :func:`lookup_cookies_for_email_hash` for
    callers that only need the freshest cookie. Returns ``None`` when no
    cookie has ever been stitched (or migration 007 has not run yet).
    """
    cookies = lookup_cookies_for_email_hash(email_hash)
    return cookies[0] if cookies else None


# --------------------------------------------------------------------------- #
# Last-touch attribution (MVP)
# --------------------------------------------------------------------------- #


def _resolve_piece_id_for_campaign(utm_campaign: str | None) -> str | None:
    """Return the ``piece_id`` whose publishings row matches this campaign."""
    if not utm_campaign:
        return None
    row = db.fetchone(
        "SELECT piece_id FROM publishings WHERE utm_campaign = ? ORDER BY id LIMIT 1",
        (utm_campaign,),
    )
    return str(row["piece_id"]) if row and row["piece_id"] else None


def _journey_for_lead(lead: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull every ``user_journey`` row attributable to this lead.

    Strategy:
      1. Use ``email_hash`` as primary user_id key.
      2. If a cookie can be stitched (stub returns None today), union those rows.
      3. Constrain to the 7-day window ending at ``first_seen_at``.
    """
    email_hash = lead.get("email_hash")
    if not email_hash:
        return []
    first_seen = lead.get("first_seen_at")
    if not first_seen:
        return []

    user_ids: list[str] = [str(email_hash)]
    # Plural lookup — union EVERY cookie ever stitched to this email so the
    # journey covers all device/session impressions, not just the freshest.
    user_ids.extend(lookup_cookies_for_email_hash(str(email_hash)))

    placeholders = ",".join(["?"] * len(user_ids))
    rows = db.fetchall(
        f"""
        SELECT id, user_id, action, utm_source, utm_medium, utm_campaign,
               utm_content, utm_term, referrer, page_path, piece_id, timestamp
        FROM user_journey
        WHERE user_id IN ({placeholders})
          AND timestamp BETWEEN datetime(?, '-{JOURNEY_WINDOW_DAYS} days') AND ?
        ORDER BY timestamp ASC
        """,
        tuple(user_ids) + (first_seen, first_seen),
    )
    return [dict(r) for r in rows]


def _apply_last_touch(lead: dict[str, Any], touchpoints: list[dict[str, Any]]) -> bool:
    """Stamp last-touch UTMs into ``leads`` if absent. Returns True if updated.

    BD override discipline: ``bd_attribution_content_id`` is never overwritten
    here when set. The ``leads.first_utm_*`` columns are stamped only if NULL.
    """
    if not touchpoints:
        return False
    # MVP last-touch: pick the most-recent non-signup attribution event.
    candidates = [t for t in touchpoints if t.get("action") in (None, "impression", "click", "visit")]
    last = candidates[-1] if candidates else touchpoints[-1]

    cur_campaign = lead.get("first_utm_campaign")
    cur_content = lead.get("first_utm_content")
    cur_term = lead.get("first_utm_term")

    new_campaign = cur_campaign or last.get("utm_campaign")
    new_content = cur_content or last.get("utm_content")
    new_term = cur_term or last.get("utm_term")

    if (new_campaign, new_content, new_term) == (cur_campaign, cur_content, cur_term):
        return False
    db.execute(
        """
        UPDATE leads SET
            first_utm_campaign = COALESCE(first_utm_campaign, ?),
            first_utm_content  = COALESCE(first_utm_content, ?),
            first_utm_term     = COALESCE(first_utm_term, ?)
        WHERE id = ?
        """,
        (new_campaign, new_content, new_term, lead["id"]),
    )
    logger.info(
        "leads.last_touch updated id=%s campaign=%s content=%s term=%s",
        lead["id"],
        new_campaign,
        new_content,
        new_term,
    )
    return True


def _renumber_and_link_journey(touchpoints: list[dict[str, Any]]) -> int:
    """Renumber ``touchpoint_seq`` 1..N and fill ``piece_id`` where derivable.

    Returns the number of journey rows that were UPDATEd.
    """
    updates = 0
    for i, tp in enumerate(touchpoints, start=1):
        new_piece = tp.get("piece_id") or _resolve_piece_id_for_campaign(tp.get("utm_campaign"))
        try:
            db.execute(
                """
                UPDATE user_journey
                SET touchpoint_seq = ?,
                    piece_id       = COALESCE(piece_id, ?)
                WHERE id = ?
                """,
                (i, new_piece, tp["id"]),
            )
            updates += 1
        except Exception as exc:
            logger.warning(
                "user_journey update failed id=%s: %s", tp.get("id"), exc
            )
    return updates


# --------------------------------------------------------------------------- #
# V1 — first-touch attribution
# --------------------------------------------------------------------------- #


def apply_first_touch(lead_id: int) -> str | None:
    """Stamp the EARLIEST touchpoint's piece_id into ``leads.first_touch_piece_id``.

    Returns the credited ``piece_id`` (or ``None`` if no touchpoints / no piece
    resolves). Idempotent — re-running just rewrites the same value.

    Behaviour:
      * Picks the chronologically first row in the journey window.
      * Resolves ``piece_id`` directly if present, else via the campaign->piece
        mapping in ``publishings``.
      * On no-touchpoints, the column is left untouched (we don't NULL a
        previously-stamped value, since that would erase a prior good run).
    """
    lead_row = db.fetchone("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead_row:
        logger.warning("apply_first_touch: lead_id=%s not found", lead_id)
        return None
    lead = dict(lead_row)
    touchpoints = _journey_for_lead(lead)
    if not touchpoints:
        return None
    first_tp = touchpoints[0]
    piece_id = first_tp.get("piece_id") or _resolve_piece_id_for_campaign(
        first_tp.get("utm_campaign")
    )
    if not piece_id:
        logger.info(
            "apply_first_touch lead_id=%s: earliest touchpoint had no resolvable piece_id",
            lead_id,
        )
        return None
    db.execute(
        "UPDATE leads SET first_touch_piece_id = ? WHERE id = ?",
        (piece_id, lead_id),
    )
    logger.info(
        "leads.first_touch_piece_id=%s set on lead_id=%s", piece_id, lead_id
    )
    return str(piece_id)


# --------------------------------------------------------------------------- #
# V2 — linear attribution
# --------------------------------------------------------------------------- #


def apply_linear(lead_id: int) -> dict[str, float]:
    """Split equal ``1/N`` credit across every distinct piece_id in the journey.

    Returns ``{piece_id: weight}``. Weights sum to ``1.0`` (within floating
    error) when at least one touchpoint resolves to a piece. Persists the
    payload JSON-encoded into ``leads.linear_weights_json``.

    If multiple touchpoints land on the same piece_id (a typical
    impression+click pair on the same Twitter thread) they each contribute
    ``1/N`` so the piece's total weight increases — this is the standard
    linear-attribution behaviour, not deduplication.
    """
    lead_row = db.fetchone("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead_row:
        logger.warning("apply_linear: lead_id=%s not found", lead_id)
        return {}
    lead = dict(lead_row)
    touchpoints = _journey_for_lead(lead)
    if not touchpoints:
        return {}

    # Map every touchpoint -> piece_id (resolve via campaign when needed),
    # discarding rows that don't resolve.
    resolved: list[str] = []
    for tp in touchpoints:
        pid = tp.get("piece_id") or _resolve_piece_id_for_campaign(tp.get("utm_campaign"))
        if pid:
            resolved.append(str(pid))
    if not resolved:
        return {}
    share = 1.0 / float(len(resolved))
    weights: dict[str, float] = {}
    for pid in resolved:
        weights[pid] = weights.get(pid, 0.0) + share

    payload = json.dumps(weights, ensure_ascii=False, sort_keys=True)
    db.execute(
        "UPDATE leads SET linear_weights_json = ? WHERE id = ?",
        (payload, lead_id),
    )
    logger.info(
        "leads.linear_weights_json set on lead_id=%s pieces=%d sum=%.4f",
        lead_id,
        len(weights),
        sum(weights.values()),
    )
    return weights


# --------------------------------------------------------------------------- #
# BD override
# --------------------------------------------------------------------------- #


def bd_attribution_override(lead_id: int, content_id: str, bd_name: str) -> None:
    """Stamp the BD's K4-confirmed attribution. K4 always wins.

    Sets ``sql_status='verified'``, ``sql_verified_at``, ``sql_verified_by``,
    and ``bd_attribution_content_id``. Pre-existing ``bd_attribution_content_id``
    is overwritten because BD K4 is the canonical truth.
    """
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    db.execute(
        """
        UPDATE leads
        SET sql_status = 'verified',
            sql_verified_at = ?,
            sql_verified_by = ?,
            bd_attribution_content_id = ?
        WHERE id = ?
        """,
        (now_iso, bd_name, content_id, lead_id),
    )
    logger.info(
        "BD override applied: lead_id=%s content_id=%s by=%s",
        lead_id,
        content_id,
        bd_name,
    )


# --------------------------------------------------------------------------- #
# Per-lead orchestration
# --------------------------------------------------------------------------- #


def _process_lead(
    lead: dict[str, Any],
    models: tuple[AttributionModel, ...],
) -> dict[str, Any]:
    """Run journey renumbering + the requested attribution models for one lead.

    Returns a per-lead summary dict. Per-model failures are caught and logged
    here so a single model's bug can't sink the daily loop.
    """
    touchpoints = _journey_for_lead(lead)
    renumbered = _renumber_and_link_journey(touchpoints) if touchpoints else 0

    lead_updated = False
    first_touch_piece: str | None = None
    linear_weights: dict[str, float] = {}

    if "last_touch" in models:
        try:
            lead_updated = _apply_last_touch(lead, touchpoints)
        except Exception as exc:
            logger.exception(
                "last_touch failed for lead_id=%s: %s", lead.get("id"), exc
            )
    if "first_touch" in models:
        try:
            first_touch_piece = apply_first_touch(int(lead["id"]))
        except Exception as exc:
            logger.exception(
                "first_touch failed for lead_id=%s: %s", lead.get("id"), exc
            )
    if "linear" in models:
        try:
            linear_weights = apply_linear(int(lead["id"]))
        except Exception as exc:
            logger.exception(
                "linear failed for lead_id=%s: %s", lead.get("id"), exc
            )

    return {
        "lead_id": lead["id"],
        "touchpoints": len(touchpoints),
        "renumbered": renumbered,
        "lead_updated": lead_updated,
        "first_touch_piece_id": first_touch_piece,
        "linear_weight_pieces": len(linear_weights),
    }


# --------------------------------------------------------------------------- #
# Weekly aggregates (called by weekly_reporter via compute_weekly_aggregates)
# --------------------------------------------------------------------------- #


def _iso_week_range(week: str) -> tuple[str, str]:
    """Map ``2026W19`` -> (``'2026-05-04T00:00:00'``, ``'2026-05-10T23:59:59'``)."""
    import re
    from datetime import date, timedelta

    m = re.match(r"^(\d{4})W(\d{1,2})$", week)
    if not m:
        raise ValueError(f"invalid ISO week token: {week!r} (expected e.g. '2026W19')")
    year, w = int(m.group(1)), int(m.group(2))
    monday = date.fromisocalendar(year, w, 1)
    sunday = monday + timedelta(days=6)
    return (f"{monday.isoformat()}T00:00:00", f"{sunday.isoformat()}T23:59:59")


def _baseline_for_dimension(
    dimension: str, current_week: str, model: AttributionModel
) -> float:
    """Rolling baseline within the same (dimension, model) over previous 4 weeks."""
    rows = db.fetchall(
        """
        SELECT week, AVG(avg_impressions) AS avg
        FROM weekly_aggregates
        WHERE dimension = ?
          AND attribution_model = ?
          AND week < ?
        GROUP BY week
        ORDER BY week DESC
        LIMIT 4
        """,
        (dimension, model, current_week),
    )
    avgs = [float(r["avg"]) for r in rows if r["avg"] is not None]
    if not avgs:
        return 0.0  # no baseline → weight defaults applied later
    return sum(avgs) / len(avgs)


def _compute_weight(actual: float | None, baseline: float) -> float:
    """Clamp(actual / baseline) into [1 - band, 1 + band]."""
    if not actual or baseline <= 0:
        return 1.0
    raw = float(actual) / float(baseline)
    return max(1.0 - WEIGHT_BAND, min(1.0 + WEIGHT_BAND, raw))


def _leads_join_clause_for_model(model: AttributionModel) -> tuple[str, str]:
    """Return (join_sql, leads_predicate) snippets to attribute leads under ``model``.

    * last_touch:   join leads via user_journey.action='signup' (existing path).
    * first_touch:  join leads.first_touch_piece_id directly to the piece in scope.
    * linear:       join leads whose linear_weights_json has ANY weight for the
                    piece in scope (uses json_each).
    """
    if model == "last_touch":
        join = (
            "LEFT JOIN user_journey uj ON uj.piece_id = pu.piece_id AND uj.action = 'signup' "
            "LEFT JOIN leads l ON l.email_hash = uj.user_id"
        )
        return join, ""
    if model == "first_touch":
        join = "LEFT JOIN leads l ON l.first_touch_piece_id = pu.piece_id"
        return join, ""
    # linear: leads whose JSON map contains this piece_id
    join = (
        "LEFT JOIN leads l ON l.linear_weights_json IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM json_each(l.linear_weights_json) je WHERE je.key = pu.piece_id)"
    )
    return join, ""


def compute_weekly_aggregates(
    week: str,
    models: tuple[AttributionModel, ...] = ALL_MODELS,
) -> int:
    """Compute 5-dim aggregates stratified by attribution_model.

    Writes one row per (week, dimension, value, attribution_model) into
    ``weekly_aggregates``. Returns the total rows inserted across every model.
    """
    week_start, week_end = _iso_week_range(week)
    total_written = 0

    # Pre-purge previous rows for this (week, model-set) to keep idempotent.
    # We only purge the models we're about to recompute so a partial run with
    # --model first_touch doesn't wipe last_touch rows.
    placeholders = ",".join(["?"] * len(models))
    db.execute(
        f"DELETE FROM weekly_aggregates WHERE week = ? AND attribution_model IN ({placeholders})",
        (week, *models),
    )

    for model in models:
        join_clause, _ = _leads_join_clause_for_model(model)
        for dim_label, dim_expr in AGG_DIMENSIONS:
            sql = f"""
            SELECT {dim_expr} AS dim_value,
                   COUNT(DISTINCT pu.id) AS publish_count,
                   AVG(m.impressions) AS avg_impressions,
                   AVG(
                       CASE WHEN COALESCE(m.impressions, 0) = 0 THEN NULL
                            ELSE CAST(
                                COALESCE(m.likes, 0) + COALESCE(m.replies, 0) +
                                COALESCE(m.quotes, 0) + COALESCE(m.retweets, 0)
                                AS REAL
                            ) / m.impressions
                       END
                   ) AS avg_eng_rate,
                   AVG(
                       CASE WHEN COALESCE(m.impressions, 0) = 0 THEN NULL
                            ELSE CAST(COALESCE(m.link_clicks, 0) AS REAL) / m.impressions
                       END
                   ) AS avg_ctr,
                   COUNT(DISTINCT l.id) AS leads_attributed,
                   SUM(CASE WHEN l.sql_status = 'verified' THEN 1 ELSE 0 END) AS sql_attributed
            FROM publishings pu
            JOIN pieces pi ON pi.id = pu.piece_id
            LEFT JOIN metrics_daily m ON m.publishing_id = pu.id AND m.snapshot_type = '7d'
            {join_clause}
            WHERE pu.published_at BETWEEN ? AND ?
            GROUP BY dim_value
            """
            try:
                rows = db.fetchall(sql, (week_start, week_end))
            except Exception as exc:
                logger.exception(
                    "weekly_aggregates query failed dim=%s model=%s: %s",
                    dim_label,
                    model,
                    exc,
                )
                alert(
                    "P2",
                    f"weekly_aggregates dim={dim_label} model={model} query failed: {exc}",
                )
                continue

            baseline = _baseline_for_dimension(dim_label, week, model)
            for r in rows:
                value = r["dim_value"]
                if value is None:
                    value = "(null)"
                actual_impr = r["avg_impressions"]
                weight = _compute_weight(
                    actual_impr,
                    baseline if baseline > 0 else (float(actual_impr or 0) or 1.0),
                )
                try:
                    db.weekly_aggregates.insert(
                        week=week,
                        dimension=dim_label,
                        value=str(value),
                        publish_count=r["publish_count"],
                        avg_impressions=r["avg_impressions"],
                        avg_engagement_rate=r["avg_eng_rate"],
                        avg_click_through_rate=r["avg_ctr"],
                        leads_attributed=r["leads_attributed"],
                        sql_attributed=r["sql_attributed"],
                        weight_for_next_week=weight,
                        attribution_model=model,
                    )
                    total_written += 1
                except Exception as exc:
                    logger.exception(
                        "weekly_aggregates insert failed dim=%s model=%s value=%s: %s",
                        dim_label,
                        model,
                        value,
                        exc,
                    )
                    db.publish_failures.insert(
                        severity="P2",
                        source="attribution_engine",
                        failure_type="weekly_aggregates_insert_failed",
                        failure_detail=f"dim={dim_label} model={model} value={value} err={exc}",
                    )
    logger.info(
        "weekly_aggregates: %d rows written for week=%s models=%s",
        total_written,
        week,
        ",".join(models),
    )
    return total_written


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    lead_id: int | None = None,
    models: tuple[AttributionModel, ...] = ALL_MODELS,
) -> dict[str, Any]:
    """Run attribution over recently-seen leads (or one lead by id).

    ``models`` selects which attribution writers run for each lead. Default is
    all three (last_touch + first_touch + linear). Per-model failures are
    logged inside ``_process_lead`` and never abort the loop.
    """
    started_at = time.monotonic()
    if lead_id is not None:
        rows = db.fetchall("SELECT * FROM leads WHERE id = ?", (lead_id,))
    else:
        rows = db.fetchall(
            """
            SELECT *
            FROM leads
            WHERE first_seen_at IS NOT NULL
              AND first_seen_at >= datetime('now', ?)
            ORDER BY first_seen_at DESC
            """,
            (f"-{JOURNEY_WINDOW_DAYS} days",),
        )
    leads = [dict(r) for r in rows]
    logger.info(
        "attribution_engine: %d leads in scope, models=%s",
        len(leads),
        ",".join(models),
    )

    processed = 0
    updated = 0
    journey_updates = 0
    first_touch_set = 0
    linear_set = 0
    failures = 0
    for lead in leads:
        try:
            result = _process_lead(lead, models)
            processed += 1
            journey_updates += int(result["renumbered"])
            if result["lead_updated"]:
                updated += 1
            if result["first_touch_piece_id"]:
                first_touch_set += 1
            if result["linear_weight_pieces"]:
                linear_set += 1
        except Exception as exc:  # noqa: BLE001 — keep going
            failures += 1
            logger.exception("attribution_engine: lead %s failed: %s", lead.get("id"), exc)
            db.publish_failures.insert(
                severity="P2",
                source=JOB_NAME,
                failure_type="process_lead_failed",
                failure_detail=f"lead_id={lead.get('id')} err={exc}",
            )

    duration = int(time.monotonic() - started_at)
    status = "ok" if failures == 0 else "warning"
    if failures and processed == 0 and leads:
        status = "failed"
    db.heartbeat.record(
        JOB_NAME,
        status,
        duration,
        rows_written=updated + journey_updates + first_touch_set + linear_set,
        error_message=f"{failures} per-lead failures" if failures else None,
    )
    logger.info(
        "attribution_engine done: status=%s leads=%d last_touch_updates=%d "
        "first_touch_set=%d linear_set=%d journey_updates=%d failures=%d duration=%ds",
        status,
        processed,
        updated,
        first_touch_set,
        linear_set,
        journey_updates,
        failures,
        duration,
    )
    return {
        "leads": processed,
        "leads_updated": updated,
        "first_touch_set": first_touch_set,
        "linear_set": linear_set,
        "journey_updates": journey_updates,
        "failures": failures,
        "status": status,
        "duration_seconds": duration,
        "models": list(models),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_bd_override(spec: str) -> dict[str, str]:
    """Parse ``lead_id=X,content_id=Y,by=name`` into a dict."""
    out: dict[str, str] = {}
    for part in spec.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    missing = {"lead_id", "content_id", "by"} - set(out)
    if missing:
        raise argparse.ArgumentTypeError(
            f"--bd-override missing keys: {missing}; "
            "expected lead_id=X,content_id=Y,by=name"
        )
    return out


def _resolve_models(flag: str) -> tuple[AttributionModel, ...]:
    """Map the ``--model`` CLI value to a model tuple."""
    if flag == "all":
        return ALL_MODELS
    if flag in ("last_touch", "first_touch", "linear"):
        return (flag,)  # type: ignore[return-value]
    raise argparse.ArgumentTypeError(
        f"--model must be one of all|last_touch|first_touch|linear (got {flag!r})"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jobs.attribution_engine",
        description="Run multi-model attribution and (optionally) compute weekly aggregates.",
    )
    p.add_argument(
        "--lead-id",
        type=int,
        default=None,
        help="Only process this lead id (debug/QA)",
    )
    p.add_argument(
        "--model",
        type=str,
        default="all",
        choices=("all", "last_touch", "first_touch", "linear"),
        help="Which attribution model(s) to run/aggregate. Default 'all'.",
    )
    p.add_argument(
        "--bd-override",
        type=str,
        default=None,
        help="Apply BD override: lead_id=X,content_id=Y,by=name (overrides any CRM auto-attribution)",
    )
    p.add_argument(
        "--compute-weekly-aggregates",
        type=str,
        default=None,
        metavar="YYYYWWW",
        help="Compute 5-dim weekly_aggregates for the given ISO week (e.g. 2026W19)",
    )
    p.add_argument(
        "--rerun-stitch",
        action="store_true",
        help=(
            "Alias for the default daily loop, but explicitly re-runs cookie "
            "stitching for every lead in the 7-day window. Use after a backfill "
            "or migration 007 first-application to retro-join anonymous "
            "impression rows to recently-signed-up leads."
        ),
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    args = _build_arg_parser().parse_args()
    models = _resolve_models(args.model)

    # BD override is fire-and-exit; doesn't run the daily loop.
    if args.bd_override:
        spec = _parse_bd_override(args.bd_override)
        try:
            bd_attribution_override(
                lead_id=int(spec["lead_id"]),
                content_id=spec["content_id"],
                bd_name=spec["by"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("bd_attribution_override failed: %s", exc)
            alert("P2", f"bd_attribution_override CLI failed: {exc}")
            return 2
        return 0

    if args.compute_weekly_aggregates:
        try:
            rows = compute_weekly_aggregates(args.compute_weekly_aggregates, models=models)
        except Exception as exc:  # noqa: BLE001
            logger.exception("compute_weekly_aggregates failed: %s", exc)
            alert("P1", f"compute_weekly_aggregates failed: {exc}")
            return 2
        logger.info(
            "compute_weekly_aggregates wrote %d rows for %s (models=%s)",
            rows,
            args.compute_weekly_aggregates,
            ",".join(models),
        )
        return 0

    try:
        summary = run(lead_id=args.lead_id, models=models)
    except Exception as exc:  # noqa: BLE001
        logger.exception("attribution_engine top-level failure: %s", exc)
        return 2
    logger.info("attribution_engine summary: %s", summary)
    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
