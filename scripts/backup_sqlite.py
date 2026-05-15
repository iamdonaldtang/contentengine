"""Daily SQLite backup script · cron 23:00.

Uses :meth:`lib.db.Database.backup_to` (SQLite online Backup API) so writers
can continue uninterrupted. Backups land in ``$BACKUP_DIR`` (default
``runtime/backups``) named ``state-YYYYMMDD-HHMMSS.db``.

After writing the new snapshot, prunes anything older than ``--keep-days``
(default reads ``config.yaml -> sqlite.backup_keep_days`` then ``14``).
Records a heartbeat row on success/failure.

CLI::

    python -m scripts.backup_sqlite
    python -m scripts.backup_sqlite --keep-days 30
    python -m scripts.backup_sqlite --dest D:/elsewhere/state-bak.db
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or (_ENGINE_ROOT / "runtime" / "backups"))
_CONFIG_YAML = _ENGINE_ROOT / "config.yaml"

_JOB_NAME = "backup_sqlite"
_DEFAULT_KEEP_DAYS = 14


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_keep_days(cli_value: int | None) -> int:
    """Resolve in priority: CLI flag > config.yaml > built-in default."""
    if cli_value is not None and cli_value > 0:
        return cli_value
    if _CONFIG_YAML.is_file():
        try:
            with _CONFIG_YAML.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            sqlite_cfg = (doc.get("sqlite") or {}) if isinstance(doc, dict) else {}
            v = sqlite_cfg.get("backup_keep_days")
            if isinstance(v, int) and v > 0:
                return v
        except Exception:
            logger.exception("failed to read backup_keep_days from %s; using default", _CONFIG_YAML)
    return _DEFAULT_KEEP_DAYS


def _new_dest_path(now: dt.datetime | None = None) -> Path:
    """Compose the dated backup filename."""
    now = now or dt.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return _BACKUP_DIR / f"state-{stamp}.db"


def prune_old_backups(keep_days: int, now: dt.datetime | None = None) -> int:
    """Delete ``state-*.db`` files older than ``keep_days``.

    Returns the count of files removed. Errors deleting individual files
    are logged but do not abort the prune loop.
    """
    if not _BACKUP_DIR.is_dir():
        return 0
    now = now or dt.datetime.now()
    cutoff = now - dt.timedelta(days=keep_days)
    removed = 0
    for p in _BACKUP_DIR.glob("state-*.db"):
        try:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime)
        except OSError:
            logger.warning("stat failed for %s; skipping", p)
            continue
        if mtime < cutoff:
            try:
                p.unlink()
                removed += 1
                logger.info("pruned old backup: %s (mtime=%s)", p.name, mtime.isoformat())
            except OSError as exc:
                logger.warning("failed to delete %s: %s", p, exc)
    return removed


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def run(keep_days: int | None = None, dest: Path | None = None) -> Path:
    """Take a backup snapshot, prune old files, write heartbeat."""
    started = time.monotonic()
    status = "ok"
    error_message: str | None = None
    rows_written = 0
    out_path: Path | None = None
    effective_keep_days = _resolve_keep_days(keep_days)

    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = dest or _new_dest_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("backing up %s -> %s", db.path, out_path)
        db.backup_to(out_path)
        rows_written = 1

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError(f"backup file empty or missing: {out_path}")

        removed = prune_old_backups(keep_days=effective_keep_days)
        logger.info(
            "backup_sqlite OK · dest=%s size=%d bytes pruned=%d keep_days=%d",
            out_path,
            out_path.stat().st_size,
            removed,
            effective_keep_days,
        )
    except Exception as exc:
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
        logger.exception("backup_sqlite failed")
        try:
            alert(
                "P1",
                "backup_sqlite failed",
                {"error": str(exc)[:300], "dest": str(out_path) if out_path else "?"},
            )
        except Exception:
            logger.exception("could not emit P1 alert for backup_sqlite failure")
        raise
    finally:
        duration = int(time.monotonic() - started)
        try:
            db.heartbeat.record(
                job_name=_JOB_NAME,
                status=status,
                duration_seconds=duration,
                rows_written=rows_written,
                error_message=error_message,
            )
        except Exception:
            logger.exception("failed to write heartbeat row for backup_sqlite")

    assert out_path is not None
    return out_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn SQLite daily backup")
    p.add_argument("--keep-days", type=int, default=None, help="override retention window")
    p.add_argument("--dest", default=None, help="override output path (single file)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        run(keep_days=args.keep_days, dest=Path(args.dest) if args.dest else None)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
