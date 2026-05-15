"""External API adapter layer for the TaskOn Marketing Engine.

This package contains pure transport adapters — one module per data source.
Each module exposes a singleton client (``postiz``, ``twitter_x``, ``ga4``,
``listmonk``, ``btouch``) used by the ``jobs/metrics_collector.py`` orchestrator.

Adapters have **zero business logic**: they only translate HTTP / SDK calls
into native Python dict / list shapes. Mapping into SQLite rows happens in
``jobs/``.

Callers should import the specific submodule they need; this package itself
exposes nothing.
"""
from __future__ import annotations

__all__: list[str] = []
