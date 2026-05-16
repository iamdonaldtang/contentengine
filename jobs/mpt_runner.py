"""MPT Runner · async submit-and-exit (A-design 2026-05-16).

WHY THIS LOOKS DIFFERENT FROM ITS HISTORY
-----------------------------------------
Pre-A-design, this module did the whole render pipeline synchronously:
submit → poll for 5-10 min → download mp4 → write publishings rows.
That blocked engine main thread, ate cron slots, and propagated MPT
timeouts as engine-side P1 failures.

Now mpt_runner only submits:

  1. Read ``runtime/drafts/<piece_id>/shorts_60s.md`` + extract the
     ``## 纯旁白稿`` narration block (existing logic preserved — see
     ``_extract_narration``).
  2. INSERT a fresh row in ``mpt_tasks`` (status='pending_submit').
     The row exists *before* the MPT POST so the reconciler can find
     it even if submit hangs / crashes.
  3. POST /api/v1/videos to MPT with ``callback_url`` + ``callback_secret``
     in the body — MPT will call us back when render finishes.
  4. UPDATE the row to status='submitted' with the returned task_id.
  5. Exit (< 1 sec total wall time excluding HTTP RTT).

Downstream:
  * ``ingestion/api/mpt-callback`` receives MPT's webhook → atomically
    flips status to 'completed' / 'failed' → spawns mp4 download.
  * ``jobs/mpt_reconciler`` (cron every 5min) catches dropped callbacks.
  * ``schedule_planner`` (Sunday 22:00) reads ``shorts_60s.md`` and posts
    to Postiz; the mp4 file at the canonical path will exist by then
    (single render takes 5-10 min; 11+ h buffer to Sunday).

Idempotency:
  If a piece already has an in-flight (pending_submit / submitted) row,
  this cron tick returns immediately with status='already_in_flight'.
  Stops cron retries from queuing duplicate renders. Override with
  ``--force`` if the operator deliberately wants to re-submit.

Hard rules (Prompt_AI系统化编程_v1.md §7):
* No silent failures — every branch logs and either returns or alerts.
* No catch-and-pass — exceptions either propagate (cron exit non-zero)
  or are wrapped with a P-alert.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from sources.mpt import DEFAULT_RESOLUTION, DEFAULT_VOICE, MPTError, mpt  # noqa: E402


logger = logging.getLogger(__name__)

JOB_NAME = "mpt_runner"

CALLBACK_URL_ENV = "MPT_CALLBACK_URL"
CALLBACK_SECRET_ENV = "MPT_CALLBACK_SECRET"
DEFAULT_CALLBACK_URL = "http://taskon-engine:5051/api/mpt-callback"


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    return Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)


def _drafts_dir() -> Path:
    return Path(os.environ.get("DRAFTS_DIR") or (_engine_root() / "runtime" / "drafts"))


def _piece_dir(piece_id: str) -> Path:
    return _drafts_dir() / piece_id


# --------------------------------------------------------------------------- #
# Narration extraction (preserved from previous version)
# --------------------------------------------------------------------------- #
#
# Matches "## 纯旁白稿", "### narration only", "## TTS input" etc.
# Authors write shorts_60s.md as a tri-track script (VISUAL / VOICE / SUBTITLE
# per timing block), then append a "## 纯旁白稿（MPT TTS 输入用）" section that
# contains the narration prose alone. Without this extraction MPT reads aloud
# every [VISUAL] / [SUBTITLE] marker — wasting audio time and breaking the
# Azure TTS subtitle alignment (root caused 2026-05-15).
_NARRATION_HEADER_RE = re.compile(
    r"(?im)^[ \t]*#{1,4}[ \t]*(?:纯旁白稿|narration only|narration-only|tts input)\b.*?$"
)


def _extract_narration(script: str) -> str:
    """Return only the TTS-ready narration paragraph(s) from a shorts script.

    Older single-track scripts (no narration header) are returned unchanged.
    HTML comments inside the narration body are stripped because authors
    sometimes leave ``<!-- sources: ... -->`` blocks the TTS would vocalize.
    """
    match = _NARRATION_HEADER_RE.search(script)
    if not match:
        return script
    body = script[match.end():]
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return body.strip()


# --------------------------------------------------------------------------- #
# Callback env validation
# --------------------------------------------------------------------------- #


def _require_callback_config() -> tuple[str, str]:
    """Pull (url, secret) from env. Raises RuntimeError if either missing.

    The A-design async path is the only supported render path now; we don't
    silently fall back to a sync mode because that's the bug we removed.
    """
    url = os.environ.get(CALLBACK_URL_ENV, "").strip() or DEFAULT_CALLBACK_URL
    secret = os.environ.get(CALLBACK_SECRET_ENV, "").strip()
    if not secret:
        raise RuntimeError(
            f"{CALLBACK_SECRET_ENV} env not set. mpt_runner requires async-callback "
            "mode (A-design 2026-05-16). Set this env on both engine + MPT containers."
        )
    if not url.startswith(("http://", "https://")):
        raise RuntimeError(
            f"{CALLBACK_URL_ENV}={url!r} must be a full http(s) URL "
            "(MPT cannot resolve container hostnames otherwise)."
        )
    return url, secret


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    piece_id: str,
    *,
    voice: str = DEFAULT_VOICE,
    resolution: str = DEFAULT_RESOLUTION,
    dry_run: bool = False,
    force: bool = False,
    script_filename: str = "shorts_60s.md",
) -> dict[str, Any]:
    """Submit a render task to MPT and return immediately.

    Returns a summary dict with keys:
        status: 'submitted' | 'dry_run' | 'skipped' | 'already_in_flight' | 'failed'
        piece_id, task_id (if submitted), mpt_task_row_id, reason (if skipped)

    Status semantics:
        submitted         — POST succeeded, task_id assigned, row marked submitted
        already_in_flight — a previous tick is still pending/submitted (return its row)
        skipped           — input missing / empty
        dry_run           — args.dry_run set; nothing submitted
        failed            — submit POST raised; row marked failed
    """
    started_at = time.monotonic()
    piece_dir = _piece_dir(piece_id)
    script_path = piece_dir / script_filename

    # ---- Read + extract narration ---- #
    if not script_path.is_file():
        msg = f"shorts script missing: {script_path}"
        logger.warning(msg)
        alert("P1", f"mpt_runner: {msg}", {"piece_id": piece_id})
        _record_heartbeat("warning", started_at, error_message=msg, rows=0)
        return {"piece_id": piece_id, "status": "skipped", "reason": "no_script", "script_path": str(script_path)}

    script = script_path.read_text(encoding="utf-8").strip()
    if not script:
        msg = f"shorts script empty: {script_path}"
        logger.warning(msg)
        alert("P1", f"mpt_runner: {msg}", {"piece_id": piece_id})
        _record_heartbeat("warning", started_at, error_message=msg, rows=0)
        return {"piece_id": piece_id, "status": "skipped", "reason": "empty_script"}

    narration = _extract_narration(script)
    if not narration:
        msg = f"shorts script has no narration after extraction: {script_path}"
        logger.warning(msg)
        alert("P1", f"mpt_runner: {msg}", {"piece_id": piece_id})
        _record_heartbeat("warning", started_at, error_message=msg, rows=0)
        return {"piece_id": piece_id, "status": "skipped", "reason": "no_narration"}

    if narration is not script:
        logger.info(
            "extracted narration · piece=%s · %d chars from %d-char script",
            piece_id, len(narration), len(script),
        )

    # ---- Dry-run early exit (no DB writes, no MPT POST) ---- #
    if dry_run:
        # Still validate callback config so dry-run catches misconfiguration.
        try:
            cb_url, _ = _require_callback_config()
        except RuntimeError as exc:
            return {"piece_id": piece_id, "status": "failed", "reason": "config_invalid", "error": str(exc)}
        logger.info(
            "DRY-RUN · piece=%s would submit %d-char narration to %s "
            "(voice=%s, resolution=%s, callback=%s)",
            piece_id, len(narration), mpt.base_url, voice, resolution, cb_url,
        )
        return {
            "piece_id": piece_id,
            "status": "dry_run",
            "script_chars": len(script),
            "narration_chars": len(narration),
            "voice": voice,
            "resolution": resolution,
        }

    # ---- Validate callback env BEFORE inserting mpt_tasks row ---- #
    try:
        callback_url, callback_secret = _require_callback_config()
    except RuntimeError as exc:
        logger.error("mpt_runner config invalid: %s", exc)
        alert("P0", f"mpt_runner config invalid for {piece_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=str(exc)[:300], rows=0)
        return {"piece_id": piece_id, "status": "failed", "reason": "config_invalid", "error": str(exc)}

    # ---- Idempotency guard: another tick already submitted? ---- #
    if not force:
        existing = db.mpt_tasks.get_in_flight_for_piece(piece_id)
        if existing is not None:
            logger.info(
                "mpt_runner: piece=%s already in flight (row_id=%d status=%s task_id=%s) — skip resubmit",
                piece_id, existing["id"], existing["status"], existing["task_id"],
            )
            _record_heartbeat("ok", started_at, error_message=None, rows=0)
            return {
                "piece_id": piece_id,
                "status": "already_in_flight",
                "mpt_task_row_id": existing["id"],
                "task_id": existing["task_id"],
                "existing_status": existing["status"],
            }

    # ---- Insert pending_submit row BEFORE MPT POST ---- #
    row_id = db.mpt_tasks.create_pending(piece_id)
    logger.debug("mpt_runner: created mpt_tasks row_id=%d piece=%s", row_id, piece_id)

    # ---- Submit to MPT with callback ---- #
    try:
        task_id = mpt.submit_video(
            narration,
            voice=voice,
            resolution=resolution,
            callback_url=callback_url,
            callback_secret=callback_secret,
        )
    except MPTError as exc:
        logger.exception("MPT submit failed for piece=%s: %s", piece_id, exc)
        db.mpt_tasks.mark_submit_failed(row_id, f"{type(exc).__name__}: {exc}"[:500])
        alert("P1", f"mpt_runner submit failed for {piece_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=str(exc)[:300], rows=1)
        return {"piece_id": piece_id, "status": "failed", "reason": "submit_error", "mpt_task_row_id": row_id, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.exception("mpt_runner unexpected error for piece=%s", piece_id)
        db.mpt_tasks.mark_submit_failed(row_id, f"unexpected {type(exc).__name__}: {exc}"[:500])
        alert("P1", f"mpt_runner crashed for {piece_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=str(exc)[:300], rows=1)
        return {"piece_id": piece_id, "status": "failed", "reason": "crash", "mpt_task_row_id": row_id, "error": str(exc)}

    # ---- Mark submitted ---- #
    transition_ok = db.mpt_tasks.mark_submitted(row_id, task_id)
    if not transition_ok:
        # Defensive: should never happen unless something else mutated the row mid-flight.
        logger.error(
            "mpt_runner: mark_submitted returned False for row_id=%d task_id=%s — inspect mpt_tasks",
            row_id, task_id,
        )
        alert("P1", f"mpt_runner mark_submitted no-op for {piece_id}",
              {"row_id": row_id, "task_id": task_id})

    duration_s = int(time.monotonic() - started_at)
    _record_heartbeat("ok", started_at, error_message=None, rows=1)

    summary = {
        "piece_id": piece_id,
        "status": "submitted",
        "mpt_task_row_id": row_id,
        "task_id": task_id,
        "narration_chars": len(narration),
        "callback_url": callback_url,
        "duration_seconds": duration_s,
    }
    logger.info(
        "mpt_runner submitted piece=%s task_id=%s duration=%ds — waiting for callback",
        piece_id, task_id, duration_s,
    )
    return summary


# --------------------------------------------------------------------------- #
# Heartbeat helper
# --------------------------------------------------------------------------- #


def _record_heartbeat(status: str, started_at: float, *, error_message: str | None, rows: int) -> None:
    duration = int(time.monotonic() - started_at)
    try:
        db.heartbeat.record(
            JOB_NAME, status, duration,
            rows_written=rows,
            error_message=error_message,
        )
    except Exception:
        logger.exception("heartbeat write failed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m jobs.mpt_runner",
        description=(
            "Submit a piece's shorts narration to MoneyPrinterTurbo. "
            "Returns immediately after MPT acknowledges submit — render "
            "completion arrives via /api/mpt-callback (A-design 2026-05-16)."
        ),
    )
    p.add_argument("--piece-id", required=True, help="folder name under runtime/drafts/")
    p.add_argument("--voice", default=DEFAULT_VOICE, help=f"edge-tts voice id (default {DEFAULT_VOICE})")
    p.add_argument("--resolution", default=DEFAULT_RESOLUTION, help=f"WxH (default {DEFAULT_RESOLUTION})")
    p.add_argument("--dry-run", action="store_true", help="validate inputs + env; don't call MPT or write DB")
    p.add_argument("--force", action="store_true", help="bypass in-flight idempotency check (re-submit even if pending row exists)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    summary = run(
        args.piece_id,
        voice=args.voice,
        resolution=args.resolution,
        dry_run=args.dry_run,
        force=args.force,
    )
    status = summary.get("status")
    if status in ("submitted", "dry_run", "skipped", "already_in_flight"):
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
