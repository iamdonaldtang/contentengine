"""KOL Watch · Sunday 10:00 cron (PRD §2.10).

For each handle in ``engine/config/kol_watchlist.yaml``, fetch the top 3
highest-engagement tweets over the last 7 days. Primary path = X Premium API
via ``sources.twitter_x``. Fallback = Twikit (third-party scraper) triggered
when three or more KOLs in a row hit a quota/429 from the primary path.

Output:
  * ``runtime/kol_topics_<YYYYWWW>.json``  — ``{kol_handle: [tweet_dict, ...]}``
  * One ``candidates`` row per kept tweet (``source_route=2``, ``status='raw'``)
    so the downstream ``topic_ranker`` can fold them into the weekly pool.

CLI::

    python -m jobs.kol_watch
    python -m jobs.kol_watch --week 2026W19
    python -m jobs.kol_watch --fallback-only
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from lib.db import db
from lib.lark import alert
from lib.twikit_pool import TwikitAccount, TwikitPool, TwikitPoolError
from sources.twitter_x import twitter_x, TwitterXError

logger = logging.getLogger(__name__)

JOB_NAME = "kol_watch"

DEFAULT_TOP_N = 3
DEFAULT_MAX_RESULTS = 30
DEFAULT_DAYS_BACK = 7
QUOTA_TRIP_THRESHOLD = 3  # consecutive 429/quota errors -> flip to fallback


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    env_root = os.environ.get("ENGINE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _runtime_dir() -> Path:
    env_runtime = os.environ.get("RUNTIME_DIR")
    if env_runtime:
        return Path(env_runtime).expanduser().resolve()
    return _engine_root() / "runtime"


def _watchlist_path() -> Path:
    return _engine_root() / "config" / "kol_watchlist.yaml"


def _twikit_cookies_path() -> Path:
    """Legacy single-account cookies path (used only when pool is empty)."""
    return _runtime_dir() / "twikit_cookies.json"


def _twikit_accounts_path() -> Path | None:
    """Resolve the pool config path. Returns ``None`` if env unset.

    Env precedence: ``TWIKIT_ACCOUNTS_PATH`` (explicit) → default
    ``runtime/twikit_accounts.json``. Default is only used when the file
    actually exists — otherwise we return ``None`` so the caller falls back
    to single-account mode without spurious warnings.
    """
    explicit = os.environ.get("TWIKIT_ACCOUNTS_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    default_path = _runtime_dir() / "twikit_accounts.json"
    return default_path if default_path.is_file() else None


def _twikit_pool_state_path() -> Path:
    return _runtime_dir() / "twikit_pool_state.json"


def _resolve_cookies_path(cookies_file: str) -> Path:
    """Resolve a per-account cookies file path. Relative paths anchor to engine root."""
    p = Path(cookies_file).expanduser()
    if p.is_absolute():
        return p
    return _engine_root() / p


def _current_iso_week() -> str:
    today = dt.date.today()
    y, w, _ = today.isocalendar()
    return f"{y}W{w:02d}"


# --------------------------------------------------------------------------- #
# Watchlist loader
# --------------------------------------------------------------------------- #


def _load_handles() -> list[dict[str, Any]]:
    """Flatten the watchlist YAML into a list of ``{handle, tier, ...}`` dicts."""
    path = _watchlist_path()
    if not path.is_file():
        raise FileNotFoundError(f"watchlist missing: {path}")
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    handles: list[dict[str, Any]] = []
    for key in ("pre_read_8", "observe_22"):
        entries = data.get(key) or []
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            handle = e.get("handle")
            if not handle:
                continue
            handles.append(
                {
                    "handle": str(handle).lstrip("@").strip(),
                    "tier": e.get("tier"),
                    "focus": e.get("focus"),
                    "angle": e.get("angle"),
                }
            )
    logger.info("kol_watch: %d handles loaded from %s", len(handles), path)
    return handles


# --------------------------------------------------------------------------- #
# Engagement scoring
# --------------------------------------------------------------------------- #


def _engagement(tweet: dict[str, Any]) -> int:
    """Sum of like / reply / quote / retweet — both X API and Twikit shapes."""
    pm = tweet.get("public_metrics") or {}
    if pm:
        return int(
            (pm.get("like_count") or 0)
            + (pm.get("reply_count") or 0)
            + (pm.get("quote_count") or 0)
            + (pm.get("retweet_count") or 0)
        )
    return int(
        (tweet.get("favorite_count") or tweet.get("likes") or 0)
        + (tweet.get("reply_count") or tweet.get("replies") or 0)
        + (tweet.get("quote_count") or tweet.get("quotes") or 0)
        + (tweet.get("retweet_count") or tweet.get("retweets") or 0)
    )


# --------------------------------------------------------------------------- #
# Primary path: X Premium API
# --------------------------------------------------------------------------- #


def _is_quota_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return ("429" in msg) or ("rate" in msg and "limit" in msg) or ("quota" in msg)


def _fetch_via_primary(
    handles: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Return ``(results, fallback_needed)``.

    Sets ``fallback_needed=True`` if the primary path hits ``>=3`` consecutive
    quota errors. Already-processed handles are kept in ``results``; the
    caller is responsible for re-running the failed tail via the fallback.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    consecutive_quota = 0
    for entry in handles:
        handle = entry["handle"]
        try:
            tweets = twitter_x.get_user_tweets(
                handle, days_back=DEFAULT_DAYS_BACK, max_results=DEFAULT_MAX_RESULTS
            )
        except TwitterXError as exc:
            if _is_quota_error(exc):
                consecutive_quota += 1
                logger.warning(
                    "kol_watch: @%s primary quota error (%d consecutive): %s",
                    handle,
                    consecutive_quota,
                    exc,
                )
                if consecutive_quota >= QUOTA_TRIP_THRESHOLD:
                    logger.warning(
                        "kol_watch: %d consecutive quota errors → switching to fallback",
                        consecutive_quota,
                    )
                    return out, True
                continue
            logger.warning("kol_watch: @%s primary failed: %s", handle, exc)
            consecutive_quota = 0
            db.publish_failures.insert(
                severity="P2",
                source="twitter_x",
                failure_type="kol_watch_primary_failed",
                failure_detail=f"@{handle} err={exc}",
            )
            continue
        consecutive_quota = 0
        out[handle] = tweets
    return out, False


# --------------------------------------------------------------------------- #
# Fallback path: Twikit
# --------------------------------------------------------------------------- #


def _is_auth_or_ratelimit_error(exc: BaseException) -> bool:
    """Match login / auth / rate-limit errors that justify pool rotation."""
    msg = str(exc).lower()
    return (
        "401" in msg
        or "429" in msg
        or "rate" in msg and "limit" in msg
        or "unauthorized" in msg
        or "forbidden" in msg
        or "captcha" in msg
        or "login" in msg
        or "auth" in msg and "fail" in msg
    )


async def _twikit_login_with_account(
    client: Any, account: TwikitAccount
) -> None:
    """Authenticate Twikit for a specific pool account.

    Reuses ``account.cookies_file`` if present, else password-logs in and
    persists fresh cookies. Login failures raise; the caller marks the
    account cooled and rotates.
    """
    cookies_path = _resolve_cookies_path(account.cookies_file)
    if cookies_path.is_file():
        try:
            client.load_cookies(str(cookies_path))
            logger.info(
                "twikit: reused cookies for %s from %s", account.username, cookies_path
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "twikit: cookie load for %s failed (%s); will re-login",
                account.username,
                exc,
            )

    await client.login(
        auth_info_1=account.username,
        auth_info_2=account.email or "",
        password=account.password,
    )
    try:
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        client.save_cookies(str(cookies_path))
        logger.info(
            "twikit: cookies for %s persisted to %s", account.username, cookies_path
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "twikit: cookie save for %s failed: %s", account.username, exc
        )


async def _twikit_login_single_account(client: Any) -> None:
    """Legacy single-account login path using ``TWIKIT_USERNAME/PASSWORD/EMAIL``."""
    cookies_path = _twikit_cookies_path()
    if cookies_path.is_file():
        try:
            client.load_cookies(str(cookies_path))
            logger.info("twikit: reused cookies from %s", cookies_path)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("twikit: cookie load failed (%s); will re-login", exc)

    username = os.environ.get("TWIKIT_USERNAME")
    password = os.environ.get("TWIKIT_PASSWORD")
    email = os.environ.get("TWIKIT_EMAIL")
    if not (username and password):
        raise RuntimeError(
            "TWIKIT_USERNAME and TWIKIT_PASSWORD must be set for fallback login "
            "(or provide TWIKIT_ACCOUNTS_PATH with a pool JSON file)"
        )
    await client.login(
        auth_info_1=username,
        auth_info_2=email or "",
        password=password,
    )
    try:
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        client.save_cookies(str(cookies_path))
        logger.info("twikit: cookies persisted to %s", cookies_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("twikit: cookie save failed: %s", exc)


def _twikit_tweet_to_dict(tweet: Any, handle: str) -> dict[str, Any]:
    """Normalize a Twikit tweet object to the same shape as the X API."""
    return {
        "id": str(getattr(tweet, "id", "")),
        "text": getattr(tweet, "text", "") or getattr(tweet, "full_text", ""),
        "created_at": getattr(tweet, "created_at", None),
        "author_handle": handle,
        "public_metrics": {
            "like_count": getattr(tweet, "favorite_count", 0) or 0,
            "reply_count": getattr(tweet, "reply_count", 0) or 0,
            "quote_count": getattr(tweet, "quote_count", 0) or 0,
            "retweet_count": getattr(tweet, "retweet_count", 0) or 0,
        },
        "_source": "twikit",
    }


def _filter_tweets_since(
    tweets: Any, handle: str, since: dt.datetime
) -> list[dict[str, Any]]:
    """Normalize a list of Twikit tweet objects and drop anything older than ``since``."""
    kept: list[dict[str, Any]] = []
    for t in tweets:
        created = getattr(t, "created_at", None)
        if isinstance(created, str):
            try:
                created_dt = dt.datetime.strptime(
                    created, "%a %b %d %H:%M:%S %z %Y"
                )
            except ValueError:
                created_dt = None
        else:
            created_dt = created
        if created_dt and created_dt < since:
            continue
        kept.append(_twikit_tweet_to_dict(t, handle))
    return kept


async def _fetch_with_client(
    client: Any,
    handles: list[dict[str, Any]],
    since: dt.datetime,
    out: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Run all ``handles`` through one already-logged-in Twikit client.

    Mutates ``out`` in place with collected tweets. Returns a list of handles
    that the caller should retry on the NEXT account (e.g. on 401/429/rate
    limit — symptoms that this account is burnt). On per-handle errors that
    are NOT auth/rate-limit (e.g. "user not found"), records a P2 failure
    and moves on with the same client.
    """
    retry_on_next: list[str] = []
    for entry in handles:
        handle = entry["handle"]
        if handle in out:
            continue
        try:
            user = await client.get_user_by_screen_name(handle)
            tweets = await user.get_tweets("Tweets", count=DEFAULT_MAX_RESULTS)
        except Exception as exc:  # noqa: BLE001
            if _is_auth_or_ratelimit_error(exc):
                # Burnt token — stop using this client, rotate.
                logger.warning(
                    "twikit: auth/rate-limit error on @%s: %s — rotating account",
                    handle,
                    exc,
                )
                # All remaining (incl. current) handles need retry on next account.
                remaining_idx = handles.index(entry)
                for e2 in handles[remaining_idx:]:
                    if e2["handle"] not in out:
                        retry_on_next.append(e2["handle"])
                return retry_on_next
            logger.warning("twikit fallback failed for @%s: %s", handle, exc)
            db.publish_failures.insert(
                severity="P2",
                source="twikit",
                failure_type="kol_watch_fallback_failed",
                failure_detail=f"@{handle} err={exc}",
            )
            continue
        out[handle] = _filter_tweets_since(tweets, handle, since)
    return retry_on_next


async def _fetch_via_twikit_pool_async(
    handles: list[dict[str, Any]],
    since: dt.datetime,
    pool: TwikitPool,
) -> dict[str, list[dict[str, Any]]]:
    """Iterate over the pool until every handle is fetched or all accounts cooled."""
    try:
        from twikit import Client  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "twikit package not installed; cannot run KOL Watch fallback"
        ) from exc

    out: dict[str, list[dict[str, Any]]] = {}
    pending = list(handles)
    accounts_tried = 0

    while pending:
        account = pool.next_account()
        if account is None:
            logger.warning(
                "twikit_pool: no usable accounts (tried %d); %d handles unfetched",
                accounts_tried,
                len(pending),
            )
            db.publish_failures.insert(
                severity="P1",
                source="twikit",
                failure_type="kol_watch_pool_exhausted",
                failure_detail=(
                    f"all {pool.size()} pool accounts cooled; "
                    f"{len(pending)} handles unfetched"
                ),
            )
            break
        accounts_tried += 1

        client = Client("en-US")
        try:
            await _twikit_login_with_account(client, account)
        except Exception as exc:  # noqa: BLE001
            pool.mark_failure(account.username, f"login failed: {exc}")
            db.publish_failures.insert(
                severity="P2",
                source="twikit",
                failure_type="kol_watch_pool_login_failed",
                failure_detail=f"acct={account.username} err={exc}",
            )
            continue

        retry_on_next = await _fetch_with_client(client, pending, since, out)
        if retry_on_next:
            pool.mark_failure(
                account.username,
                f"auth/rate-limit while fetching {len(retry_on_next)} handle(s)",
            )
            pending = [h for h in pending if h["handle"] in retry_on_next]
        else:
            pool.mark_success(account.username)
            # All assigned handles processed (success or per-handle skip).
            pending = [h for h in pending if h["handle"] not in out]
            if not pending:
                break
            # Some failed for non-rotation reasons; further retries unlikely
            # to help on a different account.
            break

    return out


async def _fetch_via_twikit_single_async(
    handles: list[dict[str, Any]],
    since: dt.datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Legacy single-account fallback — used when pool config is absent."""
    try:
        from twikit import Client  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "twikit package not installed; cannot run KOL Watch fallback"
        ) from exc

    client = Client("en-US")
    await _twikit_login_single_account(client)

    out: dict[str, list[dict[str, Any]]] = {}
    await _fetch_with_client(client, handles, since, out)
    return out


def _fetch_via_fallback(
    handles: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Dispatch fallback to pool mode if config exists, else single-account."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DEFAULT_DAYS_BACK)

    accounts_path = _twikit_accounts_path()
    pool: TwikitPool | None = None
    if accounts_path is not None:
        try:
            pool = TwikitPool.load(accounts_path, _twikit_pool_state_path())
        except TwikitPoolError as exc:
            # Malformed config → loud failure; don't silently downgrade.
            alert(
                "P1",
                f"kol_watch: twikit pool config invalid at {accounts_path}: {exc}",
            )
            raise

    if pool is not None and pool.size() > 0:
        coro = _fetch_via_twikit_pool_async(handles, since, pool)
    else:
        coro = _fetch_via_twikit_single_async(handles, since)

    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        # asyncio.run raises RuntimeError when called inside an existing loop.
        # Engine jobs run synchronously so this should not happen, but be safe.
        if "asyncio.run() cannot" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        raise


# --------------------------------------------------------------------------- #
# Output writers
# --------------------------------------------------------------------------- #


def _select_top_n(
    raw: dict[str, list[dict[str, Any]]],
    top_n: int = DEFAULT_TOP_N,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for handle, tweets in raw.items():
        ranked = sorted(tweets, key=_engagement, reverse=True)[:top_n]
        out[handle] = ranked
    return out


def _write_json(week: str, top: dict[str, list[dict[str, Any]]]) -> Path:
    path = _runtime_dir() / f"kol_topics_{week}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(top, fp, ensure_ascii=False, indent=2, default=str)
    logger.info("kol_watch: wrote %s", path)
    return path


def _insert_candidates(
    week: str,
    top: dict[str, list[dict[str, Any]]],
) -> int:
    """One candidates row per kept tweet. Idempotent via deterministic id."""
    written = 0
    for handle, tweets in top.items():
        for tweet in tweets:
            tweet_id = str(tweet.get("id") or "")
            seed = f"{week}|{handle}|{tweet_id}".encode("utf-8")
            cand_id = f"cand-{week}-kol-{hashlib.sha1(seed).hexdigest()[:10]}"
            existing = db.fetchone(
                "SELECT candidate_id FROM candidates WHERE candidate_id = ?",
                (cand_id,),
            )
            if existing:
                continue
            text = (tweet.get("text") or "")[:280]
            try:
                db.candidates.insert(
                    candidate_id=cand_id,
                    source_route=2,
                    title_hypothesis=text,
                    narrative_anchor_potential=None,
                    data_sources_hint=f"https://twitter.com/{handle}/status/{tweet_id}",
                    why_relevant=f"KOL @{handle} 高互动推（engagement={_engagement(tweet)}）",
                    relevance_score_self=None,
                    status="raw",
                )
                written += 1
            except Exception as exc:
                logger.warning(
                    "candidates.insert failed for cand_id=%s: %s", cand_id, exc
                )
    logger.info("kol_watch: %d candidate rows inserted", written)
    return written


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(week: str | None = None, fallback_only: bool = False) -> dict[str, Any]:
    started_at = time.monotonic()
    week_label = week or _current_iso_week()

    try:
        handles = _load_handles()
    except FileNotFoundError as exc:
        alert("P1", f"kol_watch: watchlist missing: {exc}")
        db.heartbeat.record(JOB_NAME, "failed", 0, 0, str(exc))
        raise

    if not handles:
        logger.warning("kol_watch: watchlist empty")
        db.heartbeat.record(JOB_NAME, "warning", 0, 0, "empty_watchlist")
        return {"week": week_label, "kept": 0, "path": None}

    raw: dict[str, list[dict[str, Any]]] = {}
    primary_failed = False

    if not fallback_only:
        try:
            raw, need_fallback = _fetch_via_primary(handles)
            if need_fallback:
                primary_failed = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("kol_watch primary path raised: %s", exc)
            primary_failed = True
            alert("P1", f"kol_watch primary path failed: {exc}")
    else:
        primary_failed = True

    if primary_failed or fallback_only:
        # Run fallback only for the handles we still don't have.
        remaining = [h for h in handles if h["handle"] not in raw]
        try:
            fallback_result = _fetch_via_fallback(remaining)
            raw.update(fallback_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("kol_watch fallback failed: %s", exc)
            alert("P0", f"kol_watch: BOTH primary and fallback failed: {exc}")
            db.heartbeat.record(
                JOB_NAME,
                "failed",
                int(time.monotonic() - started_at),
                0,
                f"primary+fallback failed: {exc}",
            )
            raise

    if not raw:
        msg = "kol_watch: no tweets fetched from any path"
        alert("P1", msg)
        db.heartbeat.record(
            JOB_NAME, "failed", int(time.monotonic() - started_at), 0, msg
        )
        return {"week": week_label, "kept": 0, "path": None}

    top = _select_top_n(raw)
    path = _write_json(week_label, top)
    inserted = _insert_candidates(week_label, top)

    duration = int(time.monotonic() - started_at)
    total_tweets = sum(len(v) for v in top.values())
    status = "ok" if not primary_failed else "warning"
    db.heartbeat.record(
        JOB_NAME,
        status,
        duration,
        rows_written=inserted,
        error_message=("primary→fallback" if primary_failed else None),
    )
    logger.info(
        "kol_watch done: week=%s handles_covered=%d top_tweets=%d candidates=%d "
        "duration=%ds status=%s",
        week_label,
        len(top),
        total_tweets,
        inserted,
        duration,
        status,
    )
    return {
        "week": week_label,
        "handles_covered": len(top),
        "top_tweets": total_tweets,
        "candidates_inserted": inserted,
        "fallback_used": primary_failed,
        "path": str(path),
        "status": status,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jobs.kol_watch",
        description="Fetch top-3 high-engagement tweets per KOL into the candidate pool.",
    )
    p.add_argument(
        "--week",
        type=str,
        default=None,
        help="ISO week label e.g. 2026W19 (default: current ISO week)",
    )
    p.add_argument(
        "--fallback-only",
        action="store_true",
        help="Skip primary X API; use Twikit fallback only",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    args = _build_arg_parser().parse_args()
    try:
        summary = run(week=args.week, fallback_only=args.fallback_only)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kol_watch top-level failure: %s", exc)
        return 2
    logger.info("kol_watch summary: %s", summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
