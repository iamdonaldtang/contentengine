"""Newsletter Assembler · T6 · B1 §2 Newsletter section.

Parse the 5-section monthly Newsletter draft markdown (produced by Cowork's
``email-sequence`` skill or hand-written by the part-time editor), render
into HTML via Jinja2, and either:

* ``--dry-run`` — write the rendered HTML to
  ``runtime/newsletter_preview_<YYYY-MM>.html`` for Donald to inspect
* default (real) — POST to Listmonk via ``sources.listmonk.create_campaign``
  with a ``send_at`` scheduled at the month-end's penultimate Wednesday
  09:00 ET

Schedule (cron in docker/crontab)::

    25 09 25 * *   # 25th of each month 09:00 — generate draft + dry-run preview
    25 09 * * 3    # every Wednesday 09:00 — if month-end window, send-or-noop

The draft markdown must contain exactly these 5 ``##``-headed sections::

    # Newsletter Subject Line (optional H1 = subject)

    ## 头条
    <300 char intro about this month's main story>

    ## 案例
    <200 char customer case study highlight>

    ## 雷达
    <300 char "what's coming next month" radar>

    ## CTA
    <100 char call to action — text only; the URL is in the front-matter>

    ## 退订
    <unsubscribe instructions or boilerplate — Listmonk handles the link>

Front-matter (optional YAML block at file top) MAY supply:

    ---
    subject: "47% Quest 预算被 Bot 吃 — 你的项目可能在踩这个坑"
    cta_url: "https://taskon.xyz/benchmark-report?utm_source=listmonk..."
    palette:
      primary: "#0F172A"
      accent: "#2563EB"
    ---

If absent, the subject defaults to the H1; the CTA URL defaults to the env
var ``NEWSLETTER_DEFAULT_CTA_URL`` or ``https://taskon.xyz``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402

logger = logging.getLogger(__name__)

JOB_NAME = "newsletter_assembler"

# The 5 sections we require. Order = order in the draft markdown.
REQUIRED_SECTIONS: tuple[str, ...] = ("头条", "案例", "雷达", "CTA", "退订")
# Map markdown section name → template context variable.
SECTION_TO_CONTEXT: dict[str, str] = {
    "头条": "intro",
    "案例": "case_study",
    "雷达": "radar",
    "CTA": "cta",
    # "退订" is rendered by the template via {{UnsubscribeURL}} — body unused.
}


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    return Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)


def _runtime_dir() -> Path:
    return Path(os.environ.get("RUNTIME_DIR") or (_engine_root() / "runtime"))


def _templates_dir() -> Path:
    return _engine_root() / "templates"


# --------------------------------------------------------------------------- #
# Markdown parsing
# --------------------------------------------------------------------------- #


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML front-matter (between ``---`` delimiters at file top)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError as exc:
        logger.warning("newsletter front-matter parse failed: %s", exc)
        meta = {}
    return meta, m.group(2)


def parse_newsletter_md(text: str) -> dict[str, Any]:
    """Parse a 5-section newsletter markdown.

    Returns context dict with keys: subject, intro, case_study, radar, cta,
    cta_url, palette, send_at (optional ISO str).

    Raises:
        ValueError: When any required section is missing OR empty after strip.
    """
    meta, body = _split_front_matter(text)

    # H1 candidate for subject
    h1_match = re.search(r"^# +(.+?)$", body, re.MULTILINE)
    default_subject = h1_match.group(1).strip() if h1_match else "TaskOn Newsletter"

    # Split body on ``## `` headers
    sections: dict[str, str] = {}
    parts = re.split(r"^## +(.+?)$", body, flags=re.MULTILINE)
    # re.split with capturing group yields [pre, header, body, header, body, ...]
    if len(parts) >= 3:
        for i in range(1, len(parts) - 1, 2):
            header = parts[i].strip()
            content = parts[i + 1].strip()
            sections[header] = content

    missing: list[str] = []
    empty: list[str] = []
    for required in REQUIRED_SECTIONS:
        if required not in sections:
            missing.append(required)
        elif not sections[required].strip():
            empty.append(required)
    if missing:
        raise ValueError(f"newsletter missing sections: {missing}")
    if empty:
        raise ValueError(f"newsletter empty sections: {empty}")

    ctx: dict[str, Any] = {
        "subject": meta.get("subject") or default_subject,
        "cta_url": meta.get("cta_url") or os.environ.get("NEWSLETTER_DEFAULT_CTA_URL") or "https://taskon.xyz",
        "palette": meta.get("palette") or {},
        "send_at": meta.get("send_at"),  # None or ISO str
    }
    for sec_name, ctx_key in SECTION_TO_CONTEXT.items():
        ctx[ctx_key] = sections[sec_name]
    return ctx


# --------------------------------------------------------------------------- #
# Send-time math
# --------------------------------------------------------------------------- #


def penultimate_wednesday_of_month(year: int, month: int, hour: int = 9) -> dt.datetime:
    """Return month's last-but-one Wednesday at ``hour`` in America/New_York.

    Used as the default ``send_at`` per B1 §2: Newsletter ships near month-end
    so subscribers don't see two issues in the same week.
    """
    from calendar import monthrange
    from zoneinfo import ZoneInfo

    _, days_in_month = monthrange(year, month)
    wednesdays: list[int] = []
    for d in range(1, days_in_month + 1):
        if dt.date(year, month, d).weekday() == 2:  # 0=Mon, 2=Wed
            wednesdays.append(d)
    if len(wednesdays) < 2:
        # February + odd combos: fall back to last Wednesday.
        target_day = wednesdays[-1] if wednesdays else 1
    else:
        target_day = wednesdays[-2]  # penultimate
    return dt.datetime(year, month, target_day, hour, 0, 0, tzinfo=ZoneInfo("America/New_York"))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_html(context: dict[str, Any], send_date: dt.date | None = None) -> str:
    """Render the Jinja template with the parsed context dict."""
    env = Environment(
        loader=FileSystemLoader(str(_templates_dir())),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tpl = env.get_template("newsletter.html.j2")
    return tpl.render(
        send_date_str=(send_date or dt.date.today()).isoformat(),
        **context,
    )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    draft_path: Path,
    *,
    dry_run: bool = False,
    list_id: int | None = None,
    send_at_override: dt.datetime | None = None,
) -> dict[str, Any]:
    """End-to-end: parse → render HTML → (dry-run write preview | listmonk POST).

    Args:
        draft_path: Path to the 5-section markdown draft.
        dry_run: When True, write preview HTML and skip Listmonk.
        list_id: Listmonk subscriber list id (default from
            ``NEWSLETTER_LIST_ID`` env var).
        send_at_override: Optional explicit send_at; otherwise computed as
            the current month's penultimate Wednesday 09:00 ET.

    Returns:
        Summary dict (status, preview_path or campaign_id, send_at iso).
    """
    started_at = time.monotonic()
    draft_path = Path(draft_path)
    if not draft_path.is_file():
        msg = f"newsletter draft missing: {draft_path}"
        logger.error(msg)
        alert("P1", msg)
        _heartbeat("failed", started_at, error_message=msg, rows=0)
        raise FileNotFoundError(msg)

    text = draft_path.read_text(encoding="utf-8")
    try:
        ctx = parse_newsletter_md(text)
    except ValueError as exc:
        logger.error("newsletter parse failed: %s", exc)
        alert("P1", f"newsletter parse failed for {draft_path.name}", {"error": str(exc)[:300]})
        _heartbeat("failed", started_at, error_message=str(exc)[:300], rows=0)
        raise

    today = dt.date.today()
    html = render_html(ctx, send_date=today)

    # YYYY-MM derived from draft filename (`newsletter_draft_YYYY-MM.md`) or
    # current month as fallback.
    m = re.search(r"(\d{4})-(\d{2})", draft_path.name)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
    else:
        year, month = today.year, today.month
    year_month = f"{year:04d}-{month:02d}"

    if dry_run:
        preview_path = _runtime_dir() / f"newsletter_preview_{year_month}.html"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(html, encoding="utf-8")
        logger.info(
            "DRY-RUN newsletter preview written: %s (chars=%d, subject=%r)",
            preview_path, len(html), ctx["subject"],
        )
        _heartbeat("ok", started_at, rows=1)
        return {
            "status": "dry_run",
            "preview_path": str(preview_path),
            "subject": ctx["subject"],
            "html_chars": len(html),
        }

    # Real send path — resolve list_id + send_at then POST to Listmonk.
    list_id = list_id or int(os.environ.get("NEWSLETTER_LIST_ID", "0") or 0)
    if list_id <= 0:
        msg = "NEWSLETTER_LIST_ID env var not set; refusing to call Listmonk without a target list"
        logger.error(msg)
        alert("P1", msg)
        _heartbeat("failed", started_at, error_message=msg, rows=0)
        raise RuntimeError(msg)

    send_at = send_at_override or penultimate_wednesday_of_month(year, month)
    if ctx.get("send_at"):
        # Front-matter override (must be ISO string).
        try:
            send_at = dt.datetime.fromisoformat(str(ctx["send_at"]))
        except ValueError as exc:
            logger.warning("ignoring invalid send_at in front-matter (%s): %s", ctx["send_at"], exc)

    from sources.listmonk import ListmonkError, listmonk

    try:
        resp = listmonk.create_campaign(
            subject=ctx["subject"],
            body_html=html,
            list_id=list_id,
            send_at=send_at,
        )
    except ListmonkError as exc:
        logger.exception("listmonk create_campaign failed")
        alert(
            "P1",
            f"newsletter_assembler: listmonk create_campaign failed for {draft_path.name}",
            {"error": str(exc)[:300]},
        )
        _heartbeat("failed", started_at, error_message=str(exc)[:300], rows=0)
        raise

    campaign_id = resp.get("id")
    # Persist to newsletter_campaigns table for our own analytics.
    try:
        db.newsletter_campaigns.insert(
            campaign_id=str(campaign_id),
            send_time=send_at.isoformat() if send_at else None,
            subject_a=ctx["subject"],
            subject_b=None,
        )
    except Exception:
        logger.exception("newsletter_campaigns insert failed for campaign_id=%s", campaign_id)

    _heartbeat("ok", started_at, rows=1)
    return {
        "status": "scheduled",
        "campaign_id": campaign_id,
        "send_at": send_at.isoformat() if send_at else None,
        "subject": ctx["subject"],
    }


def _heartbeat(status: str, started_at: float, *, rows: int, error_message: str | None = None) -> None:
    duration = int(time.monotonic() - started_at)
    try:
        db.heartbeat.record(JOB_NAME, status, duration, rows_written=rows, error_message=error_message)
    except Exception:
        logger.exception("heartbeat write failed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m jobs.newsletter_assembler",
        description="Assemble Newsletter from 5-section markdown → render HTML → Listmonk (B1 §2).",
    )
    p.add_argument(
        "--draft-path",
        required=True,
        help="path to runtime/newsletter_draft_YYYY-MM.md (Cowork email-sequence output)",
    )
    p.add_argument("--dry-run", action="store_true", help="render preview HTML; don't call Listmonk")
    p.add_argument(
        "--list-id", type=int, default=None,
        help="Listmonk list id (overrides NEWSLETTER_LIST_ID env)",
    )
    p.add_argument(
        "--send-at", default=None,
        help="override send_at (ISO 8601 datetime); default = current month penultimate Wed 09:00 ET",
    )
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    send_at_override: dt.datetime | None = None
    if args.send_at:
        try:
            send_at_override = dt.datetime.fromisoformat(args.send_at)
        except ValueError as exc:
            logger.error("invalid --send-at %r: %s", args.send_at, exc)
            return 2
    try:
        summary = run(
            Path(args.draft_path),
            dry_run=args.dry_run,
            list_id=args.list_id,
            send_at_override=send_at_override,
        )
    except FileNotFoundError:
        return 2
    except Exception:  # noqa: BLE001
        logger.exception("newsletter_assembler top-level failure")
        return 1
    logger.info("newsletter_assembler done: %s", summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
