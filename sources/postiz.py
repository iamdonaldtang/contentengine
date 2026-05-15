"""Postiz Public API client.

Postiz (self-hosted at ``content.taskon.xyz`` / ``postiz.taskon.xyz``) is the
**primary social-publishing data source** for the marketing engine. This
module is a thin transport adapter — it never parses business semantics,
never writes to SQLite, never decides policy. Callers in ``jobs/`` do that.

See ``Metrics_Collector_归因引擎_需求文档.md`` §2.1 for the API contract.

Endpoints used
--------------
* ``GET  /public-api/v1/posts?date=YYYY-MM-DD&status=published``
* ``GET  /public-api/v1/analytics/{post_id}``
* ``POST /public-api/v1/posts``  (publish/schedule — used by jobs.schedule_planner)

Auth: ``Authorization: Bearer {POSTIZ_API_KEY}``
Rate limit: 100 req/min (self-hosted; tunable)
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


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class PostizError(RuntimeError):
    """Raised on any non-recoverable Postiz API error (after retries)."""


class PostizConfigError(PostizError):
    """Raised when required environment variables are missing at call time."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class PostizClient:
    """Postiz Public API v1 client.

    Reads configuration lazily from the environment — instantiation never
    raises. The first method call validates that ``POSTIZ_BASE_URL`` and
    ``POSTIZ_API_KEY`` are present and raises :class:`PostizConfigError`
    otherwise.

    Attributes:
        base_url: Postiz instance base URL (no trailing slash).
        timeout: HTTP timeout for every request, in seconds.
    """

    timeout: int = 30

    def __init__(self) -> None:
        self.base_url: str = (os.environ.get("POSTIZ_BASE_URL") or "").rstrip("/")
        self._api_key: str = os.environ.get("POSTIZ_API_KEY") or ""
        if not self.base_url or not self._api_key:
            logger.warning(
                "PostizClient: POSTIZ_BASE_URL or POSTIZ_API_KEY not set; "
                "calls will fail until configured."
            )

    # -- private helpers --------------------------------------------------- #

    def _ensure_configured(self) -> None:
        if not self.base_url or not self._api_key:
            raise PostizConfigError(
                "POSTIZ_BASE_URL and POSTIZ_API_KEY must both be set in the "
                "environment before calling PostizClient methods."
            )

    def _headers(self) -> dict[str, str]:
        # Postiz self-hosted (unified container) uses a RAW Authorization
        # header — Bearer prefix returns 401 "Invalid API key". Cloud
        # api.postiz.com behind nginx also accepts raw. Confirmed against
        # local Postiz 5000 (host port 4007) 2026-05-13.
        return {
            "Authorization": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
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
            logger.error("Postiz GET %s failed: %s", path, exc)
            raise PostizError(f"GET {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            logger.error("Postiz GET %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise PostizError(f"GET {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise PostizError(f"GET {path} returned non-JSON body") from exc

    @retryable()
    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(
                url, headers=self._headers(), json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            logger.error("Postiz POST %s failed: %s", path, exc)
            raise PostizError(f"POST {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            logger.error("Postiz POST %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise PostizError(f"POST {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise PostizError(f"POST {path} returned non-JSON body") from exc

    # -- public surface ---------------------------------------------------- #

    def list_posts(
        self,
        date: str | None = None,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str = "published",
    ) -> list[dict[str, Any]]:
        """Return all posts whose ``published_at`` falls in the date range.

        Self-hosted Postiz Public API requires ``startDate`` + ``endDate``
        ISO 8601 (date or datetime); single ``date`` is accepted as a
        backward-compat shorthand that maps to ``startDate=date 00:00:00,
        endDate=date 23:59:59`` for callers like ``metrics_collector``.

        Args:
            date: Single ISO date ``YYYY-MM-DD`` — convenience for "this
                whole day". Maps to start/end internally.
            start_date / end_date: Explicit ISO range (either date or full
                datetime). If both ``date`` and these are passed, the
                explicit range wins.
            status: Postiz post status filter (default ``"published"``).

        Returns:
            List of raw post dicts. Empty list if no posts.

        Raises:
            PostizConfigError: env vars missing.
            PostizError: HTTP error after retries exhausted.
        """
        if start_date is None and date is not None:
            start_date = f"{date}T00:00:00.000Z"
        if end_date is None and date is not None:
            end_date = f"{date}T23:59:59.999Z"
        if not (start_date and end_date):
            raise PostizError(
                "list_posts: must pass date OR (start_date AND end_date)"
            )
        # If caller passed bare YYYY-MM-DD via start_date/end_date, pad to ISO.
        if "T" not in start_date:
            start_date = f"{start_date}T00:00:00.000Z"
        if "T" not in end_date:
            end_date = f"{end_date}T23:59:59.999Z"

        params: dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "status": status,
        }
        data = self._get("/api/public/v1/posts", params=params)
        posts: list[dict[str, Any]] = []
        if isinstance(data, dict) and isinstance(data.get("posts"), list):
            posts = data["posts"]
        elif isinstance(data, list):
            posts = data
        else:
            logger.warning("Postiz list_posts: unexpected payload shape, returning empty")
        logger.info(
            "postiz.list_posts(start=%s, end=%s, status=%s) -> %d posts",
            start_date, end_date, status, len(posts),
        )
        return posts

    def get_analytics(self, post_id: str) -> dict[str, Any]:
        """Return analytics blob for a single post.

        Args:
            post_id: Postiz internal post id (NOT the platform-native id).

        Returns:
            Dict per Metrics doc §2.1 (post_id, impressions, engagements{...},
            clicks, fetched_at). Shape passes through unmodified.

        Raises:
            PostizConfigError: env vars missing.
            PostizError: HTTP error after retries.
        """
        data = self._get(f"/api/public/v1/analytics/{post_id}")
        if not isinstance(data, dict):
            raise PostizError(f"analytics/{post_id} returned non-object: {type(data).__name__}")
        logger.info("postiz.get_analytics(post_id=%s) ok", post_id)
        return data

    def list_integrations(self) -> list[dict[str, Any]]:
        """Return all connected channels (integrations) with their UUIDs.

        Self-hosted endpoint: ``GET /api/public/v1/integrations``.
        Use the returned ``id`` per platform to populate
        ``config.yaml :: postiz.integrations.<platform_key>``.

        Returns:
            List of integration dicts. Each dict has at least:
                id              — UUID to paste into config.yaml
                name            — human label (account name in Postiz)
                providerIdentifier — "x" / "linkedin-page" / "youtube" / ...

        Raises:
            PostizConfigError: env vars missing.
            PostizError: HTTP error after retries.
        """
        data = self._get("/api/public/v1/integrations")
        if isinstance(data, dict) and isinstance(data.get("integrations"), list):
            items = data["integrations"]
        elif isinstance(data, list):
            items = data
        else:
            raise PostizError(
                f"list_integrations: unexpected shape: {type(data).__name__}"
            )
        logger.info("postiz.list_integrations -> %d connected channels", len(items))
        return items

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish a new post via Postiz (raw payload helper).

        Args:
            payload: Per Postiz API — content + platforms + scheduledAt.
                Caller is responsible for shape correctness. Use
                :meth:`create_post` for the strict-typed wrapper.

        Returns:
            Postiz response dict (post id, status, ...).

        Raises:
            PostizConfigError / PostizError as above.
        """
        data = self._post("/api/public/v1/posts", payload)
        if not isinstance(data, dict) and not isinstance(data, list):
            raise PostizError(f"publish returned unexpected type: {type(data).__name__}")
        logger.info("postiz.publish ok; response type=%s", type(data).__name__)
        # Normalise list responses (some Postiz builds return [{...}]) into a
        # dict so callers see a stable shape.
        return data if isinstance(data, dict) else {"posts": data}

    def create_post(
        self,
        *,
        integration_id: str,
        content: str,
        scheduled_at: dt.datetime,
        media_urls: list[str] | None = None,
        post_type: str = "schedule",
        tags: list[str] | None = None,
        short_link: bool = False,
        extra_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Schedule a single platform post via Postiz Public API.

        Wraps :meth:`publish` with the canonical Postiz payload shape so
        callers don't need to know the on-the-wire JSON conventions. The
        return value is the raw response (Postiz post id is extractable as
        ``resp.get("posts", [{}])[0].get("id")`` or — on older Postiz
        versions — ``resp.get("id")``; we return both flavours unchanged).

        Args:
            integration_id: Postiz integration UUID (one per platform per
                account; see ``config.yaml :: postiz.integrations``).
            content: Platform-specific text (already UTM-tagged, voice-checked).
                For multi-tweet X threads use newlines between tweets — Postiz
                splits on a configurable separator; the Postiz UI default is
                ``\\n\\n``.
            scheduled_at: Aware datetime in any timezone. Serialised as ISO
                ``YYYY-MM-DDTHH:MM:SS.sssZ`` in UTC for Postiz.
            media_urls: Optional list of image / video URLs (already uploaded
                to Postiz CDN or accessible to it).
            post_type: ``"schedule"`` (default — picks ``scheduled_at``) or
                ``"draft"`` to land in Postiz as a draft for human review.
            tags: Optional list of Postiz tag strings.
            short_link: Whether Postiz should auto-shorten URLs in ``content``.
                We pass through — TaskOn does its own shortening via shlink, so
                default is False.
            extra_settings: Additional per-platform settings forwarded into
                ``posts[0].settings`` (e.g. Twitter ``in_reply_to``, LinkedIn
                ``audience``). Keys are platform-specific; pass-through.

        Returns:
            The Postiz response payload (a dict; list responses are wrapped
            with key ``"posts"`` for stability).

        Raises:
            PostizConfigError: env vars missing.
            PostizError: integration_id empty, request fails, or response
                shape unparseable.
        """
        if not integration_id or not isinstance(integration_id, str):
            raise PostizError(
                f"create_post: integration_id must be non-empty string; got {integration_id!r}"
            )
        if not content or not isinstance(content, str):
            raise PostizError(f"create_post: content must be non-empty; got {content!r}")
        if not isinstance(scheduled_at, dt.datetime):
            raise PostizError(
                f"create_post: scheduled_at must be datetime; got {type(scheduled_at).__name__}"
            )

        # Normalise to UTC ISO with millisecond precision (Postiz expects Z).
        if scheduled_at.tzinfo is None:
            # Treat naive datetimes as UTC to keep behaviour predictable.
            scheduled_utc = scheduled_at.replace(tzinfo=dt.timezone.utc)
        else:
            scheduled_utc = scheduled_at.astimezone(dt.timezone.utc)
        iso_z = scheduled_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Postiz `image` field expects list of objects in newer versions, just
        # URLs in older. Send the URL list as-is and let Postiz coerce; if a
        # caller hits a 400, they can switch to the {url, alt, ...} dict form
        # via the publish() raw helper.
        media = list(media_urls or [])

        payload: dict[str, Any] = {
            "type": post_type,
            "shortLink": bool(short_link),
            "date": iso_z,
            "tags": list(tags or []),
            "posts": [
                {
                    "integration": {"id": integration_id},
                    "value": [
                        {
                            "content": content,
                            "image": media,
                        }
                    ],
                    "settings": dict(extra_settings or {}),
                }
            ],
        }

        logger.info(
            "postiz.create_post: integration=%s len(content)=%d scheduled=%s media=%d",
            integration_id[:8] + "...",
            len(content),
            iso_z,
            len(media),
        )
        return self.publish(payload)


# Module-level singleton
postiz: PostizClient = PostizClient()


__all__ = ["postiz", "PostizClient", "PostizError", "PostizConfigError"]


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import datetime as _dt

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )

    if not (os.environ.get("POSTIZ_BASE_URL") and os.environ.get("POSTIZ_API_KEY")):
        logger.warning("Skip smoke test: POSTIZ_BASE_URL or POSTIZ_API_KEY not set")
    else:
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        try:
            posts = postiz.list_posts(date=yesterday)
            logger.info("Smoke test ok: %d posts on %s", len(posts), yesterday)
        except PostizError as exc:
            logger.exception("Smoke test failed: %s", exc)
            try:
                alert("P1", f"Postiz smoke test failed: {exc}")
            except Exception:  # noqa: BLE001
                logger.exception("alert() failed too")
