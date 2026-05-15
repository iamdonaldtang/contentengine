"""Tests for ``lib.llm_client`` — MiniMaxi pool rotation, Anthropic fallback, complete_json retry, fallback disable."""
from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_llm_module(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    """Reload ``lib.llm_client`` with a curated env so the singleton inside
    picks up the fresh configuration on its next ``_ensure_initialized`` call."""
    # Force re-initialisation each time by deleting the cached module entry.
    import sys

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Default — three keys so the rotation logic kicks in.
    monkeypatch.setenv("MINIMAXI_API_KEY_1", env.get("MINIMAXI_API_KEY_1", "key-1"))
    monkeypatch.setenv("MINIMAXI_API_KEY_2", env.get("MINIMAXI_API_KEY_2", "key-2"))
    monkeypatch.setenv("MINIMAXI_API_KEY_3", env.get("MINIMAXI_API_KEY_3", "key-3"))
    monkeypatch.setenv("LLM_MAX_RETRIES_PER_KEY", env.get("LLM_MAX_RETRIES_PER_KEY", "2"))
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", env.get("LLM_REQUEST_TIMEOUT_SECONDS", "5"))
    monkeypatch.setenv(
        "ENABLE_ANTHROPIC_FALLBACK", env.get("ENABLE_ANTHROPIC_FALLBACK", "true")
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", env.get("ANTHROPIC_API_KEY", "anthropic-test-key"))

    if "lib.llm_client" in sys.modules:
        del sys.modules["lib.llm_client"]
    return importlib.import_module("lib.llm_client")


def _minimaxi_ok_response(text: str = "ok") -> Any:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": 10},
    }
    resp.text = json.dumps(resp.json.return_value)
    return resp


def _minimaxi_429_response() -> Any:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 429
    resp.ok = False
    resp.text = '{"error": "rate limited"}'
    return resp


# --------------------------------------------------------------------------- #
# Test 1 — All 3 MiniMaxi keys fail → Anthropic fallback triggers
# --------------------------------------------------------------------------- #


def test_all_minimaxi_keys_fail_falls_back_to_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fresh_llm_module(monkeypatch)

    # All requests.post calls return 429 → retryable, rotates to next key.
    with patch("lib.llm_client.requests.post", return_value=_minimaxi_429_response()):
        # Mock Anthropic SDK at import-time inside _call_anthropic.
        fake_anthropic = MagicMock()
        fake_client = MagicMock()
        fake_block = MagicMock()
        fake_block.text = "ANTHROPIC FALLBACK OK"
        fake_msg = MagicMock()
        fake_msg.content = [fake_block]
        fake_client.messages.create.return_value = fake_msg
        fake_anthropic.Anthropic.return_value = fake_client

        import sys

        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        result = mod.llm.complete(system="s", user="u", max_tokens=100)

    assert result == "ANTHROPIC FALLBACK OK"
    fake_anthropic.Anthropic.assert_called_once()
    fake_client.messages.create.assert_called_once()


# --------------------------------------------------------------------------- #
# Test 2 — Per-key 429 retry then rotate
# --------------------------------------------------------------------------- #


def test_per_key_429_retries_then_rotates(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fresh_llm_module(monkeypatch, LLM_MAX_RETRIES_PER_KEY="2")

    call_log: list[str] = []

    def fake_post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        # Track which key was used (last 5 chars of Authorization).
        auth = headers.get("Authorization", "")
        call_log.append(auth[-5:])
        # Key-1 (suffix 'key-1') always 429 → exhaust retries, rotate.
        # Key-2 (suffix 'key-2') succeeds on first try.
        if auth.endswith("key-1"):
            return _minimaxi_429_response()
        return _minimaxi_ok_response("from-key-2")

    with patch("lib.llm_client.requests.post", side_effect=fake_post):
        result = mod.llm.complete(system="s", user="u", max_tokens=100)

    assert result == "from-key-2"
    # Key-1 must be tried `LLM_MAX_RETRIES_PER_KEY` times before rotation.
    key1_calls = sum(1 for k in call_log if k == "key-1")
    key2_calls = sum(1 for k in call_log if k == "key-2")
    assert key1_calls == 2, f"expected 2 retries on key-1, got {key1_calls}: {call_log}"
    assert key2_calls == 1, f"expected 1 success on key-2, got {key2_calls}: {call_log}"


# --------------------------------------------------------------------------- #
# Test 3 — complete_json retries on invalid JSON up to 2 times
# --------------------------------------------------------------------------- #


def test_complete_json_retries_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fresh_llm_module(monkeypatch)

    # Sequence: two invalid bodies, then valid → succeed on 3rd attempt.
    responses_seq = [
        _minimaxi_ok_response("this is not json at all"),
        _minimaxi_ok_response("```json\nstill {bad json"),
        _minimaxi_ok_response('{"answer": 42, "ok": true}'),
    ]
    iter_seq = iter(responses_seq)

    def fake_post(*args: Any, **kwargs: Any) -> Any:
        return next(iter_seq)

    with patch("lib.llm_client.requests.post", side_effect=fake_post):
        out = mod.llm.complete_json(system="s", user="u", schema_hint="{answer:int, ok:bool}")

    assert out == {"answer": 42, "ok": True}


def test_complete_json_raises_after_three_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fresh_llm_module(monkeypatch, ENABLE_ANTHROPIC_FALLBACK="false")

    # Always return broken JSON. Should fail after 3 attempts.
    with patch(
        "lib.llm_client.requests.post",
        return_value=_minimaxi_ok_response("not json no way"),
    ):
        with pytest.raises(mod.LLMClientError):
            mod.llm.complete_json(system="s", user="u")


# --------------------------------------------------------------------------- #
# Test 4 — ENABLE_ANTHROPIC_FALLBACK=false → no fallback, raise LLMClientError
# --------------------------------------------------------------------------- #


def test_fallback_disabled_raises_after_minimaxi_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fresh_llm_module(monkeypatch, ENABLE_ANTHROPIC_FALLBACK="false")

    # Force a fresh import of anthropic to detect calls — but we should NOT see any.
    fake_anthropic = MagicMock()
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    with patch("lib.llm_client.requests.post", return_value=_minimaxi_429_response()):
        with pytest.raises(mod.LLMClientError):
            mod.llm.complete(system="s", user="u")

    # The Anthropic SDK must never be touched when fallback is off.
    fake_anthropic.Anthropic.assert_not_called()


# --------------------------------------------------------------------------- #
# Test 5 — enable_thinking: key absent from body when env unset
# --------------------------------------------------------------------------- #


def _capture_post_body(monkeypatch: pytest.MonkeyPatch, mod: Any) -> dict[str, Any]:
    """Run a single complete() and return the JSON body posted to MiniMaxi."""
    captured: dict[str, Any] = {}

    def fake_post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        captured.update(json)
        return _minimaxi_ok_response("ok")

    with patch("lib.llm_client.requests.post", side_effect=fake_post):
        mod.llm.complete(system="s", user="u", max_tokens=10)

    return captured


def test_enable_thinking_absent_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIMAXI_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("MINIMAX_ENABLE_THINKING", raising=False)
    mod = _fresh_llm_module(monkeypatch)

    body = _capture_post_body(monkeypatch, mod)
    assert "enable_thinking" not in body, (
        "When MINIMAXI_ENABLE_THINKING is unset, the request body must NOT "
        "contain the key (compat with deployments that reject unknown fields)."
    )


def test_enable_thinking_false_when_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fresh_llm_module(monkeypatch, MINIMAXI_ENABLE_THINKING="false")

    body = _capture_post_body(monkeypatch, mod)
    assert body.get("enable_thinking") is False


def test_enable_thinking_true_when_env_true(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fresh_llm_module(monkeypatch, MINIMAXI_ENABLE_THINKING="true")

    body = _capture_post_body(monkeypatch, mod)
    assert body.get("enable_thinking") is True
