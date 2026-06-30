"""Pipeline stage flags — single source of truth for the optional stages.

WHY THIS EXISTS
---------------
The content pipeline has stages that a piece may want to skip:

  * ``video`` — step 7 ``jobs.mpt_runner`` (renders ``shorts_60s.mp4``) and the
    downstream video-platform publishing (yt_shorts / yt_long / tiktok).
  * ``cta``   — step 8 ``jobs.utm_generator`` (writes ``utm_links.json`` + shlink
    short links) and the CTA-URL injection into post content.

Three more stages were added 2026-06-30 for the visual / video-quality line:

  * ``visual``   — ``jobs.visual_runner`` (renders x_hero / carousel / yt_thumb
    images from the draft, no GPU). Default ON.
  * ``avatar``   — ``jobs.avatar_runner`` (digital-human talking video, needs a
    cloud GPU worker). Default OFF — placeholder this phase.
  * ``longform`` — long-form video (LongCat-Video etc., GPU). Default OFF.

Each is an independent boolean. Any combination may be turned off. This module
resolves the *effective* value for a piece with a clear precedence:

    1. runtime override (the kou-ling — CLI flag / job payload)  <- highest
    2. per-piece persistent: selection_card.yaml ``stages:``
    3. global switch:       config.yaml ``pipeline_stages:``
    4. default: STAGE_DEFAULTS (video/cta/visual=True, avatar/longform=False)

Truthy/falsey strings accepted (case-insensitive):
    on/off, true/false, yes/no, y/n, 1/0, enable/disable

USAGE
-----

    from lib.pipeline_flags import resolve_stages

    stages = resolve_stages("2026W24-thread01")
    if not stages.video:  ...     # skip MPT render + video platforms
    if not stages.cta:    ...     # publish link-free, no UTM
    if not stages.visual: ...     # skip image render (x_hero / carousel / thumb)

    # runtime override:
    stages = resolve_stages(pid, video_override="off", visual_override="off")
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Stage names this module knows about. Keep the surface narrow on purpose.
STAGE_NAMES = ("video", "cta", "visual", "avatar", "longform")

# Per-stage default applied when nothing in the precedence chain sets a value.
# video/cta/visual default ON (video/cta preserve prior behaviour; visual is a
# no-GPU capability we want on by default). avatar/longform default OFF -- they
# require a GPU worker and are placeholders reserved for the next phase.
STAGE_DEFAULTS: dict[str, bool] = {
    "video": True,
    "cta": True,
    "visual": True,
    "avatar": False,
    "longform": False,
}

_TRUE_TOKENS = {"on", "true", "yes", "y", "1", "enable", "enabled"}
_FALSE_TOKENS = {"off", "false", "no", "n", "0", "disable", "disabled"}


@dataclass(frozen=True)
class Stages:
    """Resolved stage flags for one piece (True = stage runs)."""

    video: bool = True
    cta: bool = True
    visual: bool = True
    avatar: bool = False
    longform: bool = False
    # Where each value came from, for log/audit ("default"/"global"/"card"/"override").
    video_source: str = "default"
    cta_source: str = "default"
    visual_source: str = "default"
    avatar_source: str = "default"
    longform_source: str = "default"

    def as_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in STAGE_NAMES}

    def __getitem__(self, key: str) -> bool:  # allow stages["video"]
        return self.as_dict()[key]

    def summary(self) -> str:
        return " ".join(
            f"{name}={'on' if getattr(self, name) else 'OFF'}({getattr(self, f'{name}_source')})"
            for name in STAGE_NAMES
        )


def parse_flag(value: Any) -> bool | None:
    """Parse a flag value into a bool. Returns None when unset / unrecognised.

    Accepts native bool/int and the truthy/falsey string tokens above. Anything
    else (None, empty string, garbage) returns None so callers fall through to
    the next precedence level instead of guessing.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        if token == "":
            return None
        logger.warning("pipeline_flags.parse_flag: unrecognised token %r -- ignoring", value)
        return None
    logger.warning("pipeline_flags.parse_flag: unsupported type %s -- ignoring", type(value).__name__)
    return None


def _engine_root() -> Path:
    env_root = os.environ.get("ENGINE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _drafts_dir() -> Path:
    env_drafts = os.environ.get("DRAFTS_DIR")
    if env_drafts:
        return Path(env_drafts).expanduser().resolve()
    return _engine_root() / "runtime" / "drafts"


def _load_global_stages(cfg: dict[str, Any] | None) -> dict[str, bool | None]:
    """Read config.yaml ``pipeline_stages:``. Missing keys -> None (use default)."""
    out: dict[str, bool | None] = {name: None for name in STAGE_NAMES}
    if cfg is None:
        path = _engine_root() / "config.yaml"
        if not path.is_file():
            return out
        try:
            with path.open("r", encoding="utf-8") as fp:
                cfg = yaml.safe_load(fp) or {}
        except Exception as exc:  # noqa: BLE001 -- never block the pipeline on config read
            logger.warning("pipeline_flags: cannot read config.yaml (%s) -- using defaults", exc)
            return out
    section = (cfg or {}).get("pipeline_stages")
    if isinstance(section, dict):
        for name in STAGE_NAMES:
            out[name] = parse_flag(section.get(name))
    return out


def _load_card_stages(piece_id: str, drafts_dir: Path | None) -> dict[str, bool | None]:
    """Read per-piece selection_card.yaml ``stages:`` block.

    Also tolerates flat ``skip_video: true`` / ``skip_cta: true`` aliases -- a
    skip alias of True means the stage is OFF.
    """
    out: dict[str, bool | None] = {name: None for name in STAGE_NAMES}
    base = drafts_dir or _drafts_dir()
    card_path = base / piece_id / "selection_card.yaml"
    if not card_path.is_file():
        return out
    try:
        with card_path.open("r", encoding="utf-8") as fp:
            card = yaml.safe_load(fp) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline_flags: cannot read %s (%s) -- ignoring card stages", card_path, exc)
        return out
    if not isinstance(card, dict):
        return out
    section = card.get("stages")
    if isinstance(section, dict):
        for name in STAGE_NAMES:
            out[name] = parse_flag(section.get(name))
    # Flat skip_* aliases (skip_video: true -> video OFF). Only applied when the
    # structured stages: block did not already set the value.
    for name in STAGE_NAMES:
        if out[name] is None:
            skip = parse_flag(card.get(f"skip_{name}"))
            if skip is not None:
                out[name] = not skip
    return out


def resolve_stages(
    piece_id: str,
    *,
    cfg: dict[str, Any] | None = None,
    drafts_dir: Path | None = None,
    video_override: Any = None,
    cta_override: Any = None,
    visual_override: Any = None,
    avatar_override: Any = None,
    longform_override: Any = None,
) -> Stages:
    """Resolve effective stage flags for ``piece_id``.

    Precedence (highest first): runtime override -> selection_card.yaml
    ``stages:`` -> config.yaml ``pipeline_stages:`` -> STAGE_DEFAULTS.
    """
    glob = _load_global_stages(cfg)
    card = _load_card_stages(piece_id, drafts_dir)
    overrides = {
        "video": parse_flag(video_override),
        "cta": parse_flag(cta_override),
        "visual": parse_flag(visual_override),
        "avatar": parse_flag(avatar_override),
        "longform": parse_flag(longform_override),
    }

    resolved: dict[str, bool] = {}
    sources: dict[str, str] = {}
    for name in STAGE_NAMES:
        if overrides[name] is not None:
            resolved[name] = overrides[name]
            sources[name] = "override"
        elif card[name] is not None:
            resolved[name] = card[name]
            sources[name] = "card"
        elif glob[name] is not None:
            resolved[name] = glob[name]
            sources[name] = "global"
        else:
            resolved[name] = STAGE_DEFAULTS[name]
            sources[name] = "default"

    return Stages(
        **{name: resolved[name] for name in STAGE_NAMES},
        **{f"{name}_source": sources[name] for name in STAGE_NAMES},
    )


__all__ = ["Stages", "STAGE_NAMES", "STAGE_DEFAULTS", "parse_flag", "resolve_stages"]
