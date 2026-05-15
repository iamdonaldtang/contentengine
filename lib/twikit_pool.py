"""Twikit account cookie pool — multi-account rotation for KOL Watch fallback.

PRD §2.10: when the X Premium API trips a quota, the engine falls back to the
third-party Twikit scraper. A single X account hammered every Sunday burns
out fast (rate limits, captcha challenges, eventual suspension), so prod runs
a **rotating pool of 3-5 accounts** with cooldowns:

  * 30 minutes minimum between two uses of the same account
  * 2 hours cooldown after any failure (login error, 401, 429, ...)
  * Round-robin selection with last-used timestamps persisted on disk

Config shape — JSON file at ``$TWIKIT_ACCOUNTS_PATH`` (default
``runtime/twikit_accounts.json``)::

    [
      {"username":"a", "password":"...", "email":"...",
       "cookies_file":"runtime/twikit_cookies_1.json"},
      ...
    ]

State persisted to ``runtime/twikit_pool_state.json``::

    {
      "<username>": {"last_used": "<iso>", "cooled_until": "<iso>|null",
                     "failure_count": 0}
    }

Public surface:

  * :class:`TwikitAccount` — dataclass for one credential row
  * :class:`TwikitPool`   — pool with ``next_account() / mark_failure() /
    mark_success()`` and on-disk persistence

If the config file is missing / empty / unreadable, :meth:`TwikitPool.load`
returns ``None`` and the caller is expected to fall back to single-account
mode via legacy ``TWIKIT_USERNAME / TWIKIT_PASSWORD / TWIKIT_EMAIL`` env vars.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


COOLDOWN_BETWEEN_USES_SECONDS: int = 30 * 60  # 30 min between same-account uses
COOLDOWN_AFTER_FAILURE_SECONDS: int = 2 * 60 * 60  # 2 h on login fail / 401 / 429


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class TwikitPoolError(Exception):
    """Raised on config parse / validation errors. Empty/missing file is NOT
    an error — callers get ``None`` and fall back to single-account mode."""


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TwikitAccount:
    """One row from ``twikit_accounts.json``."""

    username: str
    password: str
    email: str
    cookies_file: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any], idx: int) -> TwikitAccount:
        missing = [k for k in ("username", "password") if not (raw.get(k) or "").strip()]
        if missing:
            raise TwikitPoolError(
                f"twikit account #{idx} missing required field(s): {missing}"
            )
        cookies_file = raw.get("cookies_file") or f"runtime/twikit_cookies_{idx + 1}.json"
        return cls(
            username=str(raw["username"]).strip(),
            password=str(raw["password"]),
            email=str(raw.get("email") or "").strip(),
            cookies_file=str(cookies_file).strip(),
        )


@dataclass(slots=True)
class _AccountState:
    """Per-account runtime state persisted to ``twikit_pool_state.json``."""

    last_used: dt.datetime | None = None
    cooled_until: dt.datetime | None = None
    failure_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "cooled_until": self.cooled_until.isoformat() if self.cooled_until else None,
            "failure_count": int(self.failure_count),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> _AccountState:
        def _parse(v: Any) -> dt.datetime | None:
            if not v:
                return None
            if isinstance(v, dt.datetime):
                return v
            try:
                return dt.datetime.fromisoformat(str(v))
            except ValueError:
                logger.warning("twikit_pool: bad datetime in state: %r", v)
                return None

        return cls(
            last_used=_parse(raw.get("last_used")),
            cooled_until=_parse(raw.get("cooled_until")),
            failure_count=int(raw.get("failure_count") or 0),
        )


# --------------------------------------------------------------------------- #
# Pool
# --------------------------------------------------------------------------- #


@dataclass
class TwikitPool:
    """Round-robin Twikit account pool with cooldowns + on-disk state.

    Construct via :meth:`load` rather than directly — that handles missing
    files and validation. Direct construction is only used by tests.
    """

    accounts: list[TwikitAccount]
    state_path: Path
    _state: dict[str, _AccountState] = field(default_factory=dict)
    _cursor: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- factory -------------------------------------------------------- #

    @classmethod
    def load(
        cls,
        accounts_path: str | os.PathLike[str] | None,
        state_path: str | os.PathLike[str],
    ) -> TwikitPool | None:
        """Return a populated pool, or ``None`` if config missing / empty.

        Raises :class:`TwikitPoolError` on malformed JSON or missing required
        fields — bad config must surface loudly (NO silent failures).
        """
        if not accounts_path:
            logger.info("twikit_pool: TWIKIT_ACCOUNTS_PATH unset → single-account fallback")
            return None
        ap = Path(accounts_path).expanduser()
        if not ap.is_file():
            logger.info("twikit_pool: accounts file %s not found → single-account fallback", ap)
            return None
        try:
            with ap.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except json.JSONDecodeError as exc:
            raise TwikitPoolError(f"twikit accounts file {ap} is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise TwikitPoolError(f"cannot read twikit accounts file {ap}: {exc}") from exc

        if not isinstance(raw, list):
            raise TwikitPoolError(
                f"twikit accounts file {ap} must contain a JSON array, got {type(raw).__name__}"
            )
        if not raw:
            logger.info("twikit_pool: accounts file %s is empty → single-account fallback", ap)
            return None

        accounts: list[TwikitAccount] = []
        seen_usernames: set[str] = set()
        for idx, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise TwikitPoolError(
                    f"twikit accounts[{idx}] must be a JSON object, got {type(entry).__name__}"
                )
            acct = TwikitAccount.from_dict(entry, idx)
            if acct.username in seen_usernames:
                raise TwikitPoolError(
                    f"twikit accounts[{idx}]: duplicate username {acct.username!r}"
                )
            seen_usernames.add(acct.username)
            accounts.append(acct)

        pool = cls(accounts=accounts, state_path=Path(state_path).expanduser())
        pool._load_state()
        logger.info(
            "twikit_pool: loaded %d accounts from %s (state=%s)",
            len(accounts),
            ap,
            pool.state_path,
        )
        return pool

    # -- state I/O ------------------------------------------------------ #

    def _load_state(self) -> None:
        sp = self.state_path
        if not sp.is_file():
            self._state = {acct.username: _AccountState() for acct in self.accounts}
            return
        try:
            with sp.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "twikit_pool: state file %s unreadable (%s) → resetting", sp, exc
            )
            self._state = {acct.username: _AccountState() for acct in self.accounts}
            return
        if not isinstance(raw, dict):
            logger.warning("twikit_pool: state file %s wrong shape → resetting", sp)
            self._state = {acct.username: _AccountState() for acct in self.accounts}
            return
        self._state = {
            acct.username: _AccountState.from_json(raw.get(acct.username) or {})
            for acct in self.accounts
        }

    def _save_state(self) -> None:
        sp = self.state_path
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            payload = {u: s.to_json() for u, s in self._state.items()}
            tmp = sp.with_suffix(sp.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, sp)
        except OSError as exc:
            # Persistence failure is logged but does not abort the run —
            # in-memory state still functions for this process. We log loudly
            # (WARNING) so monitoring catches a persistent disk issue.
            logger.warning("twikit_pool: failed to persist state to %s: %s", sp, exc)

    # -- selection ------------------------------------------------------ #

    def next_account(
        self, now: dt.datetime | None = None
    ) -> TwikitAccount | None:
        """Return the next usable account, or ``None`` if all are cooled.

        Selection strategy:
          1. Walk the pool starting from ``self._cursor`` (round-robin)
          2. Skip any account whose ``cooled_until`` is in the future
          3. Skip any account whose ``last_used`` is within
             :data:`COOLDOWN_BETWEEN_USES_SECONDS`
          4. Stamp ``last_used = now`` and persist state
        """
        with self._lock:
            now = now or dt.datetime.now(dt.timezone.utc)
            n = len(self.accounts)
            if n == 0:
                return None

            for offset in range(n):
                idx = (self._cursor + offset) % n
                acct = self.accounts[idx]
                st = self._state.setdefault(acct.username, _AccountState())
                if st.cooled_until and st.cooled_until > now:
                    logger.debug(
                        "twikit_pool: skip %s (cooled until %s)",
                        acct.username,
                        st.cooled_until,
                    )
                    continue
                if (
                    st.last_used
                    and (now - st.last_used).total_seconds() < COOLDOWN_BETWEEN_USES_SECONDS
                ):
                    logger.debug(
                        "twikit_pool: skip %s (last used %s, within 30min)",
                        acct.username,
                        st.last_used,
                    )
                    continue
                # Mark used.
                st.last_used = now
                self._cursor = (idx + 1) % n
                self._save_state()
                logger.info("twikit_pool: selected account=%s", acct.username)
                return acct

            logger.warning("twikit_pool: all %d accounts cooled / rate-limited", n)
            return None

    # -- feedback ------------------------------------------------------- #

    def mark_failure(
        self,
        username: str,
        reason: str,
        cooldown_seconds: int = COOLDOWN_AFTER_FAILURE_SECONDS,
        now: dt.datetime | None = None,
    ) -> None:
        """Mark an account as failed; cool it for ``cooldown_seconds`` (2 h)."""
        with self._lock:
            now = now or dt.datetime.now(dt.timezone.utc)
            st = self._state.setdefault(username, _AccountState())
            st.failure_count += 1
            st.cooled_until = now + dt.timedelta(seconds=cooldown_seconds)
            self._save_state()
            logger.warning(
                "twikit_pool: marked %s as failed (%s); cooled until %s (fail_count=%d)",
                username,
                reason,
                st.cooled_until,
                st.failure_count,
            )

    def mark_success(self, username: str) -> None:
        """Reset failure counter on a successful run."""
        with self._lock:
            st = self._state.setdefault(username, _AccountState())
            if st.failure_count:
                logger.info(
                    "twikit_pool: %s succeeded; clearing fail_count (was %d)",
                    username,
                    st.failure_count,
                )
            st.failure_count = 0
            self._save_state()

    # -- introspection -------------------------------------------------- #

    def size(self) -> int:
        return len(self.accounts)

    def available_count(self, now: dt.datetime | None = None) -> int:
        """Number of accounts not currently cooled. Helpful for warnings."""
        now = now or dt.datetime.now(dt.timezone.utc)
        count = 0
        for acct in self.accounts:
            st = self._state.get(acct.username) or _AccountState()
            if st.cooled_until and st.cooled_until > now:
                continue
            if (
                st.last_used
                and (now - st.last_used).total_seconds() < COOLDOWN_BETWEEN_USES_SECONDS
            ):
                continue
            count += 1
        return count


__all__ = [
    "TwikitAccount",
    "TwikitPool",
    "TwikitPoolError",
    "COOLDOWN_AFTER_FAILURE_SECONDS",
    "COOLDOWN_BETWEEN_USES_SECONDS",
]
