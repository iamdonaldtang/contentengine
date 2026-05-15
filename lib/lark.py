"""Lark (Feishu) webhook alerting · P0/P1/P2 severity-coded interactive cards.

The single public entry point is :func:`alert`, which posts an Interactive Card
to the configured Lark webhook (``LARK_WEBHOOK_URL``) and *also* records the
alert into the ``publish_failures`` table for forensic replay.

Design constraints (Prompt_AI系统化编程_v1.md §7):
  * Never raise — alerting is a side-channel and must not crash callers.
  * Every external HTTP call carries an explicit ``timeout=``.
  * All exceptions logged via :mod:`logging`.
  * No hard-coded URLs or keys; everything resolved from ``os.environ``.

If ``ENABLE_LARK_ALERTS`` is ``"false"`` (or the webhook URL is empty), the
function short-circuits, logs at INFO, still records to ``publish_failures``,
and returns ``False``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

import requests
from dotenv import load_dotenv

from lib.db import db

logger = logging.getLogger(__name__)

# Lazy dotenv load — repeated calls are cheap (python-dotenv caches by path).
load_dotenv(override=False)

_SEVERITY_COLOR: dict[str, str] = {
    "P0": "red",
    "P1": "yellow",
    "P2": "grey",  # Lark card template color uses "grey", not "gray"
}

_LARK_TIMEOUT_SECONDS = 5


def _build_card(
    severity: Literal["P0", "P1", "P2"],
    message: str,
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    """Construct the Lark Interactive Card payload.

    Lark's ``interactive`` message format expects ``msg_type='interactive'``
    with a ``card`` body containing ``header`` (with ``template`` color +
    title) and ``elements`` (list of div / markdown blocks).

    Lark custom-robot webhooks with "keyword security" enabled reject any
    payload whose serialized text does not contain at least one configured
    keyword (error code 19024 "Key Words Not Found"). To survive that, we
    embed every common keyword candidate into the card body via env var
    ``LARK_KEYWORD`` (default: ``"TaskOn 告警 alert engine notification"``).
    Comma- or space-separated; all tokens are injected as an invisible-ish
    footer line so Lark's substring scan finds at least one match.
    """
    color = _SEVERITY_COLOR.get(severity, "grey")
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{message}**",
            },
        }
    ]
    if details:
        lines = [f"- **{k}**: {v}" for k, v in details.items()]
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines),
                },
            }
        )
    # Keyword embedding — required to satisfy Lark's custom-robot keyword
    # filter. Lark scans card.header.title + card.elements[].text.content for
    # substring matches; `note` tag is NOT scanned. We inject the keyword
    # into BOTH the title and a visible div so any reasonable keyword config
    # gets matched. If Donald set a custom keyword in Lark's security tab,
    # he should mirror it via LARK_KEYWORD in .env.
    keyword_blob = (
        os.environ.get("LARK_KEYWORD") or "TaskOn 告警 alert engine notification"
    ).strip()
    # Visible keyword line (small grey text via lark_md italics)
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>_{keyword_blob}_</font>",
            },
        }
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": color,
                "title": {
                    "tag": "plain_text",
                    # Embed keyword tokens directly into the title — Lark scans
                    # this field unconditionally.
                    "content": f"[{severity}] TaskOn Engine 告警 · {keyword_blob[:40]}",
                },
            },
            "elements": elements,
        },
    }


def alert(
    severity: Literal["P0", "P1", "P2"],
    message: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Send a Lark alert and record it to ``publish_failures``.

    Args:
        severity: One of ``"P0"``, ``"P1"``, ``"P2"``. Drives card header color
            and is recorded verbatim into the database row.
        message: Short human-readable description (1-line summary recommended).
        details: Optional structured key/value extras rendered as a markdown
            bullet list inside the card body.

    Returns:
        ``True`` if the Lark webhook acknowledged with HTTP 2xx, else ``False``.
        A ``False`` return *never* indicates a programming error — callers can
        safely ignore it.
    """
    # Always record to DB regardless of webhook success — this is the audit
    # trail.
    try:
        db.publish_failures.insert(
            severity=severity,
            source="alert",
            failure_type="lark_alert",
            failure_detail=message,
        )
    except Exception:
        logger.exception("failed to record lark alert into publish_failures")

    enabled = os.environ.get("ENABLE_LARK_ALERTS", "true").strip().lower() == "true"
    webhook_url = (os.environ.get("LARK_WEBHOOK_URL") or "").strip()

    if not enabled:
        logger.info("lark alerts disabled (ENABLE_LARK_ALERTS=false); skipping send")
        return False
    if not webhook_url:
        logger.warning("LARK_WEBHOOK_URL not set; cannot send %s alert: %s", severity, message)
        return False

    payload = _build_card(severity, message, details)

    try:
        resp = requests.post(webhook_url, json=payload, timeout=_LARK_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning("lark webhook request failed: %s", exc)
        return False
    except Exception:  # noqa: BLE001 — defensive; alerting must never raise.
        logger.exception("unexpected error posting to lark webhook")
        return False

    if not resp.ok:
        logger.warning(
            "lark webhook returned non-2xx: status=%s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        return False

    # Lark returns {"code":0,"msg":"success",...} on success and non-zero codes
    # on validation failures despite HTTP 200. Try to parse.
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("code") not in (0, None):
            logger.warning("lark webhook reported business error: %s", body)
            return False
    except ValueError:
        # Non-JSON 200 is unusual but treat as success since HTTP is fine.
        pass

    logger.info("lark alert sent: severity=%s message=%s", severity, message)
    return True


__all__ = ["alert"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    ok = alert(
        "P2",
        "lib.lark self-test ping",
        {"module": "lib/lark.py", "purpose": "smoke test"},
    )
    logger.info("self-test result: sent=%s", ok)
