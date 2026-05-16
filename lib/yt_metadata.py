"""YouTube upload metadata · Cowork-supplied YAML primary + engine LLM fallback.

The Postiz YouTube integration needs more than just `content` — it requires
title / description / privacy / tags / category_id / not_made_for_kids
inside the per-platform `settings` blob. Engine does not draft this content;
that's Cowork's job (C §原则1). But until Cowork's mpt-video / shorts skill
is updated to also emit metadata, we provide a 3-tier fallback:

1. **Cowork-supplied** — ``drafts/<piece_id>/yt_metadata.yaml`` is present
   and validates → use as-is, mark ``source='cowork'``.
2. **LLM auto-derive** — engine reads ``selection_card.yaml`` +
   ``shorts_60s.md`` + ``utm_links.json`` and asks the LLM to produce the
   metadata. Output is persisted to ``yt_metadata_auto.yaml`` so Donald can
   inspect before the cron actually fires. Marked ``source='llm_auto'``.
3. **Hard template fallback** — LLM raised ``LLMClientError`` (provider
   exhausted) → assemble safe defaults from selection_card + script first
   line. Persisted to ``yt_metadata_fallback.yaml``. P2 Lark alert fires so
   on-call notices the LLM is down. Marked ``source='fallback'``.

Public surface:

    @dataclass
    class YouTubeMetadata:
        title, description, privacy, tags, category_id, not_made_for_kids,
        thumbnail_path, source

    load_or_derive(piece_id, piece_dir, *, default_privacy='public') -> YouTubeMetadata

    metadata.to_postiz_settings() -> dict ready for postiz.create_post(extra_settings=...)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv

load_dotenv(override=False)

from lib.lark import alert
from lib.llm_client import LLMClientError, llm

logger = logging.getLogger(__name__)


_ENGINE_ROOT = Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)
_PROMPT_PATH = _ENGINE_ROOT / "config" / "prompts" / "yt_metadata.txt"

# YouTube hard limits (verified against YouTube Data API v3 docs).
TITLE_MAX = 95              # YT cap 100, we leave 5 chars headroom
DESCRIPTION_MAX = 5000
TAGS_MAX_COUNT = 8

# 14 AI-slop phrases ban list — mirror of voice_checker (kept here verbatim
# to avoid an import cycle and so this module can run standalone).
_BANNED_PHRASES: tuple[str, ...] = (
    "全方位", "革命性", "颠覆", "赋能", "闭环", "抓手", "价值赋能",
    "显著", "全栈", "一站式", "无缝",
    "dive into", "let's explore", "综上所述", "在当今快速发展的",
)


# --------------------------------------------------------------------------- #
# Dataclass
# --------------------------------------------------------------------------- #


@dataclass
class YouTubeMetadata:
    """Resolved YouTube upload metadata for one piece.

    All consumers should obtain this via :func:`load_or_derive` rather than
    constructing it directly, so the 3-tier fallback path runs.
    """
    title: str
    description: str
    privacy: Literal["public", "unlisted", "private"] = "public"
    tags: list[str] = field(default_factory=list)
    category_id: int = 22
    not_made_for_kids: bool = True
    thumbnail_path: str | None = None
    source: Literal["cowork", "llm_auto", "fallback"] = "cowork"

    def to_postiz_settings(self) -> dict[str, Any]:
        """Translate to the Postiz YouTube integration's ``settings`` shape.

        Field names follow gitroomhq/postiz-app's YouTube provider:
            * title           — video title
            * description     — video description (NOT body!)
            * type            — privacy: public|unlisted|private
            * tags            — list[{value: str, label: str}]; Postiz's
              @ValidateNested rejects flat strings AND objects missing either
              key (verified 2026-05-16 S9.4 HTTP 400 × 2). The tag-input
              DTO requires both fields; the YouTube provider then maps
              them to YT API's flat string tags internally.
            * thumbnail       — URL or path Postiz can read
        """
        settings: dict[str, Any] = {
            "title": self.title,
            "description": self.description,
            "type": self.privacy,
            "tags": [{"value": t, "label": t} for t in self.tags],
            "category": self.category_id,
            "notMadeForKids": self.not_made_for_kids,
        }
        if self.thumbnail_path:
            settings["thumbnail"] = self.thumbnail_path
        return settings

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # YAML doesn't render None nicely; drop empty thumbnail.
        if d.get("thumbnail_path") is None:
            d.pop("thumbnail_path", None)
        return d


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class MetadataValidationError(ValueError):
    """Raised when Cowork-supplied / LLM output fails strict validation."""


def _validate(raw: dict[str, Any]) -> None:
    """Strict validation — raises MetadataValidationError on any rule miss.

    Server-side enforcement mirrors the prompt's hard rules so Cowork output
    that breaks them is rejected the same as LLM output.
    """
    title = (raw.get("title") or "").strip()
    if not title:
        raise MetadataValidationError("title is required and non-empty")
    if len(title) > TITLE_MAX:
        raise MetadataValidationError(f"title length {len(title)} > {TITLE_MAX}")

    description = (raw.get("description") or "").strip()
    if not description:
        raise MetadataValidationError("description is required and non-empty")
    if len(description) > DESCRIPTION_MAX:
        raise MetadataValidationError(
            f"description length {len(description)} > {DESCRIPTION_MAX}"
        )

    privacy = (raw.get("privacy") or "public").strip().lower()
    if privacy not in {"public", "unlisted", "private"}:
        raise MetadataValidationError(
            f"privacy must be public|unlisted|private; got {privacy!r}"
        )

    tags = raw.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise MetadataValidationError("tags must be list[str]")
    if len(tags) > TAGS_MAX_COUNT:
        raise MetadataValidationError(f"tags count {len(tags)} > {TAGS_MAX_COUNT}")

    # Banned-phrase check on title + description (server-side guardrail).
    lower_blob = f"{title}\n{description}".lower()
    for phrase in _BANNED_PHRASES:
        if phrase.lower() in lower_blob:
            raise MetadataValidationError(
                f"banned phrase {phrase!r} appears in title or description"
            )


def _coerce(raw: dict[str, Any], default_privacy: str, source: str) -> YouTubeMetadata:
    """Apply defaults + cast types. Assumes raw already passed _validate."""
    return YouTubeMetadata(
        title=str(raw["title"]).strip(),
        description=str(raw["description"]).strip(),
        privacy=str(raw.get("privacy") or default_privacy).strip().lower(),  # type: ignore[arg-type]
        tags=list(raw.get("tags") or []),
        category_id=int(raw.get("category_id") or 22),
        not_made_for_kids=bool(raw.get("not_made_for_kids", True)),
        thumbnail_path=raw.get("thumbnail_path") or None,
        source=source,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Tier 1: Cowork-supplied YAML
# --------------------------------------------------------------------------- #


def _try_load_cowork(piece_dir: Path) -> YouTubeMetadata | None:
    """Return metadata from yt_metadata.yaml or None when absent/invalid.

    A present-but-invalid file is logged WARNING and treated as missing — we
    fall through to LLM derivation rather than failing the publish. The
    invalid file is NOT auto-fixed; Donald sees both files in drafts/ and
    decides which to keep.
    """
    path = piece_dir / "yt_metadata.yaml"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp) or {}
        if not isinstance(raw, dict):
            raise MetadataValidationError(f"yt_metadata.yaml must be a mapping; got {type(raw).__name__}")
        _validate(raw)
        meta = _coerce(raw, default_privacy="public", source="cowork")
        logger.info("yt_metadata cowork-supplied OK for %s: title=%r", piece_dir.name, meta.title)
        return meta
    except (MetadataValidationError, yaml.YAMLError) as exc:
        logger.warning(
            "yt_metadata cowork-supplied INVALID at %s (%s); falling back to LLM derivation",
            path, exc,
        )
        return None


# --------------------------------------------------------------------------- #
# Tier 2: engine LLM auto-derive
# --------------------------------------------------------------------------- #


def _read_selection_card(piece_dir: Path) -> dict[str, Any]:
    """Best-effort read of selection_card.yaml. Returns {} when absent."""
    path = piece_dir / "selection_card.yaml"
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fp:
            doc = yaml.safe_load(fp) or {}
        return doc if isinstance(doc, dict) else {}
    except yaml.YAMLError as exc:
        logger.warning("selection_card.yaml parse failed at %s: %s", path, exc)
        return {}


def _read_script(piece_dir: Path) -> str:
    """Read shorts_60s.md text. Empty string when absent."""
    path = piece_dir / "shorts_60s.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _read_utm_link(piece_dir: Path, platform_key: str = "youtube") -> str:
    """Pull the matching utm short/long URL for YouTube from utm_links.json."""
    path = piece_dir / "utm_links.json"
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""
    blob = data.get(platform_key)
    if isinstance(blob, dict):
        # Common shapes: {"short_url": "...", "long_url": "..."} OR
        #                {"<account>": {"short_url": "...", ...}}
        if blob.get("short_url"):
            return str(blob["short_url"])
        if blob.get("long_url"):
            return str(blob["long_url"])
        for sub in blob.values():
            if isinstance(sub, dict) and (sub.get("short_url") or sub.get("long_url")):
                return str(sub.get("short_url") or sub["long_url"])
    return ""


def _try_derive_via_llm(
    piece_id: str,
    piece_dir: Path,
    *,
    channel_name: str = "TaskOn",
) -> YouTubeMetadata | None:
    """Ask the LLM to produce metadata. Returns None on LLMClientError."""
    if not _PROMPT_PATH.is_file():
        logger.error("yt_metadata prompt missing at %s", _PROMPT_PATH)
        return None
    selection_card = _read_selection_card(piece_dir)
    script = _read_script(piece_dir)
    utm_link = _read_utm_link(piece_dir, "youtube")

    if not script:
        # Without the script there's nothing meaningful to derive from.
        # Fall through to hard fallback.
        logger.warning("yt_metadata: no shorts_60s.md for %s; skipping LLM derivation", piece_id)
        return None

    user_payload = {
        "piece_id": piece_id,
        "selection_card": selection_card,
        "script": script[:3000],  # cap to keep prompt token-bounded
        "utm_link": utm_link,
        "channel_name": channel_name,
    }
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    try:
        raw = llm.complete_json(
            system=system_prompt,
            # default=str so YAML-parsed datetime.date inside selection_card
            # doesn't crash json.dumps (same root cause as the bootstrap path
            # in lib/db._normalize_card · 2026-05-15).
            user=json.dumps(user_payload, ensure_ascii=False, indent=2, default=str),
            schema_hint=(
                '{title: str (≤95 chars), description: str (≤5000), '
                'privacy: "public"|"unlisted"|"private", tags: list[str ≤8], '
                'category_id: int, not_made_for_kids: bool}'
            ),
        )
    except LLMClientError as exc:
        logger.exception("yt_metadata LLM derivation failed for %s: %s", piece_id, exc)
        return None

    try:
        _validate(raw)
    except MetadataValidationError as exc:
        # LLM produced something that breaks the rules. Don't trust it —
        # fall through to hard fallback so the publish doesn't ship slop.
        logger.warning(
            "yt_metadata LLM output rejected for %s (%s); falling back to hard template",
            piece_id, exc,
        )
        return None

    meta = _coerce(raw, default_privacy="public", source="llm_auto")
    logger.info("yt_metadata LLM-derived for %s: title=%r tags=%d", piece_id, meta.title, len(meta.tags))
    return meta


# --------------------------------------------------------------------------- #
# Tier 3: hard template fallback
# --------------------------------------------------------------------------- #


def _hard_template_fallback(
    piece_id: str,
    piece_dir: Path,
    *,
    default_privacy: str = "public",
) -> YouTubeMetadata:
    """Last-resort metadata when LLM is unreachable / output rejected.

    Title  := first non-empty line of script, truncated to TITLE_MAX-7, suffixed " | TaskOn"
              (if script empty: piece_id humanized)
    Desc   := full script + blank line + UTM link line + blank line + footer
    Tags   := from selection_card.hook_type / narrative_anchor when present, else []
    The result is intentionally bland — Donald should see fallback content
    in the dashboard and decide whether to re-render once LLM is back.
    """
    selection_card = _read_selection_card(piece_dir)
    script = _read_script(piece_dir)
    utm_link = _read_utm_link(piece_dir, "youtube")

    # Title
    first_line = next((ln.strip() for ln in script.splitlines() if ln.strip()), "")
    # Drop leading time-code like "0:00 " / "0:10 "
    if first_line[:5].count(":") == 1 and first_line[:5].replace(":", "").isdigit():
        first_line = first_line.split(" ", 1)[1] if " " in first_line else first_line
    if not first_line:
        first_line = piece_id.replace("-", " ").replace("_", " ").title()
    suffix = " | TaskOn"
    max_first = TITLE_MAX - len(suffix)
    if len(first_line) > max_first:
        first_line = first_line[: max_first - 1].rstrip() + "…"
    title = (first_line + suffix).strip()

    # Description
    desc_parts: list[str] = []
    if script:
        desc_parts.append(script[: DESCRIPTION_MAX - 500])  # leave room for footer
    if utm_link:
        desc_parts.append("")
        desc_parts.append(f"🔗 {utm_link}")
    desc_parts.append("")
    desc_parts.append("TaskOn · Web3 Growth Platform")
    desc_parts.append("Subscribe for honest takes on Quest, Sybil, and growth.")
    description = "\n".join(desc_parts).strip() or piece_id

    # Tags
    raw_tags: list[str] = []
    for key in ("hook_type", "narrative_anchor"):
        v = selection_card.get(key)
        if isinstance(v, str) and v.strip():
            raw_tags.append(v.strip().lower().replace(" ", "-").replace("_", "-"))
    raw_tags.extend(["web3", "taskon"])
    # De-dupe preserving order, cap to TAGS_MAX_COUNT.
    seen: set[str] = set()
    tags: list[str] = []
    for t in raw_tags:
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
            if len(tags) >= TAGS_MAX_COUNT:
                break

    return YouTubeMetadata(
        title=title,
        description=description,
        privacy=default_privacy,  # type: ignore[arg-type]
        tags=tags,
        category_id=22,
        not_made_for_kids=True,
        thumbnail_path=None,
        source="fallback",
    )


# --------------------------------------------------------------------------- #
# Public orchestrator
# --------------------------------------------------------------------------- #


def _persist_audit_yaml(piece_dir: Path, meta: YouTubeMetadata) -> Path | None:
    """Write the resolved metadata to disk for Donald to inspect.

    File name reflects the source:
        cowork    → no audit file (Cowork already owns the truth)
        llm_auto  → yt_metadata_auto.yaml
        fallback  → yt_metadata_fallback.yaml
    """
    if meta.source == "cowork":
        return None
    suffix = "auto" if meta.source == "llm_auto" else "fallback"
    out = piece_dir / f"yt_metadata_{suffix}.yaml"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fp:
            yaml.safe_dump(meta.to_dict(), fp, allow_unicode=True, sort_keys=False)
        logger.info("yt_metadata audit YAML written: %s", out)
        return out
    except OSError:
        logger.exception("failed to persist yt_metadata audit YAML at %s", out)
        return None


def load_or_derive(
    piece_id: str,
    piece_dir: Path,
    *,
    default_privacy: Literal["public", "unlisted", "private"] = "public",
    channel_name: str = "TaskOn",
) -> YouTubeMetadata:
    """Resolve YouTube metadata for ``piece_id`` via the 3-tier fallback chain.

    Tier order: Cowork yaml → engine LLM → hard template.
    Side effect: writes ``yt_metadata_auto.yaml`` (tier 2) or
    ``yt_metadata_fallback.yaml`` (tier 3) so a human can inspect.

    The function never raises — even the hard template path produces
    *something* shippable. If you need to gate publish on quality, run the
    voice_checker pass over ``meta.title + meta.description`` separately.
    """
    piece_dir = Path(piece_dir)

    cowork = _try_load_cowork(piece_dir)
    if cowork is not None:
        return cowork

    llm_derived = _try_derive_via_llm(piece_id, piece_dir, channel_name=channel_name)
    if llm_derived is not None:
        _persist_audit_yaml(piece_dir, llm_derived)
        return llm_derived

    fallback = _hard_template_fallback(piece_id, piece_dir, default_privacy=default_privacy)
    _persist_audit_yaml(piece_dir, fallback)
    try:
        alert(
            "P2",
            f"yt_metadata: fell back to hard template for {piece_id} "
            "(LLM unavailable or output rejected)",
            {"piece_id": piece_id, "title": fallback.title},
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to emit P2 alert from yt_metadata fallback")
    return fallback


__all__ = [
    "YouTubeMetadata",
    "MetadataValidationError",
    "load_or_derive",
    "TITLE_MAX",
    "DESCRIPTION_MAX",
    "TAGS_MAX_COUNT",
]
