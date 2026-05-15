"""Tests for ``sources/*`` adapters.

Smoke-level coverage:
  * Each source class is instantiable with an empty environment — config
    errors deferred to first method call ("lazy fail").
  * postiz.list_posts returns the expected shape.
  * ga4 client builds correct RunReportRequest (verifies dimensions / metrics).
  * listmonk basic auth header is constructed correctly.
"""
from __future__ import annotations

import base64
import importlib
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# --------------------------------------------------------------------------- #
# Smoke: every source class is instantiable with empty env
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "module_name,class_attr",
    [
        ("sources.postiz", "PostizClient"),
        ("sources.twitter_x", "TwitterXClient"),
        ("sources.ga4", "GA4Client"),
        ("sources.listmonk", "ListmonkClient"),
    ],
)
def test_source_class_lazy_instantiation(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    class_attr: str,
) -> None:
    """Importing + instantiating each source must NOT raise even if env vars are absent.

    Failure should happen at the first method call (config-error), not at
    import / __init__. This guarantees we can ``from sources.x import y`` in
    test environments without leaking credentials.
    """
    # Wipe credential vars to simulate an empty env.
    for k in (
        "POSTIZ_BASE_URL",
        "POSTIZ_API_KEY",
        "X_BEARER_TOKEN",
        "GA4_PROPERTY_ID",
        "GA4_CREDENTIALS_PATH",
        "LISTMONK_BASE_URL",
        "LISTMONK_USERNAME",
        "LISTMONK_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    if module_name in sys.modules:
        del sys.modules[module_name]
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_attr)

    # __init__ should not raise.
    instance = cls()
    assert instance is not None


# --------------------------------------------------------------------------- #
# postiz.list_posts shape
# --------------------------------------------------------------------------- #


def test_postiz_list_posts_returns_expected_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTIZ_BASE_URL", "https://postiz.test")
    monkeypatch.setenv("POSTIZ_API_KEY", "test-key")

    if "sources.postiz" in sys.modules:
        del sys.modules["sources.postiz"]
    postiz_mod = importlib.import_module("sources.postiz")

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.ok = True
    fake_response.json.return_value = {
        "posts": [
            {
                "id": "post_abc",
                "platform": "twitter",
                "external_post_id": "1789xxx",
                "published_at": "2026-05-09T09:00:00Z",
                "content": "Sample",
                "media_urls": [],
                "utm_campaign": "perps_dex_lies_v1",
            }
        ]
    }
    fake_response.text = "{}"

    with patch("sources.postiz.requests.get", return_value=fake_response) as mock_get:
        client = postiz_mod.PostizClient()
        posts = client.list_posts(date="2026-05-09")

    assert posts == fake_response.json.return_value["posts"]
    # Auth header was sent
    call = mock_get.call_args
    headers = call.kwargs.get("headers") or (call.args[1] if len(call.args) > 1 else {})
    # Postiz self-hosted Public API takes the API key as a raw header value
    # (NO "Bearer " prefix). Verified live against postiz-app:latest 2026-05-13.
    assert headers.get("Authorization") == "test-key"
    # Timeout set
    assert call.kwargs.get("timeout") == postiz_mod.PostizClient.timeout


def test_postiz_list_posts_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTIZ_BASE_URL", "https://postiz.test")
    monkeypatch.setenv("POSTIZ_API_KEY", "test-key")
    if "sources.postiz" in sys.modules:
        del sys.modules["sources.postiz"]
    postiz_mod = importlib.import_module("sources.postiz")

    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.ok = False
    fake_response.text = "boom"

    with patch("sources.postiz.requests.get", return_value=fake_response):
        client = postiz_mod.PostizClient()
        with pytest.raises(postiz_mod.PostizError):
            client.list_posts(date="2026-05-09")


# --------------------------------------------------------------------------- #
# ga4 builds correct RunReportRequest
# --------------------------------------------------------------------------- #


def test_ga4_run_landing_report_builds_correct_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Verify GA4 client passes the expected dimensions / metrics into RunReportRequest."""
    # Provide a placeholder credentials file so _ensure_client gets past file-existence.
    creds = tmp_path / "ga4_creds.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GA4_PROPERTY_ID", "999999")
    monkeypatch.setenv("GA4_CREDENTIALS_PATH", str(creds))

    if "sources.ga4" in sys.modules:
        del sys.modules["sources.ga4"]
    ga4_mod = importlib.import_module("sources.ga4")

    # Build a fake google.analytics.data_v1beta module tree.
    fake_pkg = MagicMock()
    fake_types = MagicMock()

    # Capture what RunReportRequest was constructed with.
    captured: dict[str, Any] = {}

    def _fake_RunReportRequest(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(name="RunReportRequest")

    fake_types.RunReportRequest = _fake_RunReportRequest

    class _Dim:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Met:
        def __init__(self, name: str) -> None:
            self.name = name

    class _DateRange:
        def __init__(self, start_date: str, end_date: str) -> None:
            self.start_date = start_date
            self.end_date = end_date

    class _InList:
        def __init__(self, values: list[str]) -> None:
            self.values = values

    class _Filter:
        InListFilter = _InList

        def __init__(self, field_name: str, in_list_filter: _InList) -> None:
            self.field_name = field_name
            self.in_list_filter = in_list_filter

    class _FilterExpression:
        def __init__(self, filter: _Filter) -> None:
            self.filter = filter

    fake_types.Dimension = _Dim
    fake_types.Metric = _Met
    fake_types.DateRange = _DateRange
    fake_types.Filter = _Filter
    fake_types.FilterExpression = _FilterExpression

    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.rows = []
    fake_client.run_report.return_value = fake_response

    class _BetaAnalyticsDataClient:
        @classmethod
        def from_service_account_file(cls, path: str) -> Any:
            return fake_client

    fake_pkg.BetaAnalyticsDataClient = _BetaAnalyticsDataClient

    monkeypatch.setitem(sys.modules, "google.analytics.data_v1beta", fake_pkg)
    monkeypatch.setitem(sys.modules, "google.analytics.data_v1beta.types", fake_types)

    client = ga4_mod.GA4Client()
    rows = client.run_landing_report(
        date="2026-05-09",
        page_paths=["/benchmark-report", "/free-diagnostic"],
    )

    assert rows == []
    # property string
    assert captured["property"] == "properties/999999"
    # dimensions / metrics match the PRD §2.3 list
    dim_names = [d.name for d in captured["dimensions"]]
    met_names = [m.name for m in captured["metrics"]]
    assert dim_names == list(ga4_mod._DIMENSIONS)
    assert met_names == list(ga4_mod._METRICS)
    # date range single-day
    dr = captured["date_ranges"][0]
    assert dr.start_date == "2026-05-09"
    assert dr.end_date == "2026-05-09"
    # filter applied
    assert "dimension_filter" in captured
    flt = captured["dimension_filter"]
    assert flt.filter.field_name == "pagePath"
    assert flt.filter.in_list_filter.values == ["/benchmark-report", "/free-diagnostic"]


# --------------------------------------------------------------------------- #
# listmonk basic auth header
# --------------------------------------------------------------------------- #


def test_listmonk_basic_auth_constructed_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the HTTP Basic Auth header lands as ``Basic base64(user:pass)``."""
    monkeypatch.setenv("LISTMONK_BASE_URL", "https://newsletter.test")
    monkeypatch.setenv("LISTMONK_USERNAME", "admin")
    monkeypatch.setenv("LISTMONK_PASSWORD", "secret")

    if "sources.listmonk" in sys.modules:
        del sys.modules["sources.listmonk"]
    lm_mod = importlib.import_module("sources.listmonk")

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.ok = True
    fake_response.json.return_value = {"data": {"id": 5, "name": "x"}}
    fake_response.text = "{}"

    captured_headers: dict[str, str] = {}
    captured_auth = None

    def _fake_get(url: str, **kwargs: Any) -> Any:
        nonlocal captured_auth
        captured_auth = kwargs.get("auth")
        captured_headers.update(kwargs.get("headers") or {})
        # Drain the underlying session preparation that ``requests.get`` does
        # when given an HTTPBasicAuth — we replicate the header here so the
        # test can assert on it.
        from requests.auth import HTTPBasicAuth

        if isinstance(captured_auth, HTTPBasicAuth):
            user = captured_auth.username
            pw = captured_auth.password
            token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
            captured_headers["Authorization"] = f"Basic {token}"
        return fake_response

    with patch("sources.listmonk.requests.get", side_effect=_fake_get):
        client = lm_mod.ListmonkClient()
        result = client.get_campaign(5)

    assert isinstance(result, dict)
    # Basic auth applied
    assert captured_auth is not None
    from requests.auth import HTTPBasicAuth

    assert isinstance(captured_auth, HTTPBasicAuth)
    assert captured_auth.username == "admin"
    assert captured_auth.password == "secret"
    # The Authorization header (synthesised) decodes correctly.
    expected_token = base64.b64encode(b"admin:secret").decode("ascii")
    assert captured_headers.get("Authorization") == f"Basic {expected_token}"
