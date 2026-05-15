"""TaskOn Marketing Engine · shared library layer.

All modules MUST import the singletons exposed here rather than building
their own database connections, HTTP retry loops, or LLM clients. This
keeps tracing, alerting, and resource accounting consistent.
"""

from lib.db import db  # noqa: F401
