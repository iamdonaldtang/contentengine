"""X (Twitter) Premium API v2 client.

Why a dedicated module (vs piggy-backing on Postiz analytics): Postiz returns
aggregate totals only. X Premium API v2 returns ``organic_metrics`` — in
particular ``impression_count_30m`` and ``engagement_count_30m`` — which the
30-minute health snapshot relies on (see Metrics doc §2.2 + Wave1 B3 §2).

This module is a pure transport adapter. No SQLite writes, no UTM parsing,
no rate-limit bookkeeping beyond the ``@retryable`` decorator.

Endpoints used
--------------
* ``GET /2/tweets/{id}?tweet.fields=public_metrics,non_public_metrics,organic_metrics&expansions=author_id``
* ``GET /2/tweets/search/recent?query=conversation_id:{tweet_id}&...``
* ``GET /2/users/by/username/{username}``
* ``GET /2/users/{id}/tweets?start_time=...&max_results=...``

Auth: ``Authorization: Bearer {X_BEARER_TOKEN}``
Budget: $200/mo plan = 100K reads/mo — well above need.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import requests

from lib.lark import alert
from lib.retry import retryable

logger = logging.getLogger(__name__)


_API_BASE = "https://api.twitter.com/2"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class TwitterXError(RuntimeError):
    """Raised on any non-recoverable X API error (after retries)."""


class TwitterXConfigError(TwitterXError):
    """Raised when ``X_BEARER_TOKEN`` is missing at call time."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class TwitterXClient:
    """X Premium API v2 client.

    Configuration is read lazily from env: import-time absence of
    ``X_BEARER_TOKEN`` only emits a WARNING; the first method call raises
    :class:`TwitterXConfigError`.
    """

    timeout: int = 30
    base_url: str = _API_BASE

    def __init__(self) -> None:
        self._bearer: str = os.environ.get("X_BEARER_TOKEN") or ""
        if not self._bearer:
            logger.warning(
                "TwitterXClient: X_BEARER_TOKEN not set; calls will fail "
                "until configured."
            )

    # -- private helpers --------------------------------------------------- #

    def _ensure_configured(self) -> None:
        if not self._bearer:
            raise TwitterXConfigError(
                "X_BEARER_TOKEN must be set in the environment before calling "
                "TwitterXClient methods."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer}",
            "Accept": "application/json",
        }

    @retryable()
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(
                url, headers=self._headers(), params=params, timeout=self.timeout
            )
        except requests.RequestException as exc:
            logger.error("X GET %s failed: %s", path, exc)
            raise TwitterXError(f"GET {path} transport error: {exc}") from exc
        if resp.status_code == 429:
            # 429 from X is recoverable via retry budget; let retryable handle it.
            logger.warning("X GET %s -> 429 rate-limited", path)
            raise TwitterXError(f"GET {path} rate-limited (429)")
        if resp.status_code >= 400:
            logger.error("X GET %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise TwitterXError(f"GET {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise TwitterXError(f"GET {path} returned non-JSON body") from exc

    # -- public surface ---------------------------------------------------- #

    def get_tweet_metrics(self, tweet_id: str) -> dict[str, Any]:
        """Return public + non-public + organic metrics for a single tweet.

        Args:
            tweet_id: Native X tweet id (string of digits).

        Returns:
            The raw ``data`` object from the X response, containing keys
            ``id``, ``public_metrics``, ``non_public_metrics``,
            ``organic_metrics`` (and possibly ``author_id``). See
            Metrics doc §2.2 for exact shapes.

        Raises:
            TwitterXConfigError: env var missing.
            TwitterXError: HTTP error after retries.
        """
        params: dict[str, Any] = {
            "tweet.fields": "public_metrics,non_public_metrics,organic_metrics",
            "expansions": "author_id",
        }
        raw = self._get(f"/tweets/{tweet_id}", params=params)
        if not isinstance(raw, dict) or "data" not in raw:
            raise TwitterXError(f"tweets/{tweet_id} missing 'data' key: {raw!r:.200s}")
        data = raw["data"]
        if not isinstance(data, dict):
            raise TwitterXError(f"tweets/{tweet_id} 'data' not an object")
        logger.info(
            "twitter_x.get_tweet_metrics(tweet_id=%s) ok; impressions=%s",
            tweet_id,
            data.get("public_metrics", {}).get("impression_count"),
        )
        return data

    def get_replies(self, tweet_id: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Return up to ``max_results`` replies in the conversation.

        Uses the recent-search endpoint with ``conversation_id:{tweet_id}``;
        this captures replies posted within the last ~7 days.

        Args:
            tweet_id: Native X tweet id of the root tweet.
            max_results: 10-100 per X API spec; clamped to [10, 100].

        Returns:
            List of reply tweet dicts (``id``, ``text``, ``author_id``,
            ``public_metrics``, ...). Empty list when none.

        Raises:
            TwitterXConfigError / TwitterXError.
        """
        capped = max(10, min(100, max_results))
        params: dict[str, Any] = {
            "query": f"conversation_id:{tweet_id}",
            "tweet.fields": "author_id,public_metrics,created_at",
            "max_results": capped,
        }
        raw = self._get("/tweets/search/recent", params=params)
        if not isinstance(raw, dict):
            raise TwitterXError("search/recent returned non-object")
        replies = raw.get("data") or []
        if not isinstance(replies, list):
            raise TwitterXError("search/recent 'data' not a list")
        logger.info(
            "twitter_x.get_replies(tweet_id=%s) -> %d replies (cap=%d)",
            tweet_id,
            len(replies),
            capped,
        )
        return replies

    def get_user_tweets(
        self,
        handle: str,
        days_back: int = 7,
        max_results: int = 30,
    ) -> list[dict[str, Any]]:
        """Return tweets by ``handle`` posted within the last ``days_back`` days.

        Performs two calls: first resolves the handle to a user id via
        ``/users/by/username/{username}``, then fetches the timeline via
        ``/users/{id}/tweets``.

        Args:
            handle: X handle, with or without leading ``@``.
            days_back: How many days of history to fetch. Capped to ~31 by
                the API for the standard plan; we clamp to [1, 31].
            max_results: 5-100 per X API. Clamped to [5, 100].

        Returns:
            List of tweet dicts (``id``, ``text``, ``created_at``,
            ``public_metrics``, ...). Empty list when none.

        Raises:
            TwitterXConfigError / TwitterXError.
        """
        clean_handle = handle.lstrip("@").strip()
        if not clean_handle:
            raise TwitterXError("get_user_tweets: empty handle")

        # 1. Resolve handle -> user id
        lookup = self._get(f"/users/by/username/{clean_handle}")
        if not isinstance(lookup, dict) or not isinstance(lookup.get("data"), dict):
            raise TwitterXError(
                f"users/by/username/{clean_handle} missing 'data': {lookup!r:.200s}"
            )
        user_id = lookup["data"].get("id")
        if not user_id:
            raise TwitterXError(f"users/by/username/{clean_handle} had no id")

        # 2. Fetch timeline
        clamped_days = max(1, min(31, days_back))
        clamped_max = max(5, min(100, max_results))
        start_time = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=clamped_days)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        params: dict[str, Any] = {
            "start_time": start_time,
            "max_results": clamped_max,
            "tweet.fields": "created_at,public_metrics,conversation_id,referenced_tweets",
        }
        raw = self._get(f"/users/{user_id}/tweets", params=params)
        if not isinstance(raw, dict):
            raise TwitterXError(f"users/{user_id}/tweets returned non-object")
        tweets = raw.get("data") or []
        if not isinstance(tweets, list):
            raise TwitterXError(f"users/{user_id}/tweets 'data' not a list")
        logger.info(
            "twitter_x.get_user_tweets(handle=%s, days_back=%d) -> %d tweets",
            clean_handle,
            clamped_days,
            len(tweets),
        )
        return tweets


# Module-level singleton
twitter_x: TwitterXClient = TwitterXClient()


__all__ = ["twitter_x", "TwitterXClient", "TwitterXError", "TwitterXConfigError"]


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    if not os.environ.get("X_BEARER_TOKEN"):
        logger.warning("Skip smoke test: X_BEARER_TOKEN not set")
    else:
        # Use @TaskOnxyz timeline as a stable, read-only probe.
        try:
            tweets = twitter_x.get_user_tweets(handle="TaskOnxyz", days_back=7, max_results=5)
            logger.info("Smoke test ok: fetched %d tweets", len(tweets))
        except TwitterXError as exc:
            logger.exception("Smoke test failed: %s", exc)
            try:
                alert("P1", f"twitter_x smoke test failed: {exc}")
            except Exception:  # noqa: BLE001
                logger.exception("alert() failed too")
