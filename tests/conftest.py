"""Shared pytest fixtures for the engine test suite.

Key fixtures
------------
* ``tmp_db`` — points ``SQLITE_PATH`` at a fresh tmp_path file, reloads
  ``lib.db`` so its module-level singleton picks up the new path, and yields
  the rebuilt singleton. Cleans up the per-thread connection on teardown.
* ``mock_llm`` — patches ``lib.llm_client.llm.complete`` to return a canned
  string (or a callable). Use the ``set_response`` helper to override at any
  point inside a test.
* ``sample_piece`` — creates one piece row in the ``tmp_db`` and yields its
  ``piece_id``.
* ``mock_requests`` — context-manager fixture that intercepts ``requests.get``
  / ``requests.post`` / ``requests.request`` so tests that touch HTTP never
  hit the network.

Design constraints (Prompt_AI系统化编程_v1.md §7):
  * No silent failures: fixtures raise loudly on setup errors.
  * No ``print``: logging only.
  * Reload-based isolation rather than mucking with the live ``Database``
    instance — guarantees a real WAL-mode SQLite file behind every test that
    requests ``tmp_db``, without interfering with the developer's local
    ``runtime/state.db``.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

logger = logging.getLogger(__name__)

# Ensure the engine repo root is on sys.path so `from lib.db import db` works
# regardless of how pytest was invoked.
_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))


# --------------------------------------------------------------------------- #
# tmp_db
# --------------------------------------------------------------------------- #


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Yield a fresh ``Database`` bound to a tmp_path SQLite file.

    Internally:
      1. monkeypatches ``SQLITE_PATH``
      2. reloads ``lib.db`` so its module-level singleton picks up the new
         path
      3. yields the new singleton
      4. closes the per-thread connection on teardown
    """
    sqlite_path = tmp_path / "state.db"
    monkeypatch.setenv("SQLITE_PATH", str(sqlite_path))
    # Disable Lark alerts during tests — they hit a webhook that won't exist.
    monkeypatch.setenv("ENABLE_LARK_ALERTS", "false")

    # Drop any cached import of lib.db AND lib.lark — lark imports db at
    # import time, so a stale lark binds to the OLD db singleton.
    for mod_name in ("lib.lark", "lib.db"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    db_module = importlib.import_module("lib.db")
    importlib.reload(db_module)

    yield db_module.db

    # Teardown: close thread-local connection so the tmp_path can be cleaned.
    try:
        db_module.db.close_thread_connection()
    except Exception:
        logger.exception("tmp_db: close_thread_connection failed during teardown")

    # Wipe lib.db + lib.lark from sys.modules so the NEXT test gets a fresh
    # singleton bound to its own tmp_path.
    for mod_name in ("lib.lark", "lib.db"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]


# --------------------------------------------------------------------------- #
# mock_llm
# --------------------------------------------------------------------------- #


class _MockLLMHandle:
    """Tiny controller returned by the ``mock_llm`` fixture.

    Use ``.set_response(value)`` to change what the next ``complete`` call
    returns. ``value`` can be a literal string or a callable
    ``(system, user, **kw) -> str``.
    """

    def __init__(self, complete_mock: MagicMock) -> None:
        self.complete = complete_mock
        self.responses: list[str | Callable[..., str]] = ["MOCK LLM RESPONSE"]
        self._call_idx = 0

        def _side_effect(*args: Any, **kwargs: Any) -> str:
            if not self.responses:
                return ""
            idx = min(self._call_idx, len(self.responses) - 1)
            self._call_idx += 1
            r = self.responses[idx]
            if callable(r):
                return r(*args, **kwargs)
            return r

        complete_mock.side_effect = _side_effect

    def set_response(self, value: str | Callable[..., str]) -> None:
        """Replace the response queue with a single entry."""
        self.responses = [value]
        self._call_idx = 0

    def set_responses(self, values: list[str | Callable[..., str]]) -> None:
        """Set a sequence of responses for sequential ``complete`` calls."""
        self.responses = list(values)
        self._call_idx = 0


@pytest.fixture()
def mock_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[_MockLLMHandle]:
    """Patch ``lib.llm_client.llm.complete`` with a MagicMock.

    Yields a handle exposing ``.complete`` (the mock) plus
    ``.set_response()`` / ``.set_responses()`` for tests.
    """
    # Ensure lib.llm_client is imported so we can patch on it.
    llm_mod = importlib.import_module("lib.llm_client")
    complete_mock = MagicMock(name="llm.complete")
    monkeypatch.setattr(llm_mod.llm, "complete", complete_mock, raising=True)
    handle = _MockLLMHandle(complete_mock)
    yield handle


# --------------------------------------------------------------------------- #
# sample_piece
# --------------------------------------------------------------------------- #


@pytest.fixture()
def sample_piece(tmp_db: Any) -> dict[str, Any]:
    """Insert one piece in 'candidate' state and return its identifier."""
    piece_id = "test-piece-001"
    selection_card_yaml = (
        "piece_id: test-piece-001\n"
        "hook_type: 47pct_bot\n"
        "narrative_anchor: 假用户代价\n"
        "target_persona: crypto_cmo\n"
    )
    tmp_db.pieces.create(piece_id, selection_card_yaml, actor="test")
    return {"piece_id": piece_id, "selection_card_yaml": selection_card_yaml}


# --------------------------------------------------------------------------- #
# mock_requests
# --------------------------------------------------------------------------- #


class _MockResponse:
    """Minimal stand-in for ``requests.Response`` good enough for our adapters."""

    def __init__(self, status_code: int = 200, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (str(json_data) if json_data is not None else "")
        self.headers: dict[str, str] = {"content-type": "application/json"}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no json body configured on mock response")
        return self._json_data

    def raise_for_status(self) -> None:
        if not self.ok:
            import requests

            raise requests.HTTPError(f"mock HTTP {self.status_code}")


class _MockRequests:
    """Context-manager-style mock for the top-level ``requests`` API.

    Usage::

        def test_x(mock_requests):
            mock_requests.set_response("GET", url_re=r".*/posts", json={"posts": []})
            ...
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[str, str, _MockResponse]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def set_response(
        self,
        method: str,
        url_substring: str = "",
        *,
        json: Any = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._handlers.append(
            (method.upper(), url_substring, _MockResponse(status_code=status_code, json_data=json, text=text))
        )

    def _dispatch(self, method: str, url: str, **kwargs: Any) -> _MockResponse:
        self.calls.append((method.upper(), url, dict(kwargs)))
        for m, sub, resp in self._handlers:
            if m != method.upper():
                continue
            if not sub or sub in url:
                return resp
        # Default: 200 empty JSON
        return _MockResponse(status_code=200, json_data={})


@pytest.fixture()
def mock_requests() -> Iterator[_MockRequests]:
    handle = _MockRequests()

    def _fake_get(url: str, **kwargs: Any) -> _MockResponse:
        return handle._dispatch("GET", url, **kwargs)

    def _fake_post(url: str, **kwargs: Any) -> _MockResponse:
        return handle._dispatch("POST", url, **kwargs)

    def _fake_request(method: str, url: str, **kwargs: Any) -> _MockResponse:
        return handle._dispatch(method, url, **kwargs)

    with (
        patch("requests.get", side_effect=_fake_get),
        patch("requests.post", side_effect=_fake_post),
        patch("requests.request", side_effect=_fake_request),
    ):
        yield handle


# --------------------------------------------------------------------------- #
# Misc safety: every test runs with LARK alerts disabled by default
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _disable_lark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-off Lark alerts everywhere so tests never DOS a real webhook."""
    monkeypatch.setenv("ENABLE_LARK_ALERTS", "false")
    # Some test routes still construct alert payloads; make absent URL safe.
    monkeypatch.setenv("LARK_WEBHOOK_URL", "")


# --------------------------------------------------------------------------- #
# Fake MEDIA_URL env so jobs.schedule_planner.sign_media_url doesn't crash
# tests that don't care about the URL contents. Tests that exercise missing
# env should monkeypatch.delenv() these explicitly.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _media_url_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default fake MEDIA_URL config — tests that need missing-env can delenv."""
    monkeypatch.setenv("MEDIA_URL_BASE", "https://ingest.test.local")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-secret-32-bytes-xxxxxxxxx")
