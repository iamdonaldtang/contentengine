"""UTM Generator + Short Link · PRD §2.3.

Generates per-(platform, account) UTM-tagged URLs and shortens them via the
self-hosted shlink instance. Saves the dict to
``runtime/drafts/<piece_id>/utm_links.json``.

Contract for ``lib.utm`` (built by subagent A):
    @dataclass
    class UTMParams:
        source:   str  # twitter | linkedin | reddit | ...
        medium:   str  # thread | post | carousel | shorts | ...
        campaign: str  # piece_id, lowercased
        content:  str  # account handle (donald_en | taskon_official | ...)
        term:     str  # hook_type

    def build_utm(p: UTMParams) -> str: ...        # returns "?utm_source=..."
    def append_utm(base_url: str, p: UTMParams) -> str: ...

Contract for ``lib.shlink`` (built by subagent A):
    shlink.shorten(long_url: str, custom_slug: str | None = None) -> str
    # raises shlink.ShlinkError on API failure; we catch & fallback.

Both are imported lazily so this module can still produce the long-URL
output even if the helpers are unimplemented during early development.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR") or (_ENGINE_ROOT / "runtime" / "drafts"))

# Map LinkedIn / Twitter / Reddit etc to their canonical utm_medium values.
# Aligns with PRD §2.3 medium taxonomy.
_DEFAULT_MEDIUM_BY_PLATFORM: dict[str, str] = {
    "twitter": "thread",
    "x": "thread",
    "linkedin": "post",
    "linkedin_post": "post",
    "linkedin_carousel": "carousel",
    "reddit": "post",
    "tiktok": "shorts",
    "youtube": "longvideo",
    "youtube_shorts": "shorts",
    "shorts": "shorts",
    "farcaster": "thread",
    "telegram": "post",
    "mailchimp": "edm",
    "listmonk": "edm",
    "dashboard": "dashboard_card",
}


def _resolve_medium(platform: str) -> str:
    """Map a platform string to its canonical ``utm_medium`` value."""
    return _DEFAULT_MEDIUM_BY_PLATFORM.get(platform.lower(), platform.lower())


def _slug_safe(value: str) -> str:
    """Lowercased / hyphenated / alnum-only slug for shlink ``custom_slug``."""
    v = value.strip().lower()
    v = re.sub(r"[^a-z0-9]+", "-", v)
    v = re.sub(r"-+", "-", v).strip("-")
    return v or "x"


def _piece_id_short(piece_id: str) -> str:
    """Compact stable hash so shlink slugs stay short and collision-resistant."""
    slug = _slug_safe(piece_id)
    if len(slug) <= 24:
        return slug
    digest = hashlib.sha1(piece_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:16]}-{digest}"


def generate_utm(
    *,
    piece_id: str,
    target_url: str,
    platforms: list[str],
    accounts: list[str],
    hook_type: str,
    output_path: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Build UTM links for every ``(platform, account)`` combination.

    Returns a dict keyed by ``f"{platform}_{account}"`` with shape::

        {
          "long_url":  "https://taskon.xyz/...?utm_source=...",
          "short_url": "https://l.taskon.xyz/abc",
          "utm_query": "?utm_source=...&utm_medium=..."
        }

    Args:
        piece_id: Used as ``utm_campaign`` (lower-cased per PRD).
        target_url: Destination URL (e.g. ``https://taskon.xyz/benchmark-report``).
        platforms: List of platform tokens. Mapped to utm_medium via the
            internal taxonomy.
        accounts: List of account handles → ``utm_content`` values.
        hook_type: ``utm_term``.
        output_path: Where to write the JSON. Default
            ``$DRAFTS_DIR/<piece_id>/utm_links.json``.

    Side effects:
        * Calls ``lib.shlink.shorten`` once per combination. Failures are
          logged at WARNING and the long URL is used as a fallback.
        * Writes the JSON file. Parent dirs are created as needed.
    """
    # Lazy import — these modules may not exist yet during early dev.
    try:
        from lib.utm import UTMParams, append_utm, build_utm
    except ImportError as exc:  # pragma: no cover - dev-time only
        logger.error("lib.utm not available (%s); UTM generator cannot run", exc)
        raise

    try:
        from lib.shlink import shlink  # type: ignore[attr-defined]
    except ImportError:
        shlink = None  # type: ignore[assignment]
        logger.warning("lib.shlink unavailable; will skip shortening and use long URLs")

    campaign = piece_id.lower()
    pid_short = _piece_id_short(piece_id)

    out: dict[str, dict[str, str]] = {}
    idx = 0
    for platform in platforms:
        medium = _resolve_medium(platform)
        for account in accounts:
            idx += 1
            params = UTMParams(
                source=platform.lower(),
                medium=medium,
                campaign=campaign,
                content=account.lower(),
                term=hook_type.lower(),
            )
            utm_query = build_utm(params)
            long_url = append_utm(target_url, params)

            short_url = long_url
            if shlink is not None:
                slug = f"{pid_short}-{idx}"
                try:
                    short_url = shlink.shorten(long_url, custom_slug=slug)
                except Exception as exc:
                    # Spec: log WARNING + fall back to long URL. Do not raise.
                    logger.warning(
                        "shlink.shorten failed (piece=%s slug=%s): %s; using long URL",
                        piece_id,
                        slug,
                        exc,
                    )

            key = f"{platform}_{account}"
            out[key] = {
                "long_url": long_url,
                "short_url": short_url,
                "utm_query": utm_query,
            }

    if output_path is None:
        output_path = _DRAFTS_DIR / piece_id / "utm_links.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "utm_generator wrote %s entries to %s (piece=%s)", len(out), output_path, piece_id
    )
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaskOn UTM Generator (PRD §2.3)")
    p.add_argument("--piece-id", required=True)
    p.add_argument("--target-url", required=True)
    p.add_argument(
        "--platforms",
        required=True,
        help="Comma-separated platform tokens, e.g. twitter,linkedin",
    )
    p.add_argument(
        "--accounts",
        required=True,
        help="Comma-separated account tokens, e.g. donald_en,taskon_official",
    )
    p.add_argument("--hook-type", required=True)
    p.add_argument("--out", default=None, help="override output path")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        out = generate_utm(
            piece_id=args.piece_id,
            target_url=args.target_url,
            platforms=_split_csv(args.platforms),
            accounts=_split_csv(args.accounts),
            hook_type=args.hook_type,
            output_path=Path(args.out) if args.out else None,
        )
    except Exception:
        logger.exception("utm_generator failed")
        return 1
    logger.info("utm_generator OK · %s combinations", len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
