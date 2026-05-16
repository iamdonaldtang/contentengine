"""Signed media-URL helpers for the public media endpoint.

WHY THIS EXISTS
---------------
Postiz's YouTube provider needs an HTTPS URL it can GET at publish time
to upload media to YouTube. Engine's rendered mp4 lives on local disk
(``runtime/drafts/<piece_id>/shorts_60s.mp4``), so we expose it through
the existing cloudflared tunnel at ``ingest.taskon.xyz``. Postiz's
``/public/stream`` route has SSRF protection that rejects private IPs —
the tunnel is what makes the URL "public" without exposing the host.

Open access would leak (a) eventually-public marketing content
prematurely and (b) any in-flight draft media. HMAC-SHA256 + short TTL
means only callers with the secret can construct a valid fetch URL,
and stale URLs auto-expire.

DESIGN
------
URL shape::

    https://<MEDIA_URL_BASE>/api/media/<piece_id>/<filename>
        ?expires=<unix_int>&sig=<hex>

Canonical signing string::

    f"{piece_id}/{filename}.{expires}"

The same string is reconstructed server-side at verify time and compared
via :func:`hmac.compare_digest`. ``MEDIA_URL_SECRET`` (env) is the shared
HMAC key; rotation = change env + restart ingestion. Existing in-flight
URLs constructed with the old secret will fail verification, but TTL
defaults to 1h so the blast radius is small.

ENV CONTRACT
------------
* ``MEDIA_URL_BASE`` — full origin (e.g. ``https://ingest.taskon.xyz``).
  Required; must start with http:// or https://.
* ``MEDIA_URL_SECRET`` — 32-byte random hex (``openssl rand -hex 32``).
  Empty disables signing/verification (sign raises, verify returns
  False with reason='no_secret'). Safe default.

Hard rules (Prompt_AI系统化编程_v1.md §7):
* No silent failures — every branch logs/raises.
* No secret in logs (verify uses constant-time compare; sign callers
  log only the host of the URL).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time


logger = logging.getLogger(__name__)


MEDIA_URL_BASE_ENV = "MEDIA_URL_BASE"
MEDIA_URL_SECRET_ENV = "MEDIA_URL_SECRET"

DEFAULT_TTL_SECONDS = 3600  # 1h — Postiz fetches the URL once at publish time


class MediaUrlConfigError(RuntimeError):
    """Raised when env config is missing or invalid at sign time."""


def _canonical_signing_string(piece_id: str, filename: str, expires: int) -> str:
    """Stable canonical string used both at sign + verify time.

    Embedding ``expires`` in the signed content (rather than as a
    separate header) means a stale signature cannot be reused with a
    fresher timestamp.
    """
    return f"{piece_id}/{filename}.{expires}"


def _secret() -> str:
    """Read MEDIA_URL_SECRET from env. Raises if empty."""
    s = (os.environ.get(MEDIA_URL_SECRET_ENV) or "").strip()
    if not s:
        raise MediaUrlConfigError(
            f"{MEDIA_URL_SECRET_ENV} env not set — required for signing media URLs"
        )
    return s


def sign_media_url(
    piece_id: str,
    filename: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Build a fully-qualified, HMAC-signed media URL.

    Args:
        piece_id: e.g. ``"2026W20-thread02"``. Must not contain ``/`` or ``..``.
        filename: e.g. ``"shorts_60s.mp4"``. Must be a leaf name.
        ttl_seconds: URL valid for this many seconds (default 3600).
        now: Override "now" for deterministic tests.

    Returns:
        Fully-qualified URL with ``expires`` + ``sig`` query params.

    Raises:
        MediaUrlConfigError: ``MEDIA_URL_BASE`` or ``MEDIA_URL_SECRET`` unset.
        ValueError: piece_id / filename contains path-traversal markers,
            or ttl_seconds <= 0.
    """
    if not piece_id or "/" in piece_id or ".." in piece_id or piece_id.startswith("."):
        raise ValueError(f"piece_id format invalid: {piece_id!r}")
    if not filename or "/" in filename or ".." in filename or filename.startswith("."):
        raise ValueError(f"filename must be a leaf name: {filename!r}")
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds}")

    base = (os.environ.get(MEDIA_URL_BASE_ENV) or "").strip().rstrip("/")
    if not base:
        raise MediaUrlConfigError(f"{MEDIA_URL_BASE_ENV} env not set")
    if not base.startswith(("http://", "https://")):
        raise MediaUrlConfigError(
            f"{MEDIA_URL_BASE_ENV} must start with http:// or https://, got {base!r}"
        )

    expires = int(now if now is not None else time.time()) + int(ttl_seconds)
    sig = hmac.new(
        _secret().encode("utf-8"),
        _canonical_signing_string(piece_id, filename, expires).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{base}/api/media/{piece_id}/{filename}?expires={expires}&sig={sig}"


def verify_media_url(
    piece_id: str,
    filename: str,
    expires: str | None,
    sig: str | None,
    *,
    now: int | None = None,
) -> tuple[bool, str]:
    """Verify a request's ``sig`` + ``expires`` for ``(piece_id, filename)``.

    Returns ``(ok, reason)`` — ``reason`` is a short audit token suitable
    for logs (never contains secrets or user-controlled bytes).

    Reasons:
        ok                — accept
        missing_params    — expires or sig query param missing
        bad_expires_format — expires not a base-10 int
        expired_<n>s_ago  — clock past TTL
        no_secret         — MEDIA_URL_SECRET env unset (server-side misconfig)
        sig_mismatch      — HMAC didn't match
    """
    if not sig or not expires:
        return False, "missing_params"
    try:
        expires_int = int(expires)
    except (ValueError, TypeError):
        return False, "bad_expires_format"

    current = int(now if now is not None else time.time())
    if expires_int < current:
        return False, f"expired_{current - expires_int}s_ago"

    try:
        secret = _secret()
    except MediaUrlConfigError:
        return False, "no_secret"

    expected = hmac.new(
        secret.encode("utf-8"),
        _canonical_signing_string(piece_id, filename, expires_int).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "sig_mismatch"
    return True, "ok"


__all__ = [
    "MediaUrlConfigError",
    "MEDIA_URL_BASE_ENV",
    "MEDIA_URL_SECRET_ENV",
    "DEFAULT_TTL_SECONDS",
    "sign_media_url",
    "verify_media_url",
]
