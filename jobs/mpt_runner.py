"""MPT Runner · short-video production cron (B1 §2.6/2.7, T4).

Reads ``runtime/drafts/<piece_id>/shorts_60s.md``, submits it to the local
MoneyPrinterTurbo (MPT) API, polls until rendered, downloads the mp4 to
``runtime/drafts/<piece_id>/shorts_60s.mp4``, and stamps the publishings
row's ``media_path``.

Cron position
-------------
Sits between:
  * Monday 09:00 — Cowork adapter_orchestrator writes shorts_60s.md
  * Sunday 22:00 — schedule_planner picks up the mp4 for YT Shorts / TikTok

Recommended cron: **Monday 11:00** (adapter done, before schedule_planner).

CLI
---

    python -m jobs.mpt_runner --piece-id 2026W19-thread01
    python -m jobs.mpt_runner --piece-id 2026W19-thread01 --voice en-US-AriaNeural --timeout 900
    python -m jobs.mpt_runner --piece-id 2026W19-thread01 --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from sources.mpt import (  # noqa: E402
    DEFAULT_RESOLUTION,
    DEFAULT_VOICE,
    MPTError,
    MPTTaskFailedError,
    MPTTimeoutError,
    mpt,
)

logger = logging.getLogger(__name__)

JOB_NAME = "mpt_runner"


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
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    piece_id: str,
    *,
    voice: str = DEFAULT_VOICE,
    resolution: str = DEFAULT_RESOLUTION,
    timeout_seconds: int = 600,
    dry_run: bool = False,
    script_filename: str = "shorts_60s.md",
    output_filename: str = "shorts_60s.mp4",
) -> dict[str, Any]:
    """Render the shorts script for ``piece_id`` via MPT and persist the mp4.

    Returns a summary dict (status, task_id, media_path, ...).

    Failure paths:
        * Script missing  → P1 alert, return status='skipped'
        * MPT submit fail → P1 alert, raise (cron exit non-zero)
        * Poll timeout    → P1 alert, raise
        * Download fail   → fallback: persist publishings with media_path=null
          so schedule_planner can skip TikTok / YT Shorts gracefully
    """
    started_at = time.monotonic()
    piece_dir = _piece_dir(piece_id)
    script_path = piece_dir / script_filename
    mp4_path = piece_dir / output_filename

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
        return {"piece_id": piece_id, "status": "skipped", "reason": "empty_script", "script_path": str(script_path)}

    if dry_run:
        logger.info(
            "DRY-RUN · piece=%s would submit %d-char script to %s "
            "(voice=%s, resolution=%s, target=%s)",
            piece_id, len(script), mpt.base_url, voice, resolution, mp4_path,
        )
        return {
            "piece_id": piece_id,
            "status": "dry_run",
            "script_chars": len(script),
            "target_path": str(mp4_path),
            "voice": voice,
            "resolution": resolution,
        }

    # ---- Submit ---- #
    try:
        task_id = mpt.submit_video(script, voice=voice, resolution=resolution)
    except MPTError as exc:
        logger.exception("MPT submit failed for piece=%s: %s", piece_id, exc)
        alert("P1", f"mpt_runner submit failed for {piece_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=str(exc)[:300], rows=0)
        raise

    # ---- Poll ---- #
    try:
        task_info = mpt.poll_task(task_id, timeout_seconds=timeout_seconds)
    except MPTTimeoutError as exc:
        logger.exception("MPT poll timeout piece=%s task=%s", piece_id, task_id)
        alert("P1", f"mpt_runner poll TIMEOUT piece={piece_id} task={task_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=f"timeout: {exc}", rows=0)
        raise
    except MPTTaskFailedError as exc:
        logger.exception("MPT task failed piece=%s task=%s", piece_id, task_id)
        alert("P1", f"mpt_runner task FAILED piece={piece_id} task={task_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=str(exc)[:300], rows=0)
        raise
    except MPTError as exc:
        logger.exception("MPT poll error piece=%s task=%s", piece_id, task_id)
        alert("P1", f"mpt_runner poll error piece={piece_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=str(exc)[:300], rows=0)
        raise

    # ---- Download (fallback to "no media" if download fails) ---- #
    download_ok = True
    download_err: str | None = None
    try:
        mpt.download_video(task_id, mp4_path)
    except MPTError as exc:
        download_ok = False
        download_err = str(exc)[:300]
        logger.warning(
            "MPT download failed for piece=%s task=%s; persisting publishings row "
            "WITHOUT media_path so caller can fallback: %s",
            piece_id, task_id, exc,
        )
        try:
            db.publish_failures.insert(
                severity="P2",
                source=JOB_NAME,
                failure_type="mpt_download_failed",
                failure_detail=f"piece={piece_id} task={task_id} err={exc}",
            )
        except Exception:
            logger.exception("publish_failures insert failed")
        try:
            alert("P2", f"mpt_runner download fallback piece={piece_id}", {"error": download_err})
        except Exception:
            logger.exception("alert emission failed")

    # ---- Persist publishings row(s) ---- #
    # The video powers BOTH yt_shorts and tiktok per B1 §2 default. We create
    # a publishings row for each so the schedule_planner finds media on each.
    media_path = str(mp4_path) if download_ok and mp4_path.is_file() else None
    pub_rows: list[int] = []
    for platform in ("yt_shorts", "tiktok"):
        try:
            pub_id = db.publishings.upsert(
                piece_id=piece_id,
                platform=platform,
                external_post_id=None,
                postiz_post_id=None,
                published_at=None,
                media_path=media_path,
            )
            pub_rows.append(pub_id)
            db.state_events.log(
                piece_id,
                from_state=None,
                to_state="drafted",
                actor=JOB_NAME,
                notes=f"platform={platform} task_id={task_id} media={'ok' if media_path else 'missing'}",
            )
        except Exception:
            logger.exception("publishings.upsert failed for piece=%s platform=%s", piece_id, platform)

    status = "ok" if download_ok else "warning"
    file_size = mp4_path.stat().st_size if media_path else 0
    _record_heartbeat(status, started_at, error_message=download_err, rows=len(pub_rows))

    summary = {
        "piece_id": piece_id,
        "status": status,
        "task_id": task_id,
        "task_info_keys": (
            list(task_info.keys())[:10] if isinstance(task_info, dict) else []
        ),
        "media_path": media_path,
        "file_size_bytes": file_size,
        "publishings_inserted": pub_rows,
        "download_error": download_err,
    }
    logger.info("mpt_runner done: %s", summary)
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
        description="Render short-form video for a piece via MoneyPrinterTurbo (B1 §2.6/2.7).",
    )
    p.add_argument("--piece-id", required=True, help="folder name under runtime/drafts/")
    p.add_argument("--voice", default=DEFAULT_VOICE, help=f"edge-tts voice id (default {DEFAULT_VOICE})")
    p.add_argument("--resolution", default=DEFAULT_RESOLUTION, help=f"WxH (default {DEFAULT_RESOLUTION})")
    p.add_argument("--timeout", type=int, default=600, help="poll timeout in seconds")
    p.add_argument("--dry-run", action="store_true", help="don't call MPT; print plan only")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    try:
        summary = run(
            args.piece_id,
            voice=args.voice,
            resolution=args.resolution,
            timeout_seconds=args.timeout,
            dry_run=args.dry_run,
        )
    except (MPTTimeoutError, MPTTaskFailedError, MPTError):
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("mpt_runner top-level failure")
        return 2
    return 0 if summary.get("status") in ("ok", "dry_run", "skipped") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
