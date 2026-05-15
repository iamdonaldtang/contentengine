"""Tests for ``jobs.cohort_analysis`` — cohort grouping, D7/D14/D30 boundaries,
partial-month edge cases, and idempotent re-runs.

Each test reloads the module under test so it picks up the per-test
``tmp_db`` singleton (see conftest.py).
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from typing import Any


def _import_cohort_module() -> Any:
    if "jobs.cohort_analysis" in sys.modules:
        del sys.modules["jobs.cohort_analysis"]
    return importlib.import_module("jobs.cohort_analysis")


def _seed_lead(
    tmp_db: Any,
    *,
    email: str,
    first_seen_at: str,
    qualified: bool = False,
    sql_status: str | None = None,
    sql_verified_at: str | None = None,
    last_seen_at: str | None = None,
) -> int:
    """Insert one lead row, return its rowid."""
    return tmp_db.leads.insert(
        email=email,
        email_hash=email.replace("@", "_at_"),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        qualified=1 if qualified else 0,
        sql_status=sql_status,
        sql_verified_at=sql_verified_at,
    )


# --------------------------------------------------------------------------- #
# Test 1 — basic cohort grouping by ISO week
# --------------------------------------------------------------------------- #


def test_cohort_grouping_by_iso_week(tmp_db: Any) -> None:
    """Leads in different ISO weeks should land in different cohorts."""
    mod = _import_cohort_module()

    # 2026W18 = Mon 2026-04-27 .. Sun 2026-05-03
    # 2026W19 = Mon 2026-05-04 .. Sun 2026-05-10
    _seed_lead(tmp_db, email="a@x.com", first_seen_at="2026-04-28 10:00:00")
    _seed_lead(tmp_db, email="b@x.com", first_seen_at="2026-04-30 12:00:00")
    _seed_lead(tmp_db, email="c@x.com", first_seen_at="2026-05-05 09:00:00")

    w18 = mod.compute_cohort("2026W18")
    w19 = mod.compute_cohort("2026W19")

    assert w18["cohort_size"] == 2
    assert w19["cohort_size"] == 1


# --------------------------------------------------------------------------- #
# Test 2 — D7/D14/D30 boundary correctness
# --------------------------------------------------------------------------- #


def test_d7_d14_d30_boundaries(tmp_db: Any) -> None:
    """SQL verifications should bucket correctly across the 7/14/30-day cliffs."""
    mod = _import_cohort_module()
    first = "2026-05-04 09:00:00"
    base = dt.datetime.fromisoformat(first)

    # Three leads in the same cohort (2026W19), with sql_verified_at offsets:
    #   lead1 → 6 days  (D7 + D14 + D30)
    #   lead2 → 10 days (D14 + D30 only)
    #   lead3 → 25 days (D30 only)
    _seed_lead(
        tmp_db, email="l1@x.com", first_seen_at=first,
        qualified=True, sql_status="verified",
        sql_verified_at=(base + dt.timedelta(days=6)).isoformat(sep=" "),
    )
    _seed_lead(
        tmp_db, email="l2@x.com", first_seen_at=first,
        qualified=True, sql_status="verified",
        sql_verified_at=(base + dt.timedelta(days=10)).isoformat(sep=" "),
    )
    _seed_lead(
        tmp_db, email="l3@x.com", first_seen_at=first,
        qualified=True, sql_status="verified",
        sql_verified_at=(base + dt.timedelta(days=25)).isoformat(sep=" "),
    )

    result = mod.compute_cohort("2026W19")
    assert result["cohort_size"] == 3
    assert result["d7_sql"] == 1, result
    assert result["d14_sql"] == 2, result
    assert result["d30_sql"] == 3, result
    # Conversion rates derive from cohort_size.
    assert abs(result["d7_conv_rate"] - 1 / 3) < 1e-9
    assert abs(result["d30_conv_rate"] - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# Test 3 — partial-month / empty cohort handled gracefully
# --------------------------------------------------------------------------- #


def test_partial_month_empty_cohort(tmp_db: Any) -> None:
    """A week with no leads should produce zero counts (not crash)."""
    mod = _import_cohort_module()
    # Seed a lead in 2026W18 but compute 2026W30 (no data).
    _seed_lead(tmp_db, email="far@x.com", first_seen_at="2026-04-28 10:00:00")
    result = mod.compute_cohort("2026W30")
    assert result["cohort_size"] == 0
    assert result["d7_sql"] == 0
    assert result["d30_sql"] == 0
    assert result["d7_conv_rate"] == 0.0
    assert result["d30_conv_rate"] == 0.0


# --------------------------------------------------------------------------- #
# Test 4 — idempotent re-run (no double-counting)
# --------------------------------------------------------------------------- #


def test_idempotent_rerun(tmp_db: Any) -> None:
    """Running ``run()`` twice should NOT create duplicate rows. The PK on
    cohort_week + UPSERT must keep exactly one row per cohort."""
    mod = _import_cohort_module()
    _seed_lead(
        tmp_db, email="x@x.com", first_seen_at="2026-04-28 10:00:00",
        qualified=True, sql_status="verified",
        sql_verified_at="2026-05-01 10:00:00",
    )

    # Run twice. Second run should overwrite, not append.
    mod.run(weeks=4, output=None)
    mod.run(weeks=4, output=None)

    rows = tmp_db.fetchall(
        "SELECT cohort_week, COUNT(*) AS n FROM cohort_metrics GROUP BY cohort_week"
    )
    for r in rows:
        assert r["n"] == 1, f"cohort {r['cohort_week']} duplicated: {r['n']} rows"
