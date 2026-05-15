"""Production WSGI entry point.

Used by gunicorn:

    gunicorn --bind 0.0.0.0:5051 --workers 2 ingestion.wsgi:app
"""
from __future__ import annotations

from ingestion.app import app

__all__ = ["app"]
