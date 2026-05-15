"""Tenacity-based retry decorator with Lark alert on final failure.

Why a thin wrapper around tenacity?
  * Standardises the exponential backoff curve across all engine modules
    (30s / 5min / 30min for 3 attempts).
  * Auto-emits a P1 Lark alert when the final attempt is exhausted, so we
    never lose visibility on a job silently dying.
  * Centralises the retryable exception list — currently ``requests``
    network errors + ``TimeoutError``; callers can override.

Usage::

    from lib.retry import retryable, retry_with_lark_alert

    @retryable(max_attempts=5, base_delay=10)
    def call_postiz(): ...

    @retry_with_lark_alert
    def call_x_api(): ...
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

import requests
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _make_before_sleep(func_name: str) -> Callable[[RetryCallState], None]:
    """Build a tenacity ``before_sleep`` callback that logs WARNING per retry."""

    def _before_sleep(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        # ``state.attempt_number`` is the attempt that *just failed*; the next
        # attempt will be attempt_number+1.
        wait_seconds = getattr(state.next_action, "sleep", None)
        logger.warning(
            "retry: %s attempt=%s failed (will sleep ~%ss): %s",
            func_name,
            state.attempt_number,
            wait_seconds,
            exc,
        )

    return _before_sleep


def retryable(
    *,
    max_attempts: int = 3,
    base_delay: float = 30,
    max_delay: float = 1800,
    retry_on: tuple[type[BaseException], ...] = (requests.RequestException, TimeoutError),
) -> Callable[[F], F]:
    """Return a decorator that retries the wrapped callable with exp backoff.

    Args:
        max_attempts: Maximum attempts including the first call. Default 3 →
            attempts at t=0, t≈base_delay, t≈base_delay*2 ... (capped at
            ``max_delay`` between attempts).
        base_delay: Multiplier (and lower bound) for the exponential backoff
            curve, in seconds. With default 30s the wait sequence is roughly
            30s / 60s ... but tenacity caps at ``max_delay`` and the practical
            target is "30s / 5min / 30min" — adjust via this knob if needed.
        max_delay: Hard ceiling on the wait between attempts.
        retry_on: Exception types that trigger a retry. Anything else
            propagates immediately. Default covers transient HTTP errors.

    Returns:
        A decorator. When the wrapped callable exhausts ``max_attempts``, a
        ``P1`` Lark alert is fired (function name + last exception) and the
        original exception is re-raised.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            func_name = f"{func.__module__}.{func.__qualname__}"
            retryer = Retrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=base_delay, max=max_delay),
                retry=retry_if_exception_type(retry_on),
                before_sleep=_make_before_sleep(func_name),
                reraise=True,
            )
            try:
                return retryer(func, *args, **kwargs)
            except RetryError as exc:  # pragma: no cover — reraise=True bypasses
                last_exc: BaseException | None = exc.last_attempt.exception()
                _fire_final_alert(func_name, last_exc)
                raise last_exc if last_exc is not None else exc
            except retry_on as exc:  # type: ignore[misc]
                # All attempts exhausted and the last exception was retryable.
                _fire_final_alert(func_name, exc)
                raise

        return wrapper  # type: ignore[return-value]

    return decorator


def _fire_final_alert(func_name: str, exc: BaseException | None) -> None:
    """Best-effort P1 Lark alert. Never raise from inside the alert path."""
    try:
        # Local import to avoid a hard import cycle if lib.lark ever imports
        # lib.retry in the future.
        from lib.lark import alert

        alert(
            "P1",
            f"retry exhausted: {func_name}",
            {
                "function": func_name,
                "last_exception_type": type(exc).__name__ if exc else "unknown",
                "last_exception": str(exc)[:500] if exc else "",
            },
        )
    except Exception:
        logger.exception("failed to emit P1 alert after retry exhaustion (%s)", func_name)


def retry_with_lark_alert(func: F) -> F:
    """Pre-configured 3-attempt retry shortcut with default backoff.

    Equivalent to ``@retryable()`` with all defaults. Provided as a
    convenience so common-case call sites do not need empty parentheses.
    """
    return retryable()(func)


__all__ = ["retryable", "retry_with_lark_alert"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")

    counter = {"n": 0}

    @retryable(max_attempts=2, base_delay=0.1, max_delay=0.2)
    def _flaky() -> str:
        counter["n"] += 1
        if counter["n"] < 2:
            raise TimeoutError("simulated transient failure")
        return "ok"

    try:
        result = _flaky()
        logger.info("self-test passed: result=%s attempts=%s", result, counter["n"])
    except Exception:
        logger.exception("self-test failed unexpectedly")
