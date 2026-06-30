"""Visual Runner -- config-image engine (no GPU, 2026-06-30).

WHAT THIS DOES
--------------
Mirrors ``jobs/mpt_runner`` in shape (stage-gate -> read draft -> early-exit ->
heartbeat) but renders *images* synchronously on CPU instead of submitting a
remote video job. For one piece it produces, into ``runtime/drafts/<piece_id>/``:

  * ``x_hero.png``       1600x900  -- X thread hero image (from quote/data card)
  * ``yt_thumb.png``     1280x720  -- YouTube thumbnail
  * ``carousel.pdf`` + ``carousel_pNN.png``  1080x1350 -- LinkedIn carousel pages

Inputs (best-effort; missing ones are skipped, never fatal):
  * ``selection_card.yaml``  -> title / tag / key_data_points  (preferred source)
  * ``xthread_final.md``     -> hook fallback for x_hero
  * ``carousel_10pages.md``  -> pages[].title/body for the carousel
  * ``shorts_60s.md``        -> hook for the yt_thumb

Rendering: SVG templates in ``assets/visual_templates/`` with ``{{PLACEHOLDER}}``
tokens, filled here (CJK-aware line wrapping via <tspan>), rasterised by cairosvg.
Pillow assembles the per-page PNGs into a single carousel.pdf.

Idempotency: a sidecar ``visual_render.json`` stores a hash of (source files +
templates). If it matches and all expected outputs exist, the run is skipped
unless ``--force``.

Stage gate: honours the ``visual`` flag (config / selection_card / runtime
override) via ``lib.pipeline_flags.resolve_stages``. visual OFF -> whole job
early-exits (mirrors mpt_runner's video gate).

Hard rules (Prompt_AI系统化编程_v1.md §7): no silent failures -- every branch
logs and returns/alerts; render errors are wrapped with a P-alert, never swallowed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import db  # noqa: E402
from lib.lark import alert  # noqa: E402
from lib.pipeline_flags import resolve_stages  # noqa: E402

try:  # yaml is a hard dep of the engine; guard only so import never explodes
    import yaml  # noqa: E402
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)

JOB_NAME = "visual_runner"

BRAND_DEFAULT = "TaskOn.xyz"

# Output spec: filename -> (template, render_width, render_height)
OUT_X_HERO = "x_hero.png"
OUT_YT_THUMB = "yt_thumb.png"
OUT_CAROUSEL_PDF = "carousel.pdf"


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def _engine_root() -> Path:
    return Path(os.environ.get("ENGINE_ROOT") or Path(__file__).resolve().parent.parent)


def _drafts_dir() -> Path:
    return Path(os.environ.get("DRAFTS_DIR") or (_engine_root() / "runtime" / "drafts"))


def _piece_dir(piece_id: str) -> Path:
    return _drafts_dir() / piece_id


def _templates_dir() -> Path:
    return Path(os.environ.get("VISUAL_TEMPLATES_DIR") or (_engine_root() / "assets" / "visual_templates"))


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

_XML_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"}


def _xesc(text: str) -> str:
    return "".join(_XML_ESCAPE.get(ch, ch) for ch in str(text))


def _display_width(s: str) -> float:
    """Approx visual width in 'CJK units': CJK char = 1.0, ASCII = 0.55."""
    w = 0.0
    for ch in s:
        w += 1.0 if ord(ch) > 0x2E7F else 0.55
    return w


def _wrap_lines(text: str, max_units: float, max_lines: int) -> list[str]:
    """Greedy wrap respecting CJK width. Breaks on spaces for latin, anywhere for CJK."""
    text = " ".join(str(text).split())
    if not text:
        return [""]
    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    i = 0
    tokens: list[str] = []
    # tokenise: keep latin words whole, CJK char-by-char
    buf = ""
    for ch in text:
        if ord(ch) > 0x2E7F or ch == " ":
            if buf:
                tokens.append(buf); buf = ""
            tokens.append(ch)
        else:
            buf += ch
    if buf:
        tokens.append(buf)
    for tok in tokens:
        tw = _display_width(tok)
        if tok == " ":
            if cur_w + tw <= max_units:
                cur += tok; cur_w += tw
            continue
        if cur_w + tw > max_units and cur.strip():
            lines.append(cur.strip())
            cur = tok; cur_w = tw
            if len(lines) >= max_lines - 1:
                # last line: dump remaining tokens, will be truncated below
                pass
        else:
            cur += tok; cur_w += tw
    if cur.strip():
        lines.append(cur.strip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while _display_width(last) > max_units - 1 and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or [""]


def _inject_wrapped(svg: str, key: str, value: str, *, max_lines: int) -> str:
    """Replace a <text ...>{{KEY}}</text> placeholder with wrapped <tspan>s.

    Derives x + font-size from the matched <text> element to size the wrap.
    Falls back to a plain escaped replace if the element shape is unexpected.
    """
    token = "{{" + key + "}}"
    pat = re.compile(
        r'<text\b([^>]*?)>\s*' + re.escape(token) + r'\s*</text>',
        re.DOTALL,
    )
    m = pat.search(svg)
    if not m:
        return svg.replace(token, _xesc(value))
    attrs = m.group(1)
    xm = re.search(r'x="([0-9.]+)"', attrs)
    fm = re.search(r'font-size="([0-9.]+)"', attrs)
    x = xm.group(1) if xm else "0"
    fs = float(fm.group(1)) if fm else 48.0
    # canvas width from viewBox; assume right margin == left margin (x)
    vbm = re.search(r'viewBox="0 0 ([0-9.]+) [0-9.]+"', svg)
    canvas_w = float(vbm.group(1)) if vbm else 1600.0
    usable = canvas_w - 2 * float(x)
    max_units = max(4.0, usable / (fs * 1.05))  # CJK glyph ~= font-size px wide
    lines = _wrap_lines(value, max_units, max_lines)
    tspans = []
    for idx, ln in enumerate(lines):
        dy = "0" if idx == 0 else "1.15em"
        tspans.append(f'<tspan x="{x}" dy="{dy}">{_xesc(ln)}</tspan>')
    return svg[:m.start()] + f'<text{attrs}>' + "".join(tspans) + '</text>' + svg[m.end():]


def _fill(svg: str, mapping: dict[str, str], *, wrap: dict[str, int] | None = None) -> str:
    """Fill {{KEY}} tokens. Keys in ``wrap`` get tspan line-wrapping (value=max_lines)."""
    wrap = wrap or {}
    for key, max_lines in wrap.items():
        if key in mapping:
            svg = _inject_wrapped(svg, key, mapping[key], max_lines=max_lines)
    for key, val in mapping.items():
        if key in wrap:
            continue
        svg = svg.replace("{{" + key + "}}", _xesc(val))
    # blank any leftover placeholders
    svg = re.sub(r"\{\{[A-Z0-9_]+\}\}", "", svg)
    return svg


# --------------------------------------------------------------------------- #
# Source extraction
# --------------------------------------------------------------------------- #


def _load_card(piece_dir: Path) -> dict[str, Any]:
    path = piece_dir / "selection_card.yaml"
    if not path.is_file() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual_runner: cannot parse selection_card.yaml (%s)", exc)
        return {}


def _first_hook_from_thread(piece_dir: Path) -> str:
    path = piece_dir / "xthread_final.md"
    if not path.is_file():
        return ""
    txt = path.read_text(encoding="utf-8")
    # take first non-empty content line after the first 推/hook marker
    m = re.search(r"(?:推\s*1|hook|钩子)[^\n]*\n+(.+)", txt, re.IGNORECASE)
    if m:
        line = m.group(1).strip().lstrip("*> ").strip()
        return line.split("\n")[0].strip()
    return ""


def _tag_from_card(card: dict[str, Any]) -> str:
    for k in ("hook_type", "content_type", "narrative_anchor"):
        v = card.get(k)
        if v:
            return str(v).upper().replace("_", " ")[:24]
    return "TASKON"


def _derive_hero(card: dict[str, Any], piece_dir: Path) -> dict[str, str]:
    title = str(card.get("title") or _first_hook_from_thread(piece_dir) or piece_dir.name)
    kdp = card.get("key_data_points") or []
    subtitle = str(kdp[0]) if isinstance(kdp, list) and kdp else str(card.get("narrative_anchor") or "")
    return {
        "TITLE": title,
        "SUBTITLE": subtitle,
        "TAG": _tag_from_card(card),
        "BRAND": BRAND_DEFAULT,
    }


def _derive_thumb(card: dict[str, Any], piece_dir: Path) -> dict[str, str]:
    hook = ""
    sp = piece_dir / "shorts_60s.md"
    if sp.is_file():
        txt = sp.read_text(encoding="utf-8")
        m = re.search(r"\[[^\]]*0-3s[^\]]*\][^\n]*\n+(.+)", txt)
        if m:
            hook = m.group(1).strip().split("\n")[0].strip()
    title = hook or str(card.get("title") or piece_dir.name)
    return {
        "TITLE": title,
        "SUBTITLE": str(card.get("title") or ""),
        "TAG": _tag_from_card(card),
        "BRAND": BRAND_DEFAULT,
    }


def _load_carousel_pages(piece_dir: Path) -> list[dict[str, str]]:
    path = piece_dir / "carousel_10pages.md"
    if not path.is_file() or yaml is None:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual_runner: cannot parse carousel_10pages.md (%s)", exc)
        return []
    pages = data.get("pages") if isinstance(data, dict) else None
    out: list[dict[str, str]] = []
    if isinstance(pages, list):
        for pg in pages:
            if isinstance(pg, dict):
                out.append({"title": str(pg.get("title") or ""), "body": str(pg.get("body") or "")})
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _read_template(name: str) -> str:
    path = _templates_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"visual template missing: {path}")
    return path.read_text(encoding="utf-8")


def _svg_to_png(svg: str, out_path: Path, *, width: int, height: int) -> None:
    import cairosvg  # lazy: only needed at render time (kept out of test import)
    cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        write_to=str(out_path),
        output_width=width,
        output_height=height,
    )


def _pngs_to_pdf(png_paths: list[Path], pdf_path: Path) -> bool:
    """Assemble PNG pages into one PDF. Returns True on success.

    Prefers img2pdf (lossless, needs no JPEG codec); falls back to Pillow.
    Never raises -- the carousel PDF is best-effort; the per-page PNGs always
    remain on disk even if PDF assembly is unavailable.
    """
    if not png_paths:
        return False
    try:
        import img2pdf  # lazy
        with open(pdf_path, "wb") as fp:
            fp.write(img2pdf.convert([str(p) for p in png_paths]))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual_runner: img2pdf failed (%s) - trying Pillow", exc)
    try:
        from PIL import Image  # lazy fallback
        imgs = [Image.open(p).convert("RGB") for p in png_paths]
        imgs[0].save(pdf_path, save_all=True, append_images=imgs[1:])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual_runner: Pillow PDF assembly failed (%s) - keeping PNGs only", exc)
        return False


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

_SOURCE_FILES = ("selection_card.yaml", "xthread_final.md", "carousel_10pages.md", "shorts_60s.md")


def _source_hash(piece_dir: Path) -> str:
    h = hashlib.sha256()
    for name in _SOURCE_FILES:
        p = piece_dir / name
        h.update(name.encode())
        h.update(b"\0")
        if p.is_file():
            h.update(p.read_bytes())
        h.update(b"\0")
    # include template bytes so a template change re-renders
    for tpl in sorted(_templates_dir().glob("*.svg")):
        h.update(tpl.read_bytes())
    return h.hexdigest()


def _expected_outputs(piece_dir: Path, pages: list[dict[str, str]]) -> list[Path]:
    outs = [piece_dir / OUT_X_HERO, piece_dir / OUT_YT_THUMB]
    if pages:
        outs.append(piece_dir / OUT_CAROUSEL_PDF)
    return outs


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run(
    piece_id: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    visual: Any = None,
) -> dict[str, Any]:
    """Render visual assets for one piece. Returns a summary dict.

    status: 'rendered' | 'skipped' | 'unchanged' | 'dry_run' | 'failed'
    """
    started_at = time.monotonic()

    # ---- Stage gate ---- #
    stages = resolve_stages(piece_id, visual_override=visual)
    if not stages.visual:
        logger.info("visual stage disabled for piece=%s (%s) - skipping", piece_id, stages.summary())
        return {"piece_id": piece_id, "status": "skipped", "reason": "visual_stage_disabled"}

    piece_dir = _piece_dir(piece_id)
    if not piece_dir.is_dir():
        msg = f"piece dir missing: {piece_dir}"
        logger.warning(msg)
        alert("P2", f"visual_runner: {msg}", {"piece_id": piece_id})
        _record_heartbeat("warning", started_at, error_message=msg, rows=0)
        return {"piece_id": piece_id, "status": "skipped", "reason": "no_piece_dir"}

    card = _load_card(piece_dir)
    pages = _load_carousel_pages(piece_dir)
    has_any_source = card or (piece_dir / "xthread_final.md").is_file() or pages
    if not has_any_source:
        msg = f"no usable source files in {piece_dir}"
        logger.warning(msg)
        _record_heartbeat("warning", started_at, error_message=msg, rows=0)
        return {"piece_id": piece_id, "status": "skipped", "reason": "no_source"}

    expected = _expected_outputs(piece_dir, pages)
    cur_hash = _source_hash(piece_dir)
    manifest_path = piece_dir / "visual_render.json"

    # ---- Idempotency: outputs present + hash matches -> unchanged ---- #
    if not force and manifest_path.is_file() and all(p.is_file() for p in expected):
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = {}
        if prev.get("source_hash") == cur_hash:
            logger.info("visual_runner: piece=%s unchanged (hash match) - skip render", piece_id)
            _record_heartbeat("ok", started_at, error_message=None, rows=0)
            return {"piece_id": piece_id, "status": "unchanged", "outputs": [p.name for p in expected]}

    hero = _derive_hero(card, piece_dir)
    thumb = _derive_thumb(card, piece_dir)

    # ---- Dry run: report plan, render nothing ---- #
    if dry_run:
        logger.info("DRY-RUN piece=%s hero.title=%r pages=%d", piece_id, hero["TITLE"][:40], len(pages))
        return {
            "piece_id": piece_id,
            "status": "dry_run",
            "would_render": [p.name for p in expected],
            "hero_title": hero["TITLE"],
            "carousel_pages": len(pages),
        }

    written: list[str] = []
    try:
        # x_hero (quote_landscape, 1600x900)
        svg = _fill(_read_template("quote_landscape.svg"), hero, wrap={"TITLE": 3, "SUBTITLE": 2})
        _svg_to_png(svg, piece_dir / OUT_X_HERO, width=1600, height=900)
        written.append(OUT_X_HERO)

        # yt_thumb (quote_landscape scaled to 1280x720)
        svg = _fill(_read_template("quote_landscape.svg"), thumb, wrap={"TITLE": 3, "SUBTITLE": 1})
        _svg_to_png(svg, piece_dir / OUT_YT_THUMB, width=1280, height=720)
        written.append(OUT_YT_THUMB)

        # carousel (quote_portrait, 1080x1350 per page -> PDF + per-page PNG)
        page_pngs: list[Path] = []
        if pages:
            tpl = _read_template("quote_portrait.svg")
            total = len(pages)
            for i, pg in enumerate(pages, 1):
                mapping = {
                    "TITLE": pg["title"],
                    "SUBTITLE": pg["body"],
                    "TAG": _tag_from_card(card),
                    "PAGE": f"{i}/{total}",
                    "BRAND": BRAND_DEFAULT,
                }
                page_svg = _fill(tpl, mapping, wrap={"TITLE": 3, "SUBTITLE": 5})
                png = piece_dir / f"carousel_p{i:02d}.png"
                _svg_to_png(page_svg, png, width=1080, height=1350)
                page_pngs.append(png)
                written.append(png.name)
            if _pngs_to_pdf(page_pngs, piece_dir / OUT_CAROUSEL_PDF):
                written.append(OUT_CAROUSEL_PDF)
            else:
                logger.warning("visual_runner: carousel.pdf not produced for %s (per-page PNGs kept)", piece_id)
    except Exception as exc:  # noqa: BLE001 -- render failure must alert, never silent
        logger.exception("visual_runner render failed for piece=%s", piece_id)
        alert("P1", f"visual_runner render failed for {piece_id}", {"error": str(exc)[:300]})
        _record_heartbeat("failed", started_at, error_message=str(exc)[:300], rows=len(written))
        return {"piece_id": piece_id, "status": "failed", "reason": "render_error", "error": str(exc), "partial": written}

    manifest_path.write_text(
        json.dumps({"source_hash": cur_hash, "outputs": written, "rendered_at": int(time.time())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    duration_s = int(time.monotonic() - started_at)
    _record_heartbeat("ok", started_at, error_message=None, rows=len(written))
    logger.info("visual_runner rendered piece=%s outputs=%d duration=%ds", piece_id, len(written), duration_s)
    return {"piece_id": piece_id, "status": "rendered", "outputs": written, "duration_seconds": duration_s}


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #


def _record_heartbeat(status: str, started_at: float, *, error_message: str | None, rows: int) -> None:
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
        prog="python -m jobs.visual_runner",
        description="Render x_hero / yt_thumb / carousel images for a piece (CPU, no GPU).",
    )
    p.add_argument("--piece-id", required=True, help="folder name under runtime/drafts/")
    p.add_argument("--dry-run", action="store_true", help="report plan; render nothing")
    p.add_argument("--force", action="store_true", help="re-render even if hash unchanged")
    p.add_argument("--visual", default=None, help="override visual stage (on/off). off = skip render")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )
    summary = run(args.piece_id, dry_run=args.dry_run, force=args.force, visual=args.visual)
    return 0 if summary.get("status") in ("rendered", "unchanged", "skipped", "dry_run") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
