"""Listmonk REST API client.

Listmonk is the self-hosted newsletter platform at ``newsletter.taskon.xyz``;
this adapter pulls campaign + subscriber + link-click data for the daily
metrics collector (Metrics doc §2.4).

Auth: HTTP Basic — ``LISTMONK_USERNAME`` + ``LISTMONK_PASSWORD``
Base URL: ``LISTMONK_BASE_URL``
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from lib.lark import alert
from lib.retry import retryable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class ListmonkError(RuntimeError):
    """Raised on any non-recoverable Listmonk API error (after retries)."""


class ListmonkConfigError(ListmonkError):
    """Raised when required env vars are missing at call time."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class ListmonkClient:
    """Listmonk REST API client.

    Env vars (read lazily):
        ``LISTMONK_BASE_URL``  — e.g. ``https://newsletter.taskon.xyz``
        ``LISTMONK_USERNAME``  — admin username (defaults to "admin" in example)
        ``LISTMONK_PASSWORD``  — admin password (no default)
    """

    timeout: int = 30

    def __init__(self) -> None:
        self.base_url: str = (os.environ.get("LISTMONK_BASE_URL") or "").rstrip("/")
        self._username: str = os.environ.get("LISTMONK_USERNAME") or ""
        self._password: str = os.environ.get("LISTMONK_PASSWORD") or ""
        if not self.base_url or not self._username or not self._password:
            logger.warning(
                "ListmonkClient: LISTMONK_BASE_URL / LISTMONK_USERNAME / "
                "LISTMONK_PASSWORD not all set; calls will fail until configured."
            )

    # -- private helpers --------------------------------------------------- #

    def _ensure_configured(self) -> None:
        if not (self.base_url and self._username and self._password):
            raise ListmonkConfigError(
                "LISTMONK_BASE_URL, LISTMONK_USERNAME, and LISTMONK_PASSWORD "
                "must all be set in the environment before calling Listmonk methods."
            )

    def _auth(self) -> HTTPBasicAuth:
        return HTTPBasicAuth(self._username, self._password)

    @retryable()
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(
                url,
                auth=self._auth(),
                params=params,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            logger.error("Listmonk GET %s failed: %s", path, exc)
            raise ListmonkError(f"GET {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            logger.error("Listmonk GET %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise ListmonkError(f"GET {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ListmonkError(f"GET {path} returned non-JSON body") from exc

    @retryable()
    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(
                url,
                auth=self._auth(),
                json=payload,
                timeout=self.timeout,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        except requests.RequestException as exc:
            logger.error("Listmonk POST %s failed: %s", path, exc)
            raise ListmonkError(f"POST {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            logger.error("Listmonk POST %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise ListmonkError(f"POST {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ListmonkError(f"POST {path} returned non-JSON body") from exc

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """Listmonk wraps every response in ``{"data": ...}``. Peel it off
        when present; otherwise pass through."""
        if isinstance(payload, dict) and "data" in payload and len(payload) <= 2:
            return payload["data"]
        return payload

    # -- public surface ---------------------------------------------------- #

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        """Return the full campaign object (stats + metadata).

        Args:
            campaign_id: Numeric Listmonk campaign id.

        Returns:
            Campaign dict per Metrics doc §2.4 — keys include ``id``, ``name``,
            ``subject``, ``send_at``, ``to_send``, ``sent``, ``views``,
            ``clicks``, ``bounces``, ``status``.

        Raises:
            ListmonkConfigError / ListmonkError.
        """
        raw = self._get(f"/api/campaigns/{campaign_id}")
        data = self._unwrap(raw)
        if not isinstance(data, dict):
            raise ListmonkError(
                f"campaigns/{campaign_id} returned non-object: {type(data).__name__}"
            )
        logger.info(
            "listmonk.get_campaign(%d) ok; sent=%s views=%s clicks=%s",
            campaign_id,
            data.get("sent"),
            data.get("views"),
            data.get("clicks"),
        )
        return data

    def get_campaign_link_stats(self, campaign_id: int) -> list[dict[str, Any]]:
        """Return per-link click stats for a campaign.

        Listmonk exposes this at ``/api/campaigns/{id}/analytics/links``.

        Args:
            campaign_id: Numeric Listmonk campaign id.

        Returns:
            List of link-stat dicts (``url``, ``count`` / ``clicks``, ...).
            Empty list when none.

        Raises:
            ListmonkConfigError / ListmonkError.
        """
        raw = self._get(f"/api/campaigns/{campaign_id}/analytics/links")
        data = self._unwrap(raw)
        if data is None:
            stats: list[dict[str, Any]] = []
        elif isinstance(data, list):
            stats = data
        else:
            raise ListmonkError(
                f"link stats for campaign {campaign_id} returned "
                f"non-list: {type(data).__name__}"
            )
        logger.info(
            "listmonk.get_campaign_link_stats(%d) -> %d links",
            campaign_id,
            len(stats),
        )
        return stats

    def list_subscribers(self, query: str | None = None) -> list[dict[str, Any]]:
        """Return subscribers, optionally filtered by Listmonk's query DSL.

        Listmonk paginates at 20 per page; this helper walks all pages.

        Args:
            query: Listmonk query string (e.g. ``"status='enabled'"``).
                ``None`` returns every subscriber.

        Returns:
            List of subscriber dicts.

        Raises:
            ListmonkConfigError / ListmonkError.
        """
        all_subs: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            params: dict[str, Any] = {"page": page, "per_page": per_page}
            if query is not None:
                params["query"] = query
            raw = self._get("/api/subscribers", params=params)
            data = self._unwrap(raw) or {}
            # Listmonk returns {"results": [...], "total": N, "per_page": ..., "page": ...}
            results = data.get("results") if isinstance(data, dict) else None
            if results is None:
                # Defensive: some Listmonk versions just return a list.
                if isinstance(data, list):
                    results = data
                else:
                    raise ListmonkError(
                        f"subscribers page {page} unexpected shape: {type(data).__name__}"
                    )
            if not isinstance(results, list):
                raise ListmonkError(f"subscribers 'results' not a list on page {page}")
            all_subs.extend(results)
            total = data.get("total") if isinstance(data, dict) else None
            if not results or (isinstance(total, int) and len(all_subs) >= total):
                break
            page += 1
            # Safety cap — Listmonk should never go beyond a few hundred pages
            # for our list size, but never enter an unbounded loop.
            if page > 1000:
                logger.warning("listmonk.list_subscribers: page cap of 1000 hit")
                break

        logger.info(
            "listmonk.list_subscribers(query=%r) -> %d subscribers",
            query,
            len(all_subs),
        )
        return all_subs

    def create_campaign(
        self,
        *,
        subject: str,
        body_html: str,
        list_id: int,
        name: str | None = None,
        send_at: dt.datetime | None = None,
        from_email: str | None = None,
        content_type: str = "html",
        template_id: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create (and optionally schedule) a Listmonk campaign.

        Listmonk creates campaigns in ``draft`` status by default. If
        ``send_at`` is provided, this helper additionally transitions the
        campaign to ``scheduled`` via the status-PUT endpoint so Listmonk
        will fire it at the requested time.

        Args:
            subject: Email subject line.
            body_html: Pre-rendered HTML body (use the
                ``templates/newsletter.html.j2`` Jinja template).
            list_id: Numeric Listmonk subscriber-list id (look up in the
                Listmonk admin UI; one TaskOn newsletter = one list).
            name: Optional campaign name (defaults to subject).
            send_at: Optional aware datetime — when set, the campaign is
                scheduled. Without it, the campaign stays in ``draft`` and
                a human in Listmonk admin can review then send manually.
            from_email: Optional explicit From: address. Listmonk uses the
                instance default when omitted.
            content_type: ``"html"`` (default) or ``"markdown"`` / ``"plain"``.
            template_id: Optional Listmonk template id for outer-wrapper
                styling. Omit to use the instance default template.
            tags: Optional list of Listmonk tag strings.

        Returns:
            The created campaign dict (id, status, subject, ...).

        Raises:
            ListmonkConfigError / ListmonkError as above.
        """
        if not subject or not isinstance(subject, str):
            raise ListmonkError(f"create_campaign: subject must be non-empty; got {subject!r}")
        if not body_html or not isinstance(body_html, str):
            raise ListmonkError("create_campaign: body_html must be non-empty")
        if not isinstance(list_id, int) or list_id <= 0:
            raise ListmonkError(f"create_campaign: list_id must be a positive int; got {list_id!r}")

        payload: dict[str, Any] = {
            "name": name or subject,
            "subject": subject,
            "lists": [list_id],
            "from_email": from_email or "",
            "content_type": content_type,
            "body": body_html,
            "tags": list(tags or []),
            "messenger": "email",
            "type": "regular",
        }
        if template_id is not None:
            payload["template_id"] = int(template_id)
        if send_at is not None:
            if send_at.tzinfo is None:
                send_at = send_at.replace(tzinfo=dt.timezone.utc)
            payload["send_at"] = send_at.astimezone(dt.timezone.utc).isoformat()

        raw = self._post("/api/campaigns", payload)
        data = self._unwrap(raw)
        if not isinstance(data, dict):
            raise ListmonkError(f"create_campaign: unexpected response: {type(data).__name__}")
        campaign_id = data.get("id")
        if not campaign_id:
            raise ListmonkError(f"create_campaign: response missing id: {raw!r:.200}")
        logger.info(
            "listmonk.create_campaign(id=%s, subject=%r, list_id=%d, send_at=%s) ok",
            campaign_id, subject[:80], list_id, payload.get("send_at") or "(none)",
        )

        # Transition to scheduled if a send_at was provided.
        if send_at is not None:
            try:
                self._post(
                    f"/api/campaigns/{campaign_id}/status",
                    {"status": "scheduled"},
                )
                logger.info("listmonk campaign %s transitioned to scheduled", campaign_id)
            except ListmonkError as exc:
                # Schedule failed but the draft is created — caller can
                # still recover via the Listmonk UI.
                logger.warning(
                    "listmonk campaign %s created but schedule-PUT failed: %s",
                    campaign_id, exc,
                )
        return data

    def list_campaigns(self, status: str = "finished") -> list[dict[str, Any]]:
        """Return campaigns filtered by status.

        Args:
            status: One of Listmonk's campaign statuses — ``draft``,
                ``scheduled``, ``running``, ``paused``, ``finished``,
                ``cancelled``. Defaults to ``finished`` (what the daily
                collector wants).

        Returns:
            List of campaign dicts. May be paginated; this helper walks
            all pages.

        Raises:
            ListmonkConfigError / ListmonkError.
        """
        all_campaigns: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            params: dict[str, Any] = {
                "status": status,
                "page": page,
                "per_page": per_page,
            }
            raw = self._get("/api/campaigns", params=params)
            data = self._unwrap(raw) or {}
            results = data.get("results") if isinstance(data, dict) else None
            if results is None and isinstance(data, list):
                results = data
            if not isinstance(results, list):
                raise ListmonkError(
                    f"campaigns page {page} unexpected shape: {type(data).__name__}"
                )
            all_campaigns.extend(results)
            total = data.get("total") if isinstance(data, dict) else None
            if not results or (isinstance(total, int) and len(all_campaigns) >= total):
                break
            page += 1
            if page > 1000:
                logger.warning("listmonk.list_campaigns: page cap of 1000 hit")
                break

        logger.info(
            "listmonk.list_campaigns(status=%s) -> %d campaigns",
            status,
            len(all_campaigns),
        )
        return all_campaigns


# Module-level singleton
listmonk: ListmonkClient = ListmonkClient()


__all__ = ["listmonk", "ListmonkClient", "ListmonkError", "ListmonkConfigError"]


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    required = ("LISTMONK_BASE_URL", "LISTMONK_USERNAME", "LISTMONK_PASSWORD")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.warning("Skip smoke test: env vars missing: %s", missing)
    else:
        try:
            finished = listmonk.list_campaigns(status="finished")
            logger.info("Smoke test ok: %d finished campaigns", len(finished))
        except ListmonkError as exc:
            logger.exception("Smoke test failed: %s", exc)
            try:
                alert("P1", f"listmonk smoke test failed: {exc}")
            except Exception:  # noqa: BLE001
                logger.exception("alert() failed too")
