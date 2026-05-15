"""A/B Aggregator · monthly (1st Sunday 20:00) cron (PRD §2.7 V3 + §7 Q3).

Aggregates A/B variants of ``utm_term`` within the same ``utm_campaign`` and
compares performance. For each campaign with ≥2 distinct terms we compute:

* sample_size = count of publishings on that arm
* impressions_avg = AVG(metrics_daily.impressions, snapshot_type='7d')
* ctr_mean = SUM(link_clicks) / SUM(impressions)
* ctr_lift_vs_baseline = (arm_ctr / baseline_ctr) - 1
* p_value = two-proportion z-test (arm clicks/impressions vs baseline)

Baseline = the arm with the largest sample_size (publishings rows). Ties are
broken alphabetically on utm_term. The baseline arm always gets
``verdict='baseline'`` and ``ctr_lift_vs_baseline=0`` / ``p_value=NULL``.

Verdict rules:
* baseline arm                                                 -> 'baseline'
* sample_size on either side < min_samples (default 30)         -> 'noise'
* p_value < 0.05  and lift > 0  → 'winner'
* p_value < 0.05  and lift < 0  → 'loser'
* 0.05 ≤ p_value < 0.10         → 'directional'
* p_value ≥ 0.10                 → 'noise'

Note: scipy is intentionally NOT a dependency. The normal CDF is computed via
the stdlib ``math.erf`` per Abramowitz & Stegun 26.2.17.

CLI::

    python -m jobs.ab_aggregator
    python -m jobs.ab_aggregator --month 2026-05
    python -m jobs.ab_aggregator --month 2026-05 --min-samples 50
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402

logger = logging.getLogger(__name__)

JOB_NAME = "ab_aggregator"

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR") or (_ENGINE_ROOT / "runtime"))

DEFAULT_MIN_SAMPLES = 30
P_SIGNIFICANT = 0.05
P_DIRECTIONAL = 0.10

Verdict = Literal["winner", "loser", "directional", "noise", "baseline"]


def is_first_sunday_of_month(today: dt.date | None = None) -> bool:
    """True iff ``today`` is the 1st Sunday of its calendar month."""
    today = today or dt.date.today()
    if today.isoweekday() != 7:
        return False
    return today.day <= 7


# --------------------------------------------------------------------------- #
# Statistics — pure-stdlib normal CDF + two-proportion z-test
# --------------------------------------------------------------------------- #


def normal_cdf(z: float) -> float:
    """Standard normal CDF via ``math.erf`` (Abramowitz & Stegun 26.2.17).

    Used in place of scipy.stats.norm.cdf to keep the engine's dep footprint
    trivial. Accurate to within ~1.5e-7 over the entire real line.
    """
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_proportion_p_value(
    x1: int, n1: int, x2: int, n2: int
) -> float | None:
    """Two-tailed p-value for ``p1 != p2`` via pooled z-test.

    Returns ``None`` if either denominator is 0 or the pooled variance is
    degenerate (both arms 0% or 100%). Caller treats ``None`` as 'noise'.
    """
    if n1 <= 0 or n2 <= 0:
        return None
    p1 = x1 / n1
    p2 = x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    if pooled <= 0.0 or pooled >= 1.0:
        return None
    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2))
    if se <= 0.0:
        return None
    z = (p1 - p2) / se
    # Two-tailed.
    return 2.0 * (1.0 - normal_cdf(abs(z)))


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #


def _parse_month(month: str) -> tuple[int, int]:
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"month must be YYYY-MM (e.g. 2026-05); got {month!r}")
    try:
        return int(month[:4]), int(month[5:])
    except ValueError as exc:
        raise ValueError(f"month parse failed: {month!r}") from exc


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = _parse_month(month)
    first = dt.date(year, mon, 1)
    last_day = calendar.monthrange(year, mon)[1]
    last = dt.date(year, mon, last_day)
    return (f"{first.isoformat()} 00:00:00", f"{last.isoformat()} 23:59:59")


def _previous_month(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    first_this = dt.date(today.year, today.month, 1)
    last_prev = first_this - dt.timedelta(days=1)
    return f"{last_prev.year:04d}-{last_prev.month:02d}"


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _classify(
    p_value: float | None,
    lift: float,
    sample_size: int,
    other_sample: int,
    min_samples: int,
    is_baseline: bool,
) -> Verdict:
    if is_baseline:
        return "baseline"
    if sample_size < min_samples or other_sample < min_samples:
        return "noise"
    if p_value is None:
        return "noise"
    if p_value < P_SIGNIFICANT:
        return "winner" if lift > 0 else "loser"
    if p_value < P_DIRECTIONAL:
        return "directional"
    return "noise"


def _collect_arms(month_start: str, month_end: str) -> dict[str, list[dict[str, Any]]]:
    """Return ``{utm_campaign: [arm_row, ...]}`` for the month.

    Each arm_row aggregates publishings × metrics_daily (snapshot_type='7d').
    """
    rows = db.fetchall(
        """
        SELECT
            pu.utm_campaign        AS utm_campaign,
            pu.utm_term            AS utm_term,
            COUNT(DISTINCT pu.id)  AS sample_size,
            AVG(m.impressions)     AS impressions_avg,
            SUM(COALESCE(m.impressions, 0)) AS impressions_total,
            SUM(COALESCE(m.link_clicks, 0)) AS clicks_total
        FROM publishings pu
        LEFT JOIN metrics_daily m
               ON m.publishing_id = pu.id
              AND m.snapshot_type = '7d'
        WHERE pu.utm_campaign IS NOT NULL
          AND pu.utm_term IS NOT NULL
          AND pu.published_at BETWEEN ? AND ?
        GROUP BY pu.utm_campaign, pu.utm_term
        """,
        (month_start, month_end),
    )
    by_campaign: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        campaign = r["utm_campaign"]
        by_campaign.setdefault(campaign, []).append(dict(r))
    return by_campaign


def _select_baseline(arms: list[dict[str, Any]]) -> dict[str, Any]:
    """Largest sample_size wins; ties broken alphabetically on utm_term."""
    return sorted(
        arms,
        key=lambda a: (-(a["sample_size"] or 0), str(a["utm_term"])),
    )[0]


def compute_ab_results(month: str, min_samples: int) -> list[dict[str, Any]]:
    """Aggregate + classify all multi-arm campaigns in ``month``."""
    start, end = _month_bounds(month)
    by_campaign = _collect_arms(start, end)
    out: list[dict[str, Any]] = []

    for campaign, arms in by_campaign.items():
        if len(arms) < 2:
            continue  # not an A/B at all
        baseline = _select_baseline(arms)
        b_impr = int(baseline["impressions_total"] or 0)
        b_clk = int(baseline["clicks_total"] or 0)
        baseline_ctr = (b_clk / b_impr) if b_impr > 0 else 0.0

        for arm in arms:
            a_impr = int(arm["impressions_total"] or 0)
            a_clk = int(arm["clicks_total"] or 0)
            a_ctr = (a_clk / a_impr) if a_impr > 0 else 0.0
            is_baseline = arm["utm_term"] == baseline["utm_term"]

            if is_baseline:
                lift = 0.0
                p_value: float | None = None
            elif baseline_ctr <= 0.0:
                # Can't compute relative lift if baseline CTR=0. Mark as None.
                lift = 0.0
                p_value = two_proportion_p_value(a_clk, a_impr, b_clk, b_impr)
            else:
                lift = (a_ctr / baseline_ctr) - 1.0
                p_value = two_proportion_p_value(a_clk, a_impr, b_clk, b_impr)

            verdict = _classify(
                p_value=p_value,
                lift=lift,
                sample_size=int(arm["sample_size"] or 0),
                other_sample=int(baseline["sample_size"] or 0),
                min_samples=min_samples,
                is_baseline=is_baseline,
            )
            out.append({
                "utm_campaign": campaign,
                "utm_term": arm["utm_term"],
                "sample_size": int(arm["sample_size"] or 0),
                "impressions_avg": arm["impressions_avg"],
                "ctr_mean": a_ctr,
                "ctr_lift_vs_baseline": lift,
                "p_value": p_value,
                "verdict": verdict,
            })
    return out


def write_ab_results(rows: list[dict[str, Any]]) -> int:
    """Insert all rows into ``ab_results``. Returns inserted count."""
    inserted = 0
    for r in rows:
        try:
            db.execute(
                """
                INSERT INTO ab_results (
                    utm_campaign, utm_term, sample_size, impressions_avg,
                    ctr_mean, ctr_lift_vs_baseline, p_value, verdict
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["utm_campaign"],
                    r["utm_term"],
                    r["sample_size"],
                    r["impressions_avg"],
                    r["ctr_mean"],
                    r["ctr_lift_vs_baseline"],
                    r["p_value"],
                    r["verdict"],
                ),
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "ab_aggregator: insert failed campaign=%s term=%s: %s",
                r["utm_campaign"], r["utm_term"], exc,
            )
            db.publish_failures.insert(
                severity="P2",
                source=JOB_NAME,
                failure_type="ab_results_insert_failed",
                failure_detail=f"campaign={r['utm_campaign']} term={r['utm_term']} err={exc}",
            )
    return inserted


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.2f}%"


def _fmt_ctr(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.3f}%"


def _fmt_p(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.4f}"


def render_report(month: str, rows: list[dict[str, Any]], min_samples: int) -> str:
    if not rows:
        return f"# A/B Results · {month}\n\n_No multi-arm campaigns in this month._\n"

    # Group by campaign for readable output.
    by_campaign: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_campaign.setdefault(r["utm_campaign"], []).append(r)

    parts = [
        f"# A/B Results · {month}\n",
        f"_Generated at {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}_\n",
        f"_min_samples={min_samples}; verdicts: winner/loser require p<{P_SIGNIFICANT}; "
        f"directional p<{P_DIRECTIONAL}_\n",
        f"\nCampaigns analyzed: **{len(by_campaign)}** · Arms: **{len(rows)}**\n",
    ]
    for campaign, arms in sorted(by_campaign.items()):
        # Sort: baseline first, then by ctr_lift desc.
        arms_sorted = sorted(
            arms,
            key=lambda a: (a["verdict"] != "baseline", -(a["ctr_lift_vs_baseline"] or 0.0)),
        )
        parts.append(f"\n## `{campaign}`\n")
        parts.append("| Term | Samples | Avg Impr | CTR | Lift | p-value | Verdict |")
        parts.append("|---|---:|---:|---:|---:|---:|---|")
        for a in arms_sorted:
            impr_avg = a["impressions_avg"]
            parts.append(
                "| {term} | {n} | {impr} | {ctr} | {lift} | {p} | **{v}** |".format(
                    term=a["utm_term"],
                    n=a["sample_size"],
                    impr=f"{impr_avg:.0f}" if impr_avg is not None else "—",
                    ctr=_fmt_ctr(a["ctr_mean"]),
                    lift=_fmt_pct(a["ctr_lift_vs_baseline"]) if a["verdict"] != "baseline" else "—",
                    p=_fmt_p(a["p_value"]),
                    v=a["verdict"],
                )
            )
    return "\n".join(parts) + "\n"


def write_report(content: str, month: str, output_path: Path | None) -> Path:
    target = output_path or (_RUNTIME_DIR / f"ab_results_{month}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("ab_aggregator: wrote report to %s (%d bytes)", target, len(content))
    return target


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(month: str | None = None, min_samples: int = DEFAULT_MIN_SAMPLES,
        output: Path | None = None) -> dict[str, Any]:
    started_at = time.monotonic()
    month = month or _previous_month()
    logger.info("ab_aggregator: month=%s min_samples=%d", month, min_samples)

    failures = 0
    rows: list[dict[str, Any]] = []
    inserted = 0
    target: Path | None = None
    try:
        rows = compute_ab_results(month, min_samples=min_samples)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        logger.exception("ab_aggregator: compute failed: %s", exc)
        alert("P2", f"ab_aggregator compute failed: {exc}")

    if rows:
        inserted = write_ab_results(rows)

    try:
        report = render_report(month, rows, min_samples=min_samples)
        target = write_report(report, month, output)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        logger.exception("ab_aggregator: report write failed: %s", exc)
        alert("P2", f"ab_aggregator report write failed: {exc}")

    duration = int(time.monotonic() - started_at)
    status = "ok" if failures == 0 else ("warning" if rows else "failed")
    db.heartbeat.record(
        JOB_NAME,
        status,
        duration,
        rows_written=inserted,
        error_message=f"{failures} failures" if failures else None,
    )
    logger.info(
        "ab_aggregator done: status=%s arms=%d inserted=%d failures=%d duration=%ds",
        status, len(rows), inserted, failures, duration,
    )
    return {
        "month": month,
        "arms": len(rows),
        "inserted": inserted,
        "failures": failures,
        "status": status,
        "duration_seconds": duration,
        "report_path": str(target) if target else None,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jobs.ab_aggregator",
        description="Aggregate A/B utm_term variants per utm_campaign and classify verdicts.",
    )
    p.add_argument(
        "--month",
        type=str,
        default=None,
        help="YYYY-MM to analyze (default: previous calendar month).",
    )
    p.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help=f"Minimum sample_size per arm to qualify for stat-sig (default {DEFAULT_MIN_SAMPLES}).",
    )
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Override path for the markdown report.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Bypass the 1st-Sunday-of-month self-gate.",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    args = _build_arg_parser().parse_args()
    if not args.force and not is_first_sunday_of_month():
        logger.info("ab_aggregator: not the 1st Sunday of month; exiting (use --force to override)")
        return 0
    output_path = Path(args.output) if args.output else None
    try:
        summary = run(month=args.month, min_samples=args.min_samples, output=output_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ab_aggregator top-level failure: %s", exc)
        alert("P1", f"ab_aggregator crashed: {exc}")
        return 2
    logger.info("ab_aggregator summary: %s", summary)
    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
