"""Channel Attribution (Markov chain) · monthly (1st Sunday 20:30) cron.

PRD §2.7 V3 + §7 Q3. Replacement for the R ``ChannelAttribution`` package
(non-installable in our container). Pure-Python implementation: nested-dict
transition table + iterative absorption-probability solve.

## Algorithm

1. Pull every user_journey row in the month + the user's signup status
   (a journey ends at CONVERSION if the user has a row in ``leads`` whose
   ``first_seen_at`` falls in the same month, else NULL).
2. Each "channel" state = ``utm_source/utm_medium`` (e.g. ``twitter/thread``).
   Touchpoints without a source default to ``(direct)/(none)``.
3. Build transitions as nested dicts: ``transitions[from_state][to_state]``.
   States = {START, CONVERSION, NULL} ∪ channels.
4. Normalize each row to a probability distribution.
5. Compute baseline conversion probability: P(reach CONVERSION starting from
   START) via fixed-point iteration on the absorption equation
       v[s] = sum_j P(s -> j) * v[j]
   with v[CONVERSION]=1, v[NULL]=0, iterated until ``max |Δv| < 1e-6``
   (or 1000 iterations, whichever first).
6. For each channel C, compute the "removal effect":
   - Build a modified transition where every incoming edge to C is rerouted
     to NULL (i.e. visiting C is equivalent to dropping out).
   - Recompute conversion probability v'.
   - removal_effect[C] = (baseline - v') / baseline
7. credit_share[C] = removal_effect[C] / sum(removal_effect[*]).
   conversion_credit[C] = credit_share[C] * total_conversions.

## Output

* Inserts rows into ``markov_credits`` (one per channel per period).
* Markdown report at ``runtime/markov_attribution_<month>.md``.

CLI::

    python -m jobs.channel_attribution
    python -m jobs.channel_attribution --month 2026-05
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402

logger = logging.getLogger(__name__)

JOB_NAME = "channel_attribution"

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR") or (_ENGINE_ROOT / "runtime"))

START = "__START__"
CONVERSION = "__CONVERSION__"
NULL = "__NULL__"

MAX_ITER = 1000
CONVERGENCE_DELTA = 1e-6


def is_first_sunday_of_month(today: dt.date | None = None) -> bool:
    """True iff ``today`` is the 1st Sunday of its calendar month."""
    today = today or dt.date.today()
    if today.isoweekday() != 7:
        return False
    return today.day <= 7


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #


def _parse_month(month: str) -> tuple[int, int]:
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"month must be YYYY-MM; got {month!r}")
    return int(month[:4]), int(month[5:])


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
# Journey construction
# --------------------------------------------------------------------------- #


def _channel(source: str | None, medium: str | None) -> str:
    s = (source or "(direct)").strip() or "(direct)"
    m = (medium or "(none)").strip() or "(none)"
    return f"{s}/{m}"


def collect_journeys(month_start: str, month_end: str) -> tuple[list[list[str]], int]:
    """Return (journeys, total_conversions).

    A journey = ordered list of channel states followed by CONVERSION or NULL.
    A converting journey is one whose ``user_id`` matches a lead whose
    ``email_hash`` is the same and whose ``first_seen_at`` falls in the month.
    """
    journey_rows = db.fetchall(
        """
        SELECT user_id, utm_source, utm_medium, timestamp, action
        FROM user_journey
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY user_id, timestamp
        """,
        (month_start, month_end),
    )

    # Determine which user_ids converted within the period.
    converted_rows = db.fetchall(
        """
        SELECT DISTINCT email_hash
        FROM leads
        WHERE email_hash IS NOT NULL
          AND first_seen_at BETWEEN ? AND ?
        """,
        (month_start, month_end),
    )
    converters: set[str] = {r["email_hash"] for r in converted_rows if r["email_hash"]}

    by_user: dict[str, list[str]] = defaultdict(list)
    for r in journey_rows:
        uid = r["user_id"]
        if uid is None:
            continue
        ch = _channel(r["utm_source"], r["utm_medium"])
        # Skip pure 'signup' rows whose channel is unknown — they're end-states.
        if r["action"] == "signup" and ch == "(direct)/(none)":
            continue
        by_user[uid].append(ch)

    journeys: list[list[str]] = []
    total_conversions = 0
    for uid, chans in by_user.items():
        # Collapse consecutive duplicate states to keep transitions meaningful.
        deduped: list[str] = []
        for c in chans:
            if not deduped or deduped[-1] != c:
                deduped.append(c)
        if not deduped:
            continue
        terminal = CONVERSION if uid in converters else NULL
        if terminal == CONVERSION:
            total_conversions += 1
        journeys.append([START] + deduped + [terminal])

    logger.info(
        "channel_attribution: built %d journeys (%d conversions) from %d journey rows",
        len(journeys), total_conversions, len(journey_rows),
    )
    return journeys, total_conversions


# --------------------------------------------------------------------------- #
# Markov: transitions, absorption probability, removal effect
# --------------------------------------------------------------------------- #


def build_transitions(journeys: list[list[str]]) -> dict[str, dict[str, float]]:
    """Return row-normalized transition probabilities.

    ``transitions[s][s']`` = P(go to s' next | currently at s).
    Terminal states (CONVERSION, NULL) self-loop with prob 1.0.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in journeys:
        for i in range(len(path) - 1):
            counts[path[i]][path[i + 1]] += 1

    transitions: dict[str, dict[str, float]] = {}
    for s, dests in counts.items():
        total = sum(dests.values())
        if total <= 0:
            continue
        transitions[s] = {d: c / total for d, c in dests.items()}

    # Absorbing self-loops for terminal states.
    transitions.setdefault(CONVERSION, {})[CONVERSION] = 1.0
    transitions.setdefault(NULL, {})[NULL] = 1.0
    return transitions


def absorption_probability(
    transitions: dict[str, dict[str, float]],
    start: str = START,
    target: str = CONVERSION,
) -> float:
    """Return P(eventually reach ``target`` from ``start``) via fixed-point.

    Solves v[s] = sum_j P[s][j] v[j] with v[target]=1, other absorbing=0.
    """
    states = list(transitions.keys())
    # Initial vector: 1 at target, 0 elsewhere. NULL absorbs to 0.
    v: dict[str, float] = {s: 0.0 for s in states}
    v[target] = 1.0
    for _ in range(MAX_ITER):
        delta = 0.0
        new_v = dict(v)
        for s in states:
            if s == target:
                new_v[s] = 1.0
                continue
            if s == NULL:
                new_v[s] = 0.0
                continue
            total = 0.0
            for j, p in transitions[s].items():
                total += p * v.get(j, 0.0)
            new_v[s] = total
            d = abs(new_v[s] - v[s])
            if d > delta:
                delta = d
        v = new_v
        if delta < CONVERGENCE_DELTA:
            break
    return v.get(start, 0.0)


def remove_channel(
    transitions: dict[str, dict[str, float]],
    channel: str,
) -> dict[str, dict[str, float]]:
    """Return a copy where every transition INTO ``channel`` is rerouted to NULL.

    Equivalent to deleting the channel from the funnel: any user who would
    have stepped onto it instead drops out. The channel's own outgoing edges
    are also rerouted (state becomes unreachable).
    """
    new_t: dict[str, dict[str, float]] = {}
    for s, dests in transitions.items():
        if s == channel:
            # Channel collapses to absorbing NULL.
            new_t[s] = {NULL: 1.0}
            continue
        if channel in dests:
            redirected: dict[str, float] = {NULL: 0.0}
            for d, p in dests.items():
                if d == channel:
                    redirected[NULL] = redirected.get(NULL, 0.0) + p
                else:
                    redirected[d] = redirected.get(d, 0.0) + p
            new_t[s] = redirected
        else:
            new_t[s] = dict(dests)
    return new_t


def compute_removal_effects(
    transitions: dict[str, dict[str, float]],
) -> tuple[dict[str, float], float]:
    """Return ({channel: removal_effect}, baseline_conv_prob).

    Removal effect = (baseline - removed) / baseline, clamped to [0, 1].
    If baseline is 0 we report 0 for all channels (no signal).
    """
    baseline = absorption_probability(transitions)
    if baseline <= 0.0:
        logger.warning("channel_attribution: baseline conversion probability is 0; no signal")
        return ({}, 0.0)

    channels = [s for s in transitions if s not in (START, CONVERSION, NULL)]
    effects: dict[str, float] = {}
    for c in channels:
        modified = remove_channel(transitions, c)
        p = absorption_probability(modified)
        # numerical guard
        effect = max(0.0, min(1.0, (baseline - p) / baseline))
        effects[c] = effect
    return effects, baseline


def attribute_credits(
    effects: dict[str, float],
    total_conversions: int,
) -> list[dict[str, Any]]:
    """Normalize removal effects to credit shares + conversion counts."""
    total = sum(effects.values())
    out: list[dict[str, Any]] = []
    for channel, eff in effects.items():
        share = (eff / total) if total > 0 else 0.0
        credit = share * total_conversions
        out.append({
            "channel": channel,
            "removal_effect": eff,
            "credit_share": share,
            "conversion_credit": credit,
        })
    out.sort(key=lambda r: -r["credit_share"])
    return out


def _channel_sample_sizes(journeys: list[list[str]]) -> dict[str, int]:
    """Per-channel distinct-journey count (journey-touch counts, not transition counts)."""
    counts: dict[str, int] = defaultdict(int)
    for path in journeys:
        seen: set[str] = set()
        for s in path:
            if s in (START, CONVERSION, NULL):
                continue
            if s not in seen:
                counts[s] += 1
                seen.add(s)
    return dict(counts)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def write_credits(period: str, rows: list[dict[str, Any]],
                  sample_sizes: dict[str, int]) -> int:
    """Replace the period's rows with the new attribution table."""
    db.execute("DELETE FROM markov_credits WHERE period = ?", (period,))
    inserted = 0
    for r in rows:
        try:
            db.execute(
                """
                INSERT INTO markov_credits (
                    period, channel, removal_effect, credit_share,
                    conversion_credit, sample_size
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    period,
                    r["channel"],
                    r["removal_effect"],
                    r["credit_share"],
                    r["conversion_credit"],
                    sample_sizes.get(r["channel"], 0),
                ),
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "channel_attribution: insert failed channel=%s: %s", r["channel"], exc
            )
            db.publish_failures.insert(
                severity="P2",
                source=JOB_NAME,
                failure_type="markov_credits_insert_failed",
                failure_detail=f"period={period} channel={r['channel']} err={exc}",
            )
    return inserted


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def render_report(month: str, rows: list[dict[str, Any]],
                  sample_sizes: dict[str, int],
                  baseline: float, total_conversions: int) -> str:
    head = [
        f"# Markov Channel Attribution · {month}\n",
        f"_Generated at {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}_\n",
        f"\nBaseline conversion probability (from START): **{baseline:.4f}**",
        f"\nTotal conversions in period: **{total_conversions}**",
        f"\nChannels evaluated: **{len(rows)}**\n",
    ]
    if not rows:
        head.append("\n_No channel signal — empty journeys or zero conversions._\n")
        return "\n".join(head)

    table = [
        "\n| Channel | Touches | Removal Effect | Credit Share | Conversion Credit |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        table.append(
            "| `{ch}` | {n} | {eff:.4f} | {share:.2%} | {cc:.2f} |".format(
                ch=r["channel"],
                n=sample_sizes.get(r["channel"], 0),
                eff=r["removal_effect"],
                share=r["credit_share"],
                cc=r["conversion_credit"],
            )
        )
    return "\n".join(head + table) + "\n"


def write_report(content: str, month: str, output_path: Path | None) -> Path:
    target = output_path or (_RUNTIME_DIR / f"markov_attribution_{month}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("channel_attribution: wrote report to %s (%d bytes)", target, len(content))
    return target


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(month: str | None = None, output: Path | None = None) -> dict[str, Any]:
    started_at = time.monotonic()
    month = month or _previous_month()
    start, end = _month_bounds(month)
    logger.info("channel_attribution: month=%s", month)

    failures = 0
    inserted = 0
    target: Path | None = None
    rows: list[dict[str, Any]] = []
    baseline = 0.0
    total_conversions = 0
    sample_sizes: dict[str, int] = {}

    try:
        journeys, total_conversions = collect_journeys(start, end)
        if not journeys:
            logger.warning("channel_attribution: no journeys in month=%s", month)
        else:
            transitions = build_transitions(journeys)
            effects, baseline = compute_removal_effects(transitions)
            rows = attribute_credits(effects, total_conversions)
            sample_sizes = _channel_sample_sizes(journeys)
            inserted = write_credits(month, rows, sample_sizes)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        logger.exception("channel_attribution: compute failed: %s", exc)
        alert("P2", f"channel_attribution compute failed: {exc}")

    try:
        report = render_report(month, rows, sample_sizes, baseline, total_conversions)
        target = write_report(report, month, output)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        logger.exception("channel_attribution: report write failed: %s", exc)
        alert("P2", f"channel_attribution report write failed: {exc}")

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
        "channel_attribution done: status=%s channels=%d inserted=%d conv=%d baseline=%.4f duration=%ds",
        status, len(rows), inserted, total_conversions, baseline, duration,
    )
    return {
        "month": month,
        "channels": len(rows),
        "inserted": inserted,
        "conversions": total_conversions,
        "baseline_prob": baseline,
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
        prog="python -m jobs.channel_attribution",
        description="Markov-chain data-driven multi-touch attribution.",
    )
    p.add_argument(
        "--month",
        type=str,
        default=None,
        help="YYYY-MM to analyze (default: previous calendar month).",
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
        logger.info("channel_attribution: not the 1st Sunday of month; exiting (use --force to override)")
        return 0
    output_path = Path(args.output) if args.output else None
    try:
        summary = run(month=args.month, output=output_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("channel_attribution top-level failure: %s", exc)
        alert("P1", f"channel_attribution crashed: {exc}")
        return 2
    logger.info("channel_attribution summary: %s", summary)
    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
