"""Shared post-callback download path for the A-design async MPT flow.

Used by:
  * ``ingestion.mpt_callback.mpt_callback`` — webhook handler (push path)
  * ``jobs.mpt_reconciler`` — cron tick that wins races against dropped
    callbacks (pull path)

Both paths share the same downstream logic: once the mpt_tasks row has
been atomically transitioned to status='completed', fetch the mp4 from
MPT and stamp ``media_path``. The actual atomic UPDATE happens in
``lib.db._MptTasksAdapter.mark_completed`` so we are safe to call this
even if the other path also tried (whoever lost just no-ops).

Why a separate module?
  * Keeps ``ingestion/`` purely Flask/HTTP-receiving (no scheduled-job
    concerns leak in).
  * Lets reconciler import a thread-spawning helper without dragging
    Flask Blueprint objects into the cron process.
  * Single test surface for both call sites.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from lib.db import db


logger = logging.getLogger("mpt_post_callback")

_DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR", "/app/runtime/drafts"))


def _download_mp4_thread(task_id: str, piece_id: str) -> None:
    """Background thread body: pull mp4 + stamp media_path.

    On download failure logs P2 (engine can still continue) and leaves
    media_path NULL so downstream cron can detect "completed but no
    media" rows. On crash logs P1 and exits.
    """
    try:
        from sources.mpt import mpt as mpt_client, MPTError
    except Exception:
        logger.exception("mpt_post_callback: failed importing sources.mpt for download")
        return

    dest = _DRAFTS_DIR / piece_id / "shorts_60s.mp4"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        actual_path = mpt_client.download_video(task_id, dest)
        db.mpt_tasks.set_media_path(task_id, str(actual_path))
        logger.info(
            "mpt download ok task_id=%s piece_id=%s path=%s size=%d",
            task_id, piece_id, actual_path,
            actual_path.stat().st_size if actual_path.exists() else 0,
        )
    except MPTError as exc:
        logger.error("mpt download fail task_id=%s piece_id=%s err=%s", task_id, piece_id, exc)
        try:
            from lib.lark import alert as lark_alert
            lark_alert(
                "P2",
                f"mpt download failed for piece={piece_id}",
                {"task_id": task_id, "error": str(exc)[:300]},
            )
        except Exception:
            logger.exception("mpt_post_callback: lark alert dispatch crashed")
    except Exception as exc:
        logger.exception("mpt download crashed task_id=%s piece_id=%s", task_id, piece_id)
        try:
            from lib.lark import alert as lark_alert
            lark_alert(
                "P1",
                f"mpt download crashed for piece={piece_id}",
                {"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
        except Exception:
            logger.exception("mpt_post_callback: lark alert dispatch crashed")


def spawn_download(task_id: str, piece_id: str) -> threading.Thread:
    """Start the download as a daemon thread and return it.

    Daemon=True so an engine restart cleanly kills the thread (the
    mpt_tasks row stays 'completed' but media_path NULL → reconciler /
    operator can re-trigger from the mp4_url already recorded).
    """
    t = threading.Thread(
        target=_download_mp4_thread,
        args=(task_id, piece_id),
        daemon=True,
        name=f"mpt-dl-{task_id[:12]}",
    )
    t.start()
    return t
