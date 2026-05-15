"""TaskOn internal admin API client — 6 in-product touchpoints.

The "b-touch" surface = the 6 touchpoint slots inside the TaskOn product
(dashboard card, docs link, etc.) that the marketing engine pushes content
cards into, then reads back impression / click stats from.

See Metrics doc §2.5 and Engine Components PRD §2.9 for the contract. This
adapter only speaks HTTP — pushing the *right* card on the *right* day is
the orchestrator's job (``jobs/update_btouch.py``).

Auth: ``Authorization: Bearer {TASKON_ADMIN_TOKEN}``
Base URL: ``TASKON_ADMIN_API_BASE``
"""
from __future__ import annotations

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


class BTouchError(RuntimeError):
    """Raised on any non-recoverable TaskOn admin API error (after retries)."""


class BTouchConfigError(BTouchError):
    """Raised when required env vars are missing at call time."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class BTouchClient:
    """TaskOn internal admin API client.

    Env vars (read lazily):
        ``TASKON_ADMIN_API_BASE`` — e.g. ``https://api.taskon.xyz/admin``
        ``TASKON_ADMIN_TOKEN``    — internal admin bearer token
    """

    timeout: int = 30

    def __init__(self) -> None:
        self.base_url: str = (os.environ.get("TASKON_ADMIN_API_BASE") or "").rstrip("/")
        self._token: str = os.environ.get("TASKON_ADMIN_TOKEN") or ""
        if not self.base_url or not self._token:
            logger.warning(
                "BTouchClient: TASKON_ADMIN_API_BASE or TASKON_ADMIN_TOKEN not "
                "set; calls will fail until configured."
            )

    # -- private helpers --------------------------------------------------- #

    def _ensure_configured(self) -> None:
        if not self.base_url or not self._token:
            raise BTouchConfigError(
                "TASKON_ADMIN_API_BASE and TASKON_ADMIN_TOKEN must both be set "
                "in the environment before calling BTouchClient methods."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
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
            logger.error("btouch GET %s failed: %s", path, exc)
            raise BTouchError(f"GET {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            logger.error("btouch GET %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise BTouchError(f"GET {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BTouchError(f"GET {path} returned non-JSON body") from exc

    @retryable()
    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(
                url, headers=self._headers(), json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            logger.error("btouch POST %s failed: %s", path, exc)
            raise BTouchError(f"POST {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            logger.error("btouch POST %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise BTouchError(f"POST {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BTouchError(f"POST {path} returned non-JSON body") from exc

    # -- public surface ---------------------------------------------------- #

    def push_content_card(
        self,
        touchpoint_id: int,
        title: str,
        link: str,
        preview: str,
    ) -> dict[str, Any]:
        """Push a content card into one of the 6 product touchpoints.

        Endpoint: ``POST /admin/content_card``.

        Args:
            touchpoint_id: 1..6 — which touchpoint slot to update.
            title: Card title shown to the end user.
            link: Click-through URL (must already include UTM params; UTM
                generation lives in ``lib.utm``, not here).
            preview: Short preview / subtitle (1-2 sentences max).

        Returns:
            Response dict from TaskOn admin API. Shape is owned by the
            internal team; we pass through untouched.

        Raises:
            BTouchConfigError / BTouchError.
        """
        if not 1 <= touchpoint_id <= 6:
            raise BTouchError(
                f"touchpoint_id must be in 1..6, got {touchpoint_id}"
            )
        payload: dict[str, Any] = {
            "touchpoint_id": touchpoint_id,
            "title": title,
            "link": link,
            "preview": preview,
        }
        data = self._post("/content_card", payload)
        if not isinstance(data, dict):
            raise BTouchError(
                f"push_content_card returned non-object: {type(data).__name__}"
            )
        logger.info(
            "btouch.push_content_card(touchpoint_id=%d, title=%r) ok",
            touchpoint_id,
            title[:60],
        )
        return data

    def get_touchpoint_stats(
        self,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        """Return per-touchpoint impression / click / CTR rows over a date range.

        Endpoint: ``GET /admin/touchpoint_stats?date_from=&date_to=``.

        Args:
            date_from: ISO ``YYYY-MM-DD``, inclusive.
            date_to: ISO ``YYYY-MM-DD``, inclusive.

        Returns:
            List of dicts per Metrics doc §2.5 — each with ``touchpoint_id``,
            ``touchpoint_name``, ``impressions``, ``clicks``, ``ctr``,
            ``utm_medium``. Empty list when no stats.

        Raises:
            BTouchConfigError / BTouchError.
        """
        raw = self._get(
            "/touchpoint_stats",
            params={"date_from": date_from, "date_to": date_to},
        )
        # Response may be either {"touchpoints": [...]} or a bare list.
        if isinstance(raw, dict):
            stats = raw.get("touchpoints")
            if stats is None:
                # Some internal endpoints wrap as {"data": [...]}.
                stats = raw.get("data")
            if stats is None and not raw:
                stats = []
        else:
            stats = raw
        if not isinstance(stats, list):
            raise BTouchError(
                f"touchpoint_stats unexpected shape: {type(stats).__name__}"
            )
        logger.info(
            "btouch.get_touchpoint_stats(%s..%s) -> %d rows",
            date_from,
            date_to,
            len(stats),
        )
        return stats


# Module-level singleton
btouch: BTouchClient = BTouchClient()


__all__ = ["btouch", "BTouchClient", "BTouchError", "BTouchConfigError"]


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import datetime as _dt

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    required = ("TASKON_ADMIN_API_BASE", "TASKON_ADMIN_TOKEN")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.warning("Skip smoke test: env vars missing: %s", missing)
    else:
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        try:
            stats = btouch.get_touchpoint_stats(date_from=yesterday, date_to=yesterday)
            logger.info("Smoke test ok: %d touchpoint rows on %s", len(stats), yesterday)
        except BTouchError as exc:
            logger.exception("Smoke test failed: %s", exc)
            try:
                alert("P1", f"btouch smoke test failed: {exc}")
            except Exception:  # noqa: BLE001
                logger.exception("alert() failed too")
