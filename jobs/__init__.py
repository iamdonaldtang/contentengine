"""TaskOn Marketing Engine · scheduled jobs.

Each module in this package is an independent entry point invoked via
``python -m jobs.<name>`` (also exposed in ``config.yaml`` ``scheduler.jobs``
for cron / Windows Task Scheduler).

Jobs MUST:
  * Use ``lib.db.db`` for all SQLite access (no direct ``sqlite3.connect``).
  * Read secrets from ``os.environ`` and non-secret config from ``config.yaml``.
  * Log via :mod:`logging` (never ``print``).
  * Define an ``if __name__ == "__main__":`` entry with :mod:`argparse`.
  * Record one row to the ``heartbeat`` table per run (success or failure).
"""
from __future__ import annotations

__all__: list[str] = []
