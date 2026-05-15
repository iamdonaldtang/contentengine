"""Google Analytics 4 Data API client.

GA4 is the only source that sees off-Postiz landing-page traffic. The
collector job queries one slice per day per landing-page set; this adapter
covers exactly that single ``run_report`` call.

See Metrics doc §2.3 for the canonical dimension / metric list.

Auth: service-account JSON at ``GA4_CREDENTIALS_PATH``
Property: ``GA4_PROPERTY_ID``
Quota: 25K queries/day (we use ~1).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from lib.lark import alert
from lib.retry import retryable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class GA4Error(RuntimeError):
    """Raised on any non-recoverable GA4 API error (after retries)."""


class GA4ConfigError(GA4Error):
    """Raised when GA4 env vars or credentials file are missing/invalid."""


# --------------------------------------------------------------------------- #
# Dimensions / metrics — kept as module constants to match the PRD exactly
# --------------------------------------------------------------------------- #

_DIMENSIONS: tuple[str, ...] = (
    "pagePath",
    "sessionSource",
    "sessionMedium",
    "sessionCampaignName",
    "sessionManualTerm",
    "sessionManualAdContent",
)

_METRICS: tuple[str, ...] = (
    "sessions",
    "totalUsers",
    "screenPageViews",
    "averageSessionDuration",
    "conversions",
)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class GA4Client:
    """GA4 Data API client (``google.analytics.data_v1beta``).

    The SDK and the service-account JSON are loaded lazily on first call to
    keep import-time side effects minimal — this lets the module be imported
    in environments that don't have GA configured (e.g. unit tests).
    """

    timeout: int = 60  # GA reports can be slow; SDK uses its own gRPC timeout

    def __init__(self) -> None:
        self._property_id: str = os.environ.get("GA4_PROPERTY_ID") or ""
        self._credentials_path: str = os.environ.get("GA4_CREDENTIALS_PATH") or ""
        self._client: Any = None  # lazy BetaAnalyticsDataClient
        if not self._property_id or not self._credentials_path:
            logger.warning(
                "GA4Client: GA4_PROPERTY_ID or GA4_CREDENTIALS_PATH not set; "
                "calls will fail until configured."
            )

    # -- private helpers --------------------------------------------------- #

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._property_id:
            raise GA4ConfigError("GA4_PROPERTY_ID is not set")
        if not self._credentials_path:
            raise GA4ConfigError("GA4_CREDENTIALS_PATH is not set")
        if not os.path.isfile(self._credentials_path):
            raise GA4ConfigError(
                f"GA4_CREDENTIALS_PATH does not point to a file: {self._credentials_path}"
            )
        try:
            from google.analytics.data_v1beta import BetaAnalyticsDataClient  # noqa: WPS433
        except ImportError as exc:
            raise GA4ConfigError(
                "google-analytics-data SDK is not installed; "
                "add `google-analytics-data` to requirements.txt"
            ) from exc
        try:
            self._client = BetaAnalyticsDataClient.from_service_account_file(
                self._credentials_path
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises a zoo of types
            raise GA4ConfigError(f"failed to load GA4 service account: {exc}") from exc
        return self._client

    # -- public surface ---------------------------------------------------- #

    @retryable()
    def run_landing_report(
        self,
        date: str,
        page_paths: list[str],
    ) -> list[dict[str, Any]]:
        """Run the daily landing-page report for one date.

        Args:
            date: ISO ``YYYY-MM-DD``. The same date is used for both
                ``start_date`` and ``end_date`` (single-day window).
            page_paths: List of ``pagePath`` values to InListFilter on,
                e.g. ``["/benchmark-report", "/free-diagnostic"]``. If
                empty, the filter is omitted and every page is returned —
                callers should pass an explicit list to avoid quota burn.

        Returns:
            List of row dicts. Each dict carries one key per dimension and
            one per metric (string values for dimensions, native-typed
            ints / floats for metrics). Empty list when GA returns no rows.

        Raises:
            GA4ConfigError: env / credentials misconfiguration.
            GA4Error: SDK error after retries.
        """
        # Imports kept local: only loaded after _ensure_client validates env.
        try:
            from google.analytics.data_v1beta.types import (  # noqa: WPS433
                DateRange,
                Dimension,
                Filter,
                FilterExpression,
                Metric,
                RunReportRequest,
            )
            from google.analytics.data_v1beta.types import (  # noqa: WPS433
                Filter as _Filter,
            )
            InListFilter = _Filter.InListFilter  # nested message class
        except ImportError as exc:
            raise GA4ConfigError(
                "google-analytics-data SDK missing required types"
            ) from exc

        client = self._ensure_client()

        request_kwargs: dict[str, Any] = {
            "property": f"properties/{self._property_id}",
            "dimensions": [Dimension(name=d) for d in _DIMENSIONS],
            "metrics": [Metric(name=m) for m in _METRICS],
            "date_ranges": [DateRange(start_date=date, end_date=date)],
        }
        if page_paths:
            request_kwargs["dimension_filter"] = FilterExpression(
                filter=Filter(
                    field_name="pagePath",
                    in_list_filter=InListFilter(values=list(page_paths)),
                )
            )

        request = RunReportRequest(**request_kwargs)

        try:
            response = client.run_report(request)
        except Exception as exc:  # noqa: BLE001 - SDK raises GoogleAPIError, etc.
            logger.error("GA4 run_report failed: %s", exc)
            raise GA4Error(f"GA4 run_report failed: {exc}") from exc

        rows: list[dict[str, Any]] = []
        for r in response.rows:
            row: dict[str, Any] = {}
            for i, dim_header in enumerate(_DIMENSIONS):
                row[dim_header] = r.dimension_values[i].value if i < len(r.dimension_values) else None
            for i, met_header in enumerate(_METRICS):
                raw_val = r.metric_values[i].value if i < len(r.metric_values) else None
                row[met_header] = _coerce_metric(met_header, raw_val)
            rows.append(row)

        logger.info(
            "ga4.run_landing_report(date=%s, page_paths=%d) -> %d rows",
            date,
            len(page_paths),
            len(rows),
        )
        return rows


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _coerce_metric(name: str, raw: str | None) -> int | float | None:
    """Coerce GA's stringified metric values to native ints / floats."""
    if raw is None or raw == "":
        return None
    # averageSessionDuration is seconds (float); conversions can be float in
    # GA4 (event count). All others are integers.
    try:
        if name in ("averageSessionDuration", "conversions"):
            return float(raw)
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("ga4: could not coerce metric %s=%r", name, raw)
        return None


# Module-level singleton
ga4: GA4Client = GA4Client()


__all__ = ["ga4", "GA4Client", "GA4Error", "GA4ConfigError"]


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import datetime as _dt

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    required = ("GA4_PROPERTY_ID", "GA4_CREDENTIALS_PATH")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.warning("Skip smoke test: env vars missing: %s", missing)
    else:
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        try:
            rows = ga4.run_landing_report(
                date=yesterday,
                page_paths=["/benchmark-report", "/free-diagnostic", "/growth-playbook"],
            )
            logger.info("Smoke test ok: %d rows on %s", len(rows), yesterday)
        except GA4Error as exc:
            logger.exception("Smoke test failed: %s", exc)
            try:
                alert("P1", f"ga4 smoke test failed: {exc}")
            except Exception:  # noqa: BLE001
                logger.exception("alert() failed too")
