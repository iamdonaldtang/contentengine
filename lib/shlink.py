"""Self-hosted shlink REST v3 client · short-link service for UTM-tagged URLs.

The engine routes every outbound URL through shlink (``l.taskon.xyz``) so we
can swap destinations without re-publishing content and so click attribution
is captured even when the destination strips UTM.

Public surface::

    from lib.shlink import shlink, ShlinkError

    short_url = shlink.shorten("https://taskon.xyz/...", custom_slug="bench-q1",
                                tags=["2026q2", "benchmark"])
    stats     = shlink.stats("bench-q1")
    items     = shlink.list(search_term="benchmark")

Network behaviour:
  * All calls hit ``SHLINK_BASE_URL`` with header ``X-Api-Key: SHLINK_API_KEY``.
  * Explicit ``timeout=15`` on every request.
  * Decorated with :func:`lib.retry.retryable` (default 3 attempts).
  * Failures raise :class:`ShlinkError` after logging WARNING — callers that
    treat the short URL as nice-to-have can ``try/except`` and fall back to
    the long URL.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests
from dotenv import load_dotenv

from lib.retry import retryable

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15


class ShlinkError(Exception):
    """Raised when the shlink REST API returns an error or is unreachable."""


class ShlinkClient:
    """Thin wrapper over the shlink REST v3 API.

    The client lazy-loads credentials from the environment on first use so
    importing the module is side-effect free beyond reading ``.env``.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._base_url: str = ""
        self._api_key: str = ""

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        load_dotenv(override=False)
        self._base_url = (os.environ.get("SHLINK_BASE_URL") or "").rstrip("/")
        self._api_key = os.environ.get("SHLINK_API_KEY", "").strip()
        if not self._base_url:
            raise ShlinkError("SHLINK_BASE_URL not configured")
        if not self._api_key:
            raise ShlinkError("SHLINK_API_KEY not configured")
        self._initialized = True

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @retryable()
    def shorten(
        self,
        long_url: str,
        custom_slug: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Create (or reuse) a short URL for ``long_url`` and return it.

        Args:
            long_url: The destination URL to shorten. Should already include
                any UTM segments — shlink does *not* add them for you.
            custom_slug: Optional custom path (e.g. ``"bench-q1"``). Omit to
                let shlink generate a random slug. If the slug is taken,
                shlink will error unless ``findIfExists`` matches.
            tags: Optional list of tag strings for filtering in the shlink UI.

        Returns:
            The full short URL (e.g. ``"https://l.taskon.xyz/bench-q1"``).

        Raises:
            ShlinkError: On any API failure (after retries).
        """
        self._ensure_initialized()
        if not long_url or not isinstance(long_url, str):
            raise ShlinkError(f"long_url must be a non-empty string; got {long_url!r}")

        body: dict[str, Any] = {
            "longUrl": long_url,
            "findIfExists": True,
        }
        if custom_slug:
            body["customSlug"] = custom_slug
        if tags:
            body["tags"] = list(tags)

        url = f"{self._base_url}/rest/v3/short-urls"
        try:
            resp = requests.post(url, json=body, headers=self._headers(), timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.warning("shlink shorten request failed: %s", exc)
            raise  # let @retryable handle / re-raise

        if not resp.ok:
            logger.warning(
                "shlink shorten HTTP %s: %s", resp.status_code, resp.text[:500]
            )
            raise ShlinkError(
                f"shlink shorten failed: HTTP {resp.status_code} {resp.text[:300]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ShlinkError(f"shlink shorten returned non-JSON: {resp.text[:300]}") from exc

        short_url = payload.get("shortUrl")
        if not short_url:
            raise ShlinkError(f"shlink shorten response missing shortUrl: {payload}")
        return short_url

    @retryable()
    def stats(self, short_code: str) -> dict[str, Any]:
        """Fetch visit statistics for ``short_code``.

        Returns the raw shlink ``/visits`` payload (a dict with ``visits``
        pagination object). Callers typically only need ``visits.pagination``
        or ``visits.data`` — exposing the raw dict keeps us forward-compatible.
        """
        self._ensure_initialized()
        if not short_code:
            raise ShlinkError("short_code must be non-empty")
        url = f"{self._base_url}/rest/v3/short-urls/{short_code}/visits"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.warning("shlink stats request failed: %s", exc)
            raise
        if not resp.ok:
            logger.warning(
                "shlink stats HTTP %s: %s", resp.status_code, resp.text[:500]
            )
            raise ShlinkError(
                f"shlink stats failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ShlinkError(f"shlink stats returned non-JSON: {resp.text[:300]}") from exc

    @retryable()
    def list(self, search_term: str | None = None) -> list[dict[str, Any]]:
        """List short URLs, optionally filtered by ``search_term``.

        Returns the ``shortUrls.data`` array (list of short-URL dicts). When
        there are no matches an empty list is returned.
        """
        self._ensure_initialized()
        url = f"{self._base_url}/rest/v3/short-urls"
        params: dict[str, Any] = {}
        if search_term:
            params["searchTerm"] = search_term
        try:
            resp = requests.get(
                url,
                params=params or None,
                headers=self._headers(),
                timeout=_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("shlink list request failed: %s", exc)
            raise
        if not resp.ok:
            logger.warning(
                "shlink list HTTP %s: %s", resp.status_code, resp.text[:500]
            )
            raise ShlinkError(
                f"shlink list failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ShlinkError(f"shlink list returned non-JSON: {resp.text[:300]}") from exc
        try:
            return list(payload.get("shortUrls", {}).get("data", []))
        except AttributeError as exc:
            raise ShlinkError(f"shlink list response shape unexpected: {payload}") from exc


# Module-level singleton.
shlink: ShlinkClient = ShlinkClient()


__all__ = ["shlink", "ShlinkClient", "ShlinkError"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    try:
        slug = shlink.shorten("https://taskon.xyz/benchmark-report")
        logger.info("self-test shortened url: %s", slug)
    except ShlinkError as exc:
        logger.error("self-test failed: %s", exc)
    except Exception:
        logger.exception("self-test crashed unexpectedly")
