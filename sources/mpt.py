"""MoneyPrinterTurbo (MPT) REST API client.

MPT is the **single source of truth for short-video generation** in the
TaskOn marketing stack (C §原则3 红线: "已部署不允许再接其他视频生成 API").
Deployed locally at ``${MPT_API_BASE}`` (defaults to
``http://host.docker.internal:8090`` so the engine container can reach
the host-side MPT instance on Windows/Mac dev boxes; Linux deployments
typically rewire to a docker-network alias like ``http://moneyprinterturbo-api:8090``).

Public surface
--------------

    submit_video(script, voice=..., resolution=...) -> task_id
        Create a new MPT task. Returns the task id string.

    poll_task(task_id, timeout_seconds=600) -> dict
        Block until the task reaches a terminal state (``state in (1, -1)``)
        or the timeout elapses. Returns the final task dict.

    download_video(task_id, dest_path) -> Path
        Stream the produced mp4 to ``dest_path`` and return it.

    submit_and_wait(script, dest_path, **kw) -> Path
        Convenience: submit + poll + download in one call.

Design rules (Prompt_AI系统化编程_v1.md §7)
------------------------------------------
* No silent failures — every branch logs or raises.
* No hardcoded path: the host:port comes from ``MPT_API_BASE``.
* HTTP calls all carry explicit ``timeout=``.
* External calls wrapped in :func:`lib.retry.retryable` for transient
  fault tolerance.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from lib.retry import retryable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class MPTError(RuntimeError):
    """Raised on any non-recoverable MoneyPrinterTurbo API error."""


class MPTConfigError(MPTError):
    """Raised when MPT is not reachable / configured."""


class MPTTaskFailedError(MPTError):
    """Raised when the polled task reaches a terminal failure state."""


class MPTTimeoutError(MPTError):
    """Raised when poll_task exceeds ``timeout_seconds``."""


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_VOICE = "zh-CN-YunxiNeural-Male"
DEFAULT_RESOLUTION = "1080x1920"  # 9:16 portrait — Shorts / TikTok
DEFAULT_TIMEOUT = 30  # per-HTTP-call


# MPT task state semantics (from harry0703/MoneyPrinterTurbo):
#   0 = pending / queued
#   1 = succeeded (final video ready)
#  -1 = failed (error in pipeline)
# Anything else => still running.
_MPT_TERMINAL_STATES: set[int] = {1, -1}


def _safe_host(url: str) -> str:
    """Return ``scheme://host:port`` from a URL, stripping path / query / fragment.

    Used for logging callback URLs without leaking any secret that callers
    might accidentally put in a query string (e.g. ``?token=...``).
    """
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "<unparseable>"


def _aspect_from_resolution(resolution: str) -> str:
    """Map ``WxH`` -> ``W:H`` aspect token MPT accepts (``9:16``, ``16:9``, ``1:1``)."""
    if "x" not in resolution:
        return resolution
    try:
        w, h = (int(x) for x in resolution.lower().split("x"))
    except ValueError:
        return resolution
    if w * 16 == h * 9:
        return "9:16"
    if h * 16 == w * 9:
        return "16:9"
    if w == h:
        return "1:1"
    # Reduce by gcd to a generic w:h
    from math import gcd
    g = gcd(w, h)
    return f"{w // g}:{h // g}"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class MPTClient:
    """MoneyPrinterTurbo REST client (lazy-initialised singleton)."""

    timeout: int = DEFAULT_TIMEOUT

    def __init__(self) -> None:
        self.base_url: str = (
            os.environ.get("MPT_API_BASE")
            or "http://host.docker.internal:8090"
        ).rstrip("/")
        if not self.base_url:
            logger.warning("MPTClient: MPT_API_BASE not set; calls will fail")

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Content-Type": "application/json"}

    # -- private --------------------------------------------------------- #

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """MPT wraps responses as ``{data: {...}}``. Peel one level when present."""
        if isinstance(payload, dict) and "data" in payload and len(payload) <= 3:
            return payload["data"]
        return payload

    @retryable()
    def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(url, json=body, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("MPT POST %s failed: %s", path, exc)
            raise
        if not resp.ok:
            logger.error("MPT POST %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise MPTError(f"POST {path} HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise MPTError(f"POST {path} returned non-JSON body") from exc

    @retryable()
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("MPT GET %s failed: %s", path, exc)
            raise
        if not resp.ok:
            logger.error("MPT GET %s -> %s: %s", path, resp.status_code, resp.text[:500])
            raise MPTError(f"GET {path} HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise MPTError(f"GET {path} returned non-JSON body") from exc

    # -- public --------------------------------------------------------- #

    def submit_video(
        self,
        script: str,
        *,
        subject: str | None = None,
        voice: str = DEFAULT_VOICE,
        resolution: str = DEFAULT_RESOLUTION,
        clip_duration: int = 5,
        video_count: int = 1,
        extra_params: dict[str, Any] | None = None,
        callback_url: str | None = None,
        callback_secret: str | None = None,
    ) -> str:
        """Create a new MPT task.

        Args:
            script: Full narration script (the 60-second Shorts copy).
            subject: Optional short title — used by MPT to drive Pexels keyword
                search. If omitted, the first line of ``script`` is used.
            voice: edge-tts voice id (e.g. ``zh-CN-YunxiNeural-Male`` or
                ``en-US-AriaNeural``). See MPT UI for the full list.
            resolution: Target resolution ``WxH``. Auto-converted to MPT's
                ``9:16`` / ``16:9`` aspect token.
            clip_duration: Seconds per stock-clip cut. Default 5 is a good
                fit for 60-second Shorts.
            video_count: Number of independent renders. Engine cron always
                requests 1 — caller can override for A/B variants.
            extra_params: Forwarded into the MPT body verbatim (override or
                add knobs MPT supports — voice_volume, bgm_type, ...).
            callback_url: Async callback (A-design 2026-05-16). When set
                together with ``callback_secret``, MPT will POST to this URL
                on task completion (or failure) with an HMAC-SHA256 signed
                body, eliminating engine-side polling. Opt-in: when both
                are ``None`` the sync poll model is preserved.
            callback_secret: Shared secret for the HMAC-SHA256 signature MPT
                attaches to the callback POST. Engine verifies via the same
                secret. Must be paired with ``callback_url`` (XOR rejected).

        Returns:
            The MPT task_id (string).

        Raises:
            MPTConfigError: ``MPT_API_BASE`` missing.
            MPTError: Submission failed, or callback args are
                inconsistent (only one of url/secret given, or url has
                non-http(s) scheme).
        """
        if not self.base_url:
            raise MPTConfigError("MPT_API_BASE not set")
        if not script or not isinstance(script, str):
            raise MPTError(f"submit_video: script must be non-empty string; got {script!r}")

        # XOR check: callback is opt-in but both fields must travel together.
        # Sending callback_url without secret would let MPT POST unauthenticated
        # to engine; sending secret without url is just nonsense.
        if bool(callback_url) ^ bool(callback_secret):
            raise MPTError(
                "submit_video: callback_url and callback_secret must both be set "
                "or both be None (XOR rejected to avoid unauthenticated callbacks)"
            )
        if callback_url and not callback_url.startswith(("http://", "https://")):
            raise MPTError(
                f"submit_video: callback_url must start with http:// or https://, "
                f"got: {callback_url[:60]!r}"
            )

        subj = subject or script.strip().splitlines()[0][:80]
        body: dict[str, Any] = {
            "video_subject": subj,
            "video_script": script,
            "voice_name": voice,
            "video_aspect": _aspect_from_resolution(resolution),
            "video_clip_duration": clip_duration,
            "video_count": video_count,
        }
        if extra_params:
            body.update(extra_params)
        if callback_url and callback_secret:
            body["callback_url"] = callback_url
            body["callback_secret"] = callback_secret

        raw = self._post("/api/v1/videos", body)
        data = self._unwrap(raw) or {}
        if not isinstance(data, dict):
            raise MPTError(f"submit_video: unexpected response shape: {type(data).__name__}")
        task_id = data.get("task_id") or data.get("taskId") or data.get("id")
        if not task_id:
            raise MPTError(f"submit_video: response missing task_id: {raw!r:.200}")
        # NEVER log callback_secret — only the host of the URL for ops debugging.
        if callback_url:
            logger.info(
                "MPT submit ok (callback mode) task_id=%s voice=%s aspect=%s callback_host=%s",
                task_id, voice, body["video_aspect"], _safe_host(callback_url),
            )
        else:
            logger.info("MPT submit ok task_id=%s voice=%s aspect=%s", task_id, voice, body["video_aspect"])
        return str(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Single non-blocking GET of an MPT task — never polls, never sleeps.

        Used by ``jobs.mpt_reconciler`` to check whether a task has reached
        a terminal state since the engine last looked. Distinct from
        :meth:`poll_task` which loops for minutes.

        Returns the unwrapped task dict (``state``, ``progress``, ``videos``
        etc.).

        Raises:
            MPTError: HTTP failure after retries.
            MPTConfigError: ``MPT_API_BASE`` not set.
        """
        if not task_id:
            raise MPTError("get_task: task_id must be non-empty")
        if not self.base_url:
            raise MPTConfigError("MPT_API_BASE not set")
        raw = self._get(f"/api/v1/tasks/{task_id}")
        data = self._unwrap(raw) or {}
        if not isinstance(data, dict):
            raise MPTError(f"get_task: unexpected response shape: {type(data).__name__}")
        return data

    def poll_task(
        self,
        task_id: str,
        *,
        timeout_seconds: int = 600,
        poll_interval: float = 5.0,
    ) -> dict[str, Any]:
        """Poll an MPT task until terminal state or timeout.

        Args:
            task_id: Task identifier returned by :meth:`submit_video`.
            timeout_seconds: Hard cap. Defaults to 10 min — typical 60-second
                Shorts renders complete in 1-3 min on a CPU box.
            poll_interval: Seconds between polls. MPT does not stream events,
                so polling is the only signal.

        Returns:
            The final task dict (keys vary; ``state``, ``progress``,
            ``videos`` are typical).

        Raises:
            MPTTaskFailedError: ``state == -1``.
            MPTTimeoutError: Hit the cap before terminal state.
            MPTError: Any HTTP failure after retries.
        """
        if not task_id:
            raise MPTError("poll_task: task_id must be non-empty")
        deadline = time.monotonic() + max(10, timeout_seconds)
        last_state: Any = None
        last_progress: Any = None
        while time.monotonic() < deadline:
            raw = self._get(f"/api/v1/tasks/{task_id}")
            data = self._unwrap(raw) or {}
            if isinstance(data, dict):
                state = data.get("state")
                last_state = state
                last_progress = data.get("progress")
                if state in _MPT_TERMINAL_STATES:
                    if state == -1:
                        logger.error("MPT task %s FAILED: %s", task_id, data)
                        raise MPTTaskFailedError(
                            f"task {task_id} failed: {data.get('error') or data}"
                        )
                    logger.info(
                        "MPT task %s done: progress=%s videos=%s",
                        task_id, last_progress, data.get("videos") or data.get("combined_videos"),
                    )
                    return data
            time.sleep(poll_interval)
        raise MPTTimeoutError(
            f"task {task_id} did not finish within {timeout_seconds}s "
            f"(last state={last_state} progress={last_progress})"
        )

    def download_video(self, task_id: str, dest_path: Path) -> Path:
        """Stream the produced mp4 to ``dest_path`` and return it.

        Tries multiple URL conventions in order — newer MPT versions expose
        ``/tasks/{task_id}/final-1.mp4``; older variants use
        ``/api/v1/static/{task_id}.mp4`` or ``/output/{task_id}.mp4``. The
        first one that returns 200 wins.
        """
        if not self.base_url:
            raise MPTConfigError("MPT_API_BASE not set")
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        candidates = [
            f"{self.base_url}/tasks/{task_id}/final-1.mp4",
            f"{self.base_url}/tasks/{task_id}/combined-1.mp4",
            f"{self.base_url}/api/v1/static/{task_id}.mp4",
            f"{self.base_url}/api/v1/tasks/{task_id}/download",
            f"{self.base_url}/output/{task_id}.mp4",
        ]
        last_err: str = ""
        for url in candidates:
            try:
                with requests.get(url, stream=True, timeout=60) as resp:
                    if resp.status_code == 404:
                        last_err = f"404 at {url}"
                        continue
                    if not resp.ok:
                        last_err = f"HTTP {resp.status_code} at {url}: {resp.text[:200]}"
                        continue
                    with dest_path.open("wb") as fp:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                fp.write(chunk)
                logger.info(
                    "MPT download ok: %s -> %s (%d bytes)",
                    url, dest_path, dest_path.stat().st_size,
                )
                return dest_path
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__} at {url}: {exc}"
                continue
        raise MPTError(f"download_video: no working URL for task_id={task_id}; last_err={last_err}")

    # -- convenience ---------------------------------------------------- #

    def submit_and_wait(
        self,
        script: str,
        dest_path: Path,
        *,
        timeout_seconds: int = 600,
        **submit_kwargs: Any,
    ) -> Path:
        """One-call submit → poll → download. Returns ``dest_path``."""
        task_id = self.submit_video(script, **submit_kwargs)
        self.poll_task(task_id, timeout_seconds=timeout_seconds)
        return self.download_video(task_id, dest_path)


# Module-level singleton
mpt: MPTClient = MPTClient()


__all__ = [
    "mpt",
    "MPTClient",
    "MPTError",
    "MPTConfigError",
    "MPTTaskFailedError",
    "MPTTimeoutError",
    "DEFAULT_VOICE",
    "DEFAULT_RESOLUTION",
]


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.info("MPTClient base_url=%s", mpt.base_url)
    try:
        # Cheap reachability probe — list tasks endpoint or root health.
        # MPT exposes /docs at root for swagger; HEAD that.
        r = requests.head(f"{mpt.base_url}/docs", timeout=5)
        logger.info("MPT reachable: %s", r.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.error("MPT unreachable: %s", exc)
