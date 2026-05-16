"""Public signed-URL media endpoint — streams engine mp4s for Postiz to fetch.

WHY THIS EXISTS
---------------
Postiz YouTube provider (gitroomhq/postiz-app v0.x) needs an HTTPS URL
to GET media from when publishing to YouTube. Engine renders mp4 to
``runtime/drafts/<piece_id>/shorts_60s.mp4`` via MoneyPrinterTurbo;
this Blueprint exposes those files through the existing
``ingest.taskon.xyz`` cloudflared tunnel so Postiz can fetch them.

Without this route, Postiz's YT publish would fail with
``TypeError: Invalid URL`` (verified 2026-05-16 piece-02 S9.5 flow).

SECURITY
--------
* HMAC-signed URLs only (``lib.media_url.verify_media_url``). Unsigned
  or stale requests get 401. Defaults so empty ``MEDIA_URL_SECRET``
  silently disables — same safe-default pattern as the MPT callback.
* Path-traversal defense: ``piece_id`` and ``filename`` are rejected if
  they contain ``/`` ``..`` or start with ``.``. After resolving to
  absolute path, ``Path.relative_to(drafts_dir)`` confirms we never
  escape the drafts root (catches symlink trickery).
* Extension whitelist: only ``.mp4`` and ``.mp3`` are served. Markdown
  / yaml / json drafts must not leak through this route.
* Audit log on every request (single-line JSON), with outcome reason.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from lib.media_url import verify_media_url


logger = logging.getLogger("media_routes")

media_bp = Blueprint("media_routes", __name__)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_ALLOWED_SUFFIXES = {".mp4": "video/mp4", ".mp3": "audio/mpeg"}
_DRAFTS_DIR_ENV = "DRAFTS_DIR"
_DEFAULT_DRAFTS_DIR = "/app/runtime/drafts"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _drafts_dir() -> Path:
    return Path(os.environ.get(_DRAFTS_DIR_ENV) or _DEFAULT_DRAFTS_DIR)


def _err(status: int, code: str, message: str, **extra: Any) -> tuple[Response, int]:
    body: dict[str, Any] = {"status": "error", "code": code, "message": message}
    body.update(extra)
    return jsonify(body), status


def _safe_log(audit: dict[str, Any]) -> None:
    """Single-line JSON audit log. Never includes secrets / query string."""
    try:
        logger.info("media_serve %s", json.dumps(audit, ensure_ascii=False, sort_keys=True))
    except Exception:
        logger.exception("media_routes: audit serialization failed")


def _suffix(filename: str) -> str:
    """Lowercase suffix including the leading dot, or empty string."""
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


@media_bp.get("/api/media/<piece_id>/<filename>")
def serve_media(piece_id: str, filename: str):
    """Stream a media file from ``runtime/drafts/<piece_id>/<filename>``.

    Query params (required):
        expires (int)   unix seconds; rejected if past
        sig (hex)       HMAC-SHA256 over canonical signing string

    Responses:
        200 streaming   media bytes (supports Range, conditional GET)
        400 bad_*       piece_id / filename / suffix invalid
        401 unauthorized signature missing / expired / mismatch
        404 not_found   file not on disk
    """
    # 1. Sanitize ids (Flask routing won't permit / inside a path segment,
    #    but check defensively — protect against URL-decoded variants).
    if not piece_id or "/" in piece_id or ".." in piece_id or piece_id.startswith("."):
        return _err(400, "bad_piece_id", "piece_id format invalid")
    if not filename or "/" in filename or ".." in filename or filename.startswith("."):
        return _err(400, "bad_filename", "filename format invalid")

    # 2. Suffix whitelist.
    suffix = _suffix(filename)
    mimetype = _ALLOWED_SUFFIXES.get(suffix)
    if mimetype is None:
        _safe_log({"event": "bad_suffix", "filename": filename, "remote": request.remote_addr})
        return _err(400, "bad_suffix", f"only {sorted(_ALLOWED_SUFFIXES)} allowed")

    # 3. HMAC + expires verification.
    ok, reason = verify_media_url(
        piece_id,
        filename,
        request.args.get("expires"),
        request.args.get("sig"),
    )
    if not ok:
        _safe_log({
            "event": "auth_fail",
            "piece_id": piece_id,
            "filename": filename,
            "reason": reason,
            "remote": request.remote_addr,
        })
        return _err(401, "unauthorized", f"signed URL verification failed: {reason}")

    # 4. Resolve target + confirm it lives under drafts root.
    drafts_root = _drafts_dir().resolve()
    target = (drafts_root / piece_id / filename).resolve()
    try:
        target.relative_to(drafts_root)
    except ValueError:
        # Symlink trickery or unexpected path resolution — reject.
        _safe_log({
            "event": "path_escape",
            "piece_id": piece_id,
            "filename": filename,
            "remote": request.remote_addr,
        })
        return _err(400, "bad_path", "target outside drafts directory")

    if not target.is_file():
        _safe_log({
            "event": "not_found",
            "piece_id": piece_id,
            "filename": filename,
            "remote": request.remote_addr,
        })
        return _err(404, "not_found", f"no such media: {piece_id}/{filename}")

    size = target.stat().st_size
    _safe_log({
        "event": "serve",
        "piece_id": piece_id,
        "filename": filename,
        "size_bytes": size,
        "remote": request.remote_addr,
    })

    # ``conditional=True`` enables HTTP 304 + Range requests so Postiz /
    # YT-API can stream the 30+ MB mp4 incrementally.
    return send_file(
        target,
        mimetype=mimetype,
        as_attachment=False,
        conditional=True,
    )
