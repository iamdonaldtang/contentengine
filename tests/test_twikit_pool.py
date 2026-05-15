"""Tests for ``lib.twikit_pool`` — rotation, cooldowns, persistence, fallback."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from lib.twikit_pool import (
    COOLDOWN_AFTER_FAILURE_SECONDS,
    COOLDOWN_BETWEEN_USES_SECONDS,
    TwikitPool,
    TwikitPoolError,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_accounts(path: Path, n: int) -> Path:
    data = [
        {
            "username": f"user{i}",
            "password": f"pw{i}",
            "email": f"u{i}@example.com",
            "cookies_file": f"runtime/twikit_cookies_{i + 1}.json",
        }
        for i in range(n)
    ]
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture()
def pool_3(tmp_path: Path) -> TwikitPool:
    accounts_path = _write_accounts(tmp_path / "accounts.json", 3)
    state_path = tmp_path / "state.json"
    pool = TwikitPool.load(accounts_path, state_path)
    assert pool is not None
    return pool


# --------------------------------------------------------------------------- #
# Test 1 — round-robin rotation across 3 accounts
# --------------------------------------------------------------------------- #


def test_next_account_round_robin(pool_3: TwikitPool) -> None:
    base = dt.datetime(2026, 5, 13, 10, 0, 0, tzinfo=dt.timezone.utc)

    # Each successive call should pick a *different* account because the 30-min
    # same-account cooldown blocks immediate re-use.
    a1 = pool_3.next_account(now=base)
    a2 = pool_3.next_account(now=base + dt.timedelta(seconds=1))
    a3 = pool_3.next_account(now=base + dt.timedelta(seconds=2))
    a4 = pool_3.next_account(now=base + dt.timedelta(seconds=3))  # should be None — all in 30-min window

    assert a1 is not None and a2 is not None and a3 is not None
    assert {a1.username, a2.username, a3.username} == {"user0", "user1", "user2"}
    # 4th call within the cooldown window returns None.
    assert a4 is None

    # After 30+ min, user0 should be selectable again.
    later = base + dt.timedelta(seconds=COOLDOWN_BETWEEN_USES_SECONDS + 5)
    a5 = pool_3.next_account(now=later)
    assert a5 is not None
    assert a5.username == "user0"


# --------------------------------------------------------------------------- #
# Test 2 — account marked failed is skipped
# --------------------------------------------------------------------------- #


def test_marked_cooled_account_is_skipped(pool_3: TwikitPool) -> None:
    base = dt.datetime(2026, 5, 13, 10, 0, 0, tzinfo=dt.timezone.utc)

    # Cool user0 for 2 h.
    pool_3.mark_failure("user0", "test 401", now=base)

    # Next selection should skip user0 entirely.
    a1 = pool_3.next_account(now=base + dt.timedelta(seconds=1))
    a2 = pool_3.next_account(now=base + dt.timedelta(seconds=2))
    assert a1 is not None and a2 is not None
    assert {a1.username, a2.username} == {"user1", "user2"}

    # Within failure-cooldown window, user0 must still be skipped.
    mid_cooldown = base + dt.timedelta(seconds=COOLDOWN_AFTER_FAILURE_SECONDS // 2)
    a3 = pool_3.next_account(now=mid_cooldown)
    assert a3 is not None
    assert a3.username != "user0"  # only user1/user2 viable

    # After both failure cooldown AND same-account cooldown expire, user0 is
    # selectable again. We don't care which account the round-robin lands on
    # first — only that user0 is reachable within one full rotation.
    after_full_cooldown = base + dt.timedelta(
        seconds=max(COOLDOWN_AFTER_FAILURE_SECONDS, COOLDOWN_BETWEEN_USES_SECONDS) + 5
    )
    seen_usernames: set[str] = set()
    for _ in range(3):
        picked = pool_3.next_account(now=after_full_cooldown)
        if picked is None:
            break
        seen_usernames.add(picked.username)
    assert "user0" in seen_usernames


# --------------------------------------------------------------------------- #
# Test 3 — all accounts cooled → next_account() returns None
# --------------------------------------------------------------------------- #


def test_all_accounts_cooled_returns_none(pool_3: TwikitPool) -> None:
    base = dt.datetime(2026, 5, 13, 10, 0, 0, tzinfo=dt.timezone.utc)
    for u in ("user0", "user1", "user2"):
        pool_3.mark_failure(u, "all burnt", now=base)

    assert pool_3.next_account(now=base + dt.timedelta(seconds=1)) is None
    assert pool_3.available_count(now=base + dt.timedelta(seconds=1)) == 0


# --------------------------------------------------------------------------- #
# Test 4 — state persistence
# --------------------------------------------------------------------------- #


def test_state_persistence_round_trip(tmp_path: Path) -> None:
    accounts_path = _write_accounts(tmp_path / "accounts.json", 3)
    state_path = tmp_path / "state.json"

    pool1 = TwikitPool.load(accounts_path, state_path)
    assert pool1 is not None
    base = dt.datetime(2026, 5, 13, 10, 0, 0, tzinfo=dt.timezone.utc)

    a1 = pool1.next_account(now=base)
    assert a1 is not None and a1.username == "user0"
    pool1.mark_failure("user2", "rate limit", now=base)

    # New pool instance, same files → should remember user0 last_used + user2 cooled.
    pool2 = TwikitPool.load(accounts_path, state_path)
    assert pool2 is not None

    # user0 still within 30-min cooldown from last_used.
    a2 = pool2.next_account(now=base + dt.timedelta(seconds=10))
    assert a2 is not None
    assert a2.username == "user1"  # user0 used recently, user2 cooled → user1

    # user2 still cooled.
    cool_state = pool2._state["user2"]  # type: ignore[attr-defined]
    assert cool_state.cooled_until is not None
    assert cool_state.cooled_until > base


# --------------------------------------------------------------------------- #
# Test 5 — missing config → load() returns None, caller handles
# --------------------------------------------------------------------------- #


def test_missing_config_returns_none(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"

    # Unset path.
    assert TwikitPool.load(None, state_path) is None
    assert TwikitPool.load("", state_path) is None

    # Non-existent path.
    assert TwikitPool.load(tmp_path / "does_not_exist.json", state_path) is None

    # Empty array.
    p = tmp_path / "empty.json"
    p.write_text("[]", encoding="utf-8")
    assert TwikitPool.load(p, state_path) is None


def test_malformed_config_raises(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not json at all", encoding="utf-8")
    with pytest.raises(TwikitPoolError):
        TwikitPool.load(bad_json, state_path)

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text('{"not":"a list"}', encoding="utf-8")
    with pytest.raises(TwikitPoolError):
        TwikitPool.load(wrong_shape, state_path)

    missing_field = tmp_path / "missing.json"
    missing_field.write_text(
        json.dumps([{"username": "u", "email": "e"}]), encoding="utf-8"
    )
    with pytest.raises(TwikitPoolError):
        TwikitPool.load(missing_field, state_path)


def test_mark_success_clears_failure_count(pool_3: TwikitPool) -> None:
    base = dt.datetime(2026, 5, 13, 10, 0, 0, tzinfo=dt.timezone.utc)
    pool_3.mark_failure("user0", "transient blip", now=base)
    assert pool_3._state["user0"].failure_count == 1  # type: ignore[attr-defined]

    pool_3.mark_success("user0")
    assert pool_3._state["user0"].failure_count == 0  # type: ignore[attr-defined]
