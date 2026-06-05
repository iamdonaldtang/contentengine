"""Unified LLM client · MiniMaxi (primary) + Anthropic Opus 4.7 (fallback).

Public surface (module-level singleton ``llm``)::

    from lib.llm_client import llm

    text = llm.complete(system="...", user="...")
    data = llm.complete_json(system="...", user="...", schema_hint="...")

Design contract:
  * Primary: MiniMaxi portal — OpenAI-compatible ``/chat/completions``. Uses
    three rotating API keys (``MINIMAXI_API_KEY_1/2/3``); each key gets up to
    ``LLM_MAX_RETRIES_PER_KEY`` attempts on 429 / 5xx before rotation.
  * Fallback: Anthropic SDK with ``ANTHROPIC_FALLBACK_MODEL`` (default
    ``claude-opus-4-7``) — engaged only if every MiniMaxi key is exhausted AND
    ``ENABLE_ANTHROPIC_FALLBACK=true``.
  * Final failure raises ``LLMClientError`` and emits a P1 Lark alert.
  * ``complete_json`` adds a JSON-only instruction, parses the response, and
    retries up to twice (asking the model to fix the JSON) before raising.
  * Every HTTP call has an explicit ``timeout=`` (env
    ``LLM_REQUEST_TIMEOUT_SECONDS``, default 60).
  * ``print()`` is banned (§7) — everything logged via :mod:`logging`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class LLMClientError(Exception):
    """Raised when both the MiniMaxi pool and Anthropic fallback fail.

    Carries an optional ``cause`` reference to the last underlying exception
    for forensics.
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


# HTTP status codes that justify rotating to the next key / retrying.
_RETRYABLE_STATUSES: set[int] = {408, 425, 429, 500, 502, 503, 504, 529}


class LLMClient:
    """Lazy-initialised LLM client. Module-level singleton ``llm = LLMClient()``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._minimaxi_keys: list[str] = []
        self._minimaxi_base_url: str = ""
        self._default_model: str = "MiniMax-M1"
        self._anthropic_model: str = "claude-opus-4-7"
        self._anthropic_enabled: bool = True
        self._anthropic_api_key: str = ""
        self._max_retries_per_key: int = 2
        self._timeout: float = 60.0
        # MiniMax M2 emits ``<think>...</think>`` reasoning blocks by default.
        # When the env var is set, we pass ``enable_thinking`` in the request
        # body to suppress it at the API layer (saves 50-80 % output tokens
        # for production runs). ``None`` = env unset → behave like before.
        self._enable_thinking: bool | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            load_dotenv(override=False)
            # Accept any of these env var names, in priority order. Both
            # spellings of the brand are tolerated (`MINIMAXI` is canonical
            # since the host is api.minimaxi.com, but `MINIMAX` is the more
            # common shorthand). Both singular and `_1/_2/_3` numbered forms
            # are accepted; we dedupe so the same secret isn't tried twice.
            candidate_env_vars = [
                "MINIMAXI_API_KEY_1",
                "MINIMAXI_API_KEY_2",
                "MINIMAXI_API_KEY_3",
                "MINIMAXI_API_KEY",
                "MINIMAX_API_KEY_1",
                "MINIMAX_API_KEY_2",
                "MINIMAX_API_KEY_3",
                "MINIMAX_API_KEY",
            ]
            seen: set[str] = set()
            self._minimaxi_keys = []
            for name in candidate_env_vars:
                v = (os.environ.get(name) or "").strip()
                if v and v not in seen:
                    seen.add(v)
                    self._minimaxi_keys.append(v)
            self._minimaxi_base_url = (
                os.environ.get("MINIMAXI_BASE_URL")
                or os.environ.get("MINIMAX_BASE_URL")
                or "https://api.minimaxi.com/v1"
            ).rstrip("/")
            # Tolerate four spellings of the "which model" env var name:
            #   MINIMAXI_DEFAULT_MODEL | MINIMAX_DEFAULT_MODEL
            #   MINIMAXI_MODEL         | MINIMAX_MODEL
            self._default_model = (
                os.environ.get("MINIMAXI_DEFAULT_MODEL")
                or os.environ.get("MINIMAX_DEFAULT_MODEL")
                or os.environ.get("MINIMAXI_MODEL")
                or os.environ.get("MINIMAX_MODEL")
                or "MiniMax-M2.7-highspeed"
            )
            self._anthropic_model = (
                os.environ.get("ANTHROPIC_FALLBACK_MODEL") or "claude-opus-4-7"
            )
            self._anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            self._anthropic_enabled = (
                os.environ.get("ENABLE_ANTHROPIC_FALLBACK", "true").strip().lower() == "true"
                and bool(self._anthropic_api_key)
            )
            # 2026-05-18: default 2 → 5. Combined with exponential backoff
            # in complete() the worst-case wait is 1+2+4+8 = 15s before giving
            # up on a single key — enough to ride out most MiniMaxi 429/5xx
            # windows without Anthropic fallback (which Donald has opted out
            # of on cost grounds; see memory/user_llm_cost_constraint.md).
            try:
                self._max_retries_per_key = max(
                    1, int(os.environ.get("LLM_MAX_RETRIES_PER_KEY", "5"))
                )
            except ValueError:
                self._max_retries_per_key = 5
            try:
                self._timeout = float(os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "60"))
            except ValueError:
                self._timeout = 60.0
            # `enable_thinking` — opt-in toggle. Only forwarded to MiniMaxi
            # when the env var is explicitly set; otherwise we leave the key
            # off the request body to stay compatible with unknown
            # deployments that don't speak this parameter.
            raw_thinking = (
                os.environ.get("MINIMAXI_ENABLE_THINKING")
                or os.environ.get("MINIMAX_ENABLE_THINKING")
            )
            if raw_thinking is not None and raw_thinking.strip() != "":
                self._enable_thinking = raw_thinking.strip().lower() in (
                    "true",
                    "1",
                    "yes",
                )
            else:
                self._enable_thinking = None
            self._initialized = True
            logger.info(
                "LLMClient initialized: minimaxi_keys=%d anthropic_fallback=%s "
                "default_model=%s enable_thinking=%s",
                len(self._minimaxi_keys),
                self._anthropic_enabled,
                self._default_model,
                self._enable_thinking,
            )

    # -- public API --------------------------------------------------------- #

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        temperature: float = 0.7,
        model: str | None = None,
    ) -> str:
        """Run a chat completion and return the assistant message text.

        Tries every MiniMaxi key with per-key retries; falls back to Anthropic
        if all keys exhaust and fallback is enabled. Raises ``LLMClientError``
        on total failure.
        """
        self._ensure_initialized()
        model_name = model or self._default_model
        last_exc: BaseException | None = None

        # --- primary: MiniMaxi pool --- #
        # Retryable errors get exponential backoff between attempts on the
        # SAME key (most MiniMaxi 429/5xx windows are seconds-long; immediate
        # retry just burns the next quota slot). Non-retryable errors rotate
        # off the key with no wait.
        for idx, key in enumerate(self._minimaxi_keys, start=1):
            for attempt in range(1, self._max_retries_per_key + 1):
                try:
                    return self._call_minimaxi(
                        api_key=key,
                        model=model_name,
                        system=system,
                        user=user,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                except _RetryableLLMError as exc:
                    last_exc = exc
                    logger.warning(
                        "MiniMaxi key#%d attempt=%d retryable error: %s",
                        idx,
                        attempt,
                        exc,
                    )
                    if attempt < self._max_retries_per_key:
                        backoff = 2 ** (attempt - 1)  # 1, 2, 4, 8, 16 s
                        logger.info(
                            "MiniMaxi key#%d backoff %ds before retry %d",
                            idx, backoff, attempt + 1,
                        )
                        time.sleep(backoff)
                    continue
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    logger.warning(
                        "MiniMaxi key#%d attempt=%d non-retryable error: %s",
                        idx,
                        attempt,
                        exc,
                    )
                    break  # rotate to next key on non-retryable errors too
            logger.info("rotating off MiniMaxi key#%d", idx)

        # --- fallback: Anthropic --- #
        if self._anthropic_enabled:
            logger.info(
                "MiniMaxi pool exhausted; falling back to Anthropic model=%s",
                self._anthropic_model,
            )
            try:
                return self._call_anthropic(
                    system=system,
                    user=user,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.error("Anthropic fallback failed: %s", exc)
        else:
            logger.warning("Anthropic fallback disabled or missing API key; cannot recover")

        # --- total failure --- #
        # P2 (was P1) — single-key + no Anthropic fallback means transient
        # MiniMaxi bursts land here; next cron typically recovers. See
        # memory/user_llm_cost_constraint.md for cost rationale.
        self._fire_alert("P2", "complete() exhausted all providers", last_exc)
        raise LLMClientError(
            "LLM completion failed across all MiniMaxi keys and Anthropic fallback",
            cause=last_exc,
        )

    def complete_json(
        self,
        system: str,
        user: str,
        schema_hint: str | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        """Run a completion and parse the response as a JSON object.

        Appends a strict "JSON only" instruction to the system prompt and
        retries up to twice (asking the model to fix invalid JSON) before
        raising ``LLMClientError``.
        """
        self._ensure_initialized()
        # JSON generation needs a bigger token budget than free text. MiniMax
        # M2.7 may emit a (later-stripped) <think> block before the answer, so
        # the visible JSON can be short yet still get truncated mid-object once
        # thinking + answer exceed max_tokens — producing errors like
        # "Expecting ',' delimiter". Worse, the fix-retry below re-runs with the
        # SAME ceiling, so a truncated object can never be repaired. Give
        # complete_json its own higher, env-tunable default; an explicit
        # caller-supplied max_tokens still wins. (Set MINIMAXI_ENABLE_THINKING=
        # false on the engine to also suppress the thinking block at the source.)
        if "max_tokens" not in kw:
            try:
                kw["max_tokens"] = max(
                    4000, int(os.environ.get("LLM_JSON_MAX_TOKENS", "8000"))
                )
            except ValueError:
                kw["max_tokens"] = 8000
        json_instruction = (
            "Respond with ONLY a valid JSON object — no markdown fences, no "
            "preamble, no trailing prose."
        )
        if schema_hint:
            json_instruction += f" Conform to this schema/shape: {schema_hint}"
        sys_prompt = f"{system}\n\n{json_instruction}".strip()

        last_raw = ""
        last_err: Exception | None = None
        # 1 initial + up to 2 fix retries = 3 total attempts.
        for attempt in range(1, 4):
            try:
                raw = self.complete(system=sys_prompt, user=user, **kw)
            except LLMClientError:
                raise  # provider-level failure already alerted; bubble up.
            last_raw = raw
            try:
                cleaned = _strip_code_fences(raw)
                data = json.loads(cleaned)
                if not isinstance(data, dict):
                    raise ValueError(f"expected JSON object, got {type(data).__name__}")
                return data
            except (json.JSONDecodeError, ValueError) as exc:
                last_err = exc
                logger.warning(
                    "complete_json attempt=%d failed to parse: %s; raw=%s",
                    attempt,
                    exc,
                    raw[:300],
                )
                if attempt >= 3:
                    break
                # Ask the model to fix it.
                user = (
                    "Your previous response was not valid JSON. "
                    f"Parser error: {exc}. "
                    "Return a corrected JSON object only. "
                    f"Previous response was:\n{raw}"
                )

        self._fire_alert("P2", "complete_json() could not parse a valid JSON object", last_err)
        raise LLMClientError(
            f"complete_json failed to produce valid JSON after 3 attempts; "
            f"last_error={last_err}; last_raw={last_raw[:300]}",
            cause=last_err,
        )

    # -- internals ---------------------------------------------------------- #

    def _call_minimaxi(
        self,
        *,
        api_key: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        url = f"{self._minimaxi_base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        # Only forward `enable_thinking` if explicitly configured — older /
        # unknown MiniMaxi deployments may reject unknown body keys.
        if self._enable_thinking is not None:
            body["enable_thinking"] = self._enable_thinking
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            # Network-level errors are always retryable.
            raise _RetryableLLMError(f"network error: {exc}") from exc

        if resp.status_code in _RETRYABLE_STATUSES:
            raise _RetryableLLMError(
                f"retryable HTTP {resp.status_code}: {resp.text[:300]}"
            )
        if not resp.ok:
            raise LLMClientError(
                f"MiniMaxi HTTP {resp.status_code}: {resp.text[:500]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise LLMClientError(f"MiniMaxi returned non-JSON body: {resp.text[:300]}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMClientError(
                f"MiniMaxi response missing choices[0].message.content: {payload}"
            ) from exc
        return self._strip_thinking(content or "")

    def _call_anthropic(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        try:
            import anthropic  # local import keeps cold-start light
        except ImportError as exc:
            raise LLMClientError("anthropic SDK not installed") from exc

        client = anthropic.Anthropic(api_key=self._anthropic_api_key, timeout=self._timeout)
        try:
            msg = client.messages.create(
                model=self._anthropic_model,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            raise LLMClientError(f"Anthropic SDK error: {exc}") from exc

        try:
            # ``msg.content`` is a list of content blocks; concatenate text.
            parts: list[str] = []
            for block in msg.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            joined = "".join(parts).strip()
            if not joined:
                raise LLMClientError(f"Anthropic returned empty content: {msg}")
            return self._strip_thinking(joined)
        except AttributeError as exc:
            raise LLMClientError(f"Anthropic response shape unexpected: {msg}") from exc

    # Tags emitted by reasoning models before their final answer. We strip
    # whole `<tag>...</tag>` blocks, and if an opener is left dangling
    # (because ``max_tokens`` cut off mid-thought) we discard everything from
    # the opener onward — caller can retry with a larger budget.
    _THINKING_TAGS = ("think", "thinking", "reasoning", "reflection")

    @classmethod
    def _strip_thinking(cls, text: str) -> str:
        """Remove ``<think>...</think>``-style reasoning blocks.

        MiniMax M2+, DeepSeek R1, and similar models prepend a reasoning block
        before the actual answer. Downstream consumers (especially
        :meth:`complete_json`) need clean output, so we strip these blocks
        defensively. Anthropic models don't emit them inline, so this is a
        no-op for the fallback path.
        """
        import re

        cleaned = text
        for tag in cls._THINKING_TAGS:
            cleaned = re.sub(
                rf"<{tag}>.*?</{tag}>",
                "",
                cleaned,
                flags=re.DOTALL | re.IGNORECASE,
            )
        for tag in cls._THINKING_TAGS:
            opener = f"<{tag}>"
            idx = cleaned.lower().find(opener.lower())
            if idx >= 0:
                # Unclosed opener → model ran out of tokens before answering.
                cleaned = cleaned[:idx]
        return cleaned.strip()

    @staticmethod
    def _fire_alert(severity: str, message: str, exc: BaseException | None) -> None:
        """Emit a Lark alert at the given severity.

        2026-05-18: downgraded LLM-exhaustion alerts from P1 to P2. Reason:
        single-key MiniMaxi setup + no Anthropic fallback (cost-driven, see
        memory/user_llm_cost_constraint.md) means short MiniMaxi 429/5xx
        bursts trigger this path even though the next cron run will recover.
        Don't wake people up — let the heartbeat + weekly_reporter notice.
        """
        try:
            from lib.lark import alert

            alert(
                severity,
                f"LLMClient: {message}",
                {
                    "last_exception_type": type(exc).__name__ if exc else "unknown",
                    "last_exception": str(exc)[:500] if exc else "",
                },
            )
        except Exception:
            logger.exception("failed to emit %s Lark alert from LLMClient", severity)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


class _RetryableLLMError(Exception):
    """Marker exception for transient MiniMaxi failures (429 / 5xx / network)."""


def _strip_code_fences(text: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` fences and leading/trailing whitespace.

    Models occasionally wrap JSON in markdown fences despite instructions; be
    tolerant rather than failing on the first round trip.
    """
    s = text.strip()
    if s.startswith("```"):
        # Drop the first fence line (possibly ```json).
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[: -3].rstrip()
    return s.strip()


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #


llm: LLMClient = LLMClient()


__all__ = ["llm", "LLMClient", "LLMClientError"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    try:
        result = llm.complete(
            system="You are a haiku poet.",
            user="Write a haiku about SQLite WAL mode.",
            max_tokens=200,
            temperature=0.7,
        )
        logger.info("self-test haiku:\n%s", result)
    except LLMClientError as exc:
        logger.error("self-test failed: %s (cause=%s)", exc, exc.cause)
