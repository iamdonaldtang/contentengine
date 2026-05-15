"""UTM 5-segment naming helper · source / medium / campaign / content / term.

The TaskOn marketing engine standardises every outbound link with a 5-segment
UTM scheme. This module is the *only* place that should encode / decode UTM
parameters — callers should never hand-build query strings.

Public surface::

    @dataclass(frozen=True)
    class UTMParams: source, medium, campaign, content, term

    build_utm(p) -> "?utm_source=...&..."
    append_utm(url, p) -> "https://...?...&utm_source=..."
    parse_utm(url_or_query) -> UTMParams | None

Normalisation rule (Prompt §UTM): every segment lowercase, alphanumeric +
underscore only. Spaces and hyphens become underscores; anything else is a
``ValueError``. ``piece_id`` style strings like ``2026W19-thread01`` normalise
to ``2026w19_thread01`` without loss.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# After normalisation a segment must consist solely of [a-z0-9_].
_ALLOWED_CHARS = re.compile(r"^[a-z0-9_]+$")
# Pre-normalisation: spaces and hyphens are tolerated and rewritten.
_NORMALIZE_REPLACE = re.compile(r"[\s\-]+")


def _normalize(s: str) -> str:
    """Lowercase + replace spaces/hyphens with underscores; reject anything else.

    Args:
        s: The raw segment value (must be a non-empty string).

    Returns:
        A canonical lowercase, underscore-separated, alphanumeric+underscore
        string suitable for use in a UTM segment.

    Raises:
        ValueError: If ``s`` is empty, not a string, or contains disallowed
            characters after normalisation.
    """
    if not isinstance(s, str):
        raise ValueError(f"utm segment must be a string, got {type(s).__name__}")
    cleaned = s.strip().lower()
    if not cleaned:
        raise ValueError("utm segment is empty after stripping")
    cleaned = _NORMALIZE_REPLACE.sub("_", cleaned)
    # Collapse runs of underscores introduced by the replacement.
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or not _ALLOWED_CHARS.match(cleaned):
        raise ValueError(
            f"utm segment contains disallowed characters after normalisation: "
            f"input={s!r} cleaned={cleaned!r}"
        )
    return cleaned


@dataclass(frozen=True)
class UTMParams:
    """Immutable 5-segment UTM parameter bundle.

    Fields are stored verbatim (caller-supplied raw strings). Normalisation
    happens at serialise time via :func:`build_utm` / :func:`append_utm`, so
    constructing a ``UTMParams`` directly is cheap and doesn't validate. Use
    :meth:`normalized` to get a copy with all segments canonicalised — or to
    fail fast on invalid input.
    """

    source: str  # e.g. twitter|linkedin|reddit|tiktok|youtube|farcaster|telegram|listmonk|dashboard|docs
    medium: str  # e.g. thread|post|carousel|shorts|longvideo|edm|dashboard_card
    campaign: str  # piece_id lowercased, e.g. "perps_dex_lies_v1"
    content: str  # e.g. donald_en|taskon_official|bd_alex|dashboard_top
    term: str  # hook type, e.g. "47pct_bot"

    def normalized(self) -> "UTMParams":
        """Return a new ``UTMParams`` with every segment normalised.

        Raises ``ValueError`` for any segment that cannot be normalised.
        """
        return UTMParams(
            source=_normalize(self.source),
            medium=_normalize(self.medium),
            campaign=_normalize(self.campaign),
            content=_normalize(self.content),
            term=_normalize(self.term),
        )

    def as_query_dict(self) -> dict[str, str]:
        """Return the 5 segments as a ``utm_*``-keyed dict (normalised)."""
        n = self.normalized()
        return {
            "utm_source": n.source,
            "utm_medium": n.medium,
            "utm_campaign": n.campaign,
            "utm_content": n.content,
            "utm_term": n.term,
        }


def build_utm(p: UTMParams) -> str:
    """Render the 5 segments as a leading-``?`` query string.

    Example::

        >>> build_utm(UTMParams("Twitter", "thread", "2026W19-thread01",
        ...                     "donald_en", "47pct_bot"))
        '?utm_source=twitter&utm_medium=thread&utm_campaign=2026w19_thread01&utm_content=donald_en&utm_term=47pct_bot'

    Raises ``ValueError`` for any segment that fails normalisation.
    """
    qs = urlencode(p.as_query_dict())
    return f"?{qs}"


def append_utm(url: str, p: UTMParams) -> str:
    """Merge UTM segments into ``url``, preserving any pre-existing query.

    If the URL already has ``utm_*`` keys, they are overwritten by the new
    segments (the explicit caller intent wins). Fragments (``#anchor``) are
    preserved.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("url must be a non-empty string")
    parsed = urlparse(url)
    existing: dict[str, list[str]] = parse_qs(parsed.query, keep_blank_values=True)
    # Strip any existing utm_* keys so we don't end up with duplicates.
    existing = {k: v for k, v in existing.items() if not k.lower().startswith("utm_")}
    # Re-flatten existing multi-value params (keep all values).
    flat_pairs: list[tuple[str, str]] = []
    for k, vs in existing.items():
        for v in vs:
            flat_pairs.append((k, v))
    # Append our 5 utm_* keys in canonical order.
    for k, v in p.as_query_dict().items():
        flat_pairs.append((k, v))
    new_query = urlencode(flat_pairs)
    return urlunparse(parsed._replace(query=new_query))


def parse_utm(url_or_query: str) -> UTMParams | None:
    """Inverse of :func:`build_utm`. Returns ``None`` if any segment missing.

    Accepts either a full URL or a bare query string (with or without leading
    ``?``). Values are returned verbatim (already-normalised, since they were
    produced by :func:`build_utm`).
    """
    if not isinstance(url_or_query, str) or not url_or_query:
        return None
    # Detect URL vs raw query string by looking for "://".
    if "://" in url_or_query:
        query = urlparse(url_or_query).query
    else:
        query = url_or_query.lstrip("?")
    if not query:
        return None
    params = parse_qs(query, keep_blank_values=False)
    needed = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
    if not all(k in params and params[k] for k in needed):
        return None
    return UTMParams(
        source=params["utm_source"][0],
        medium=params["utm_medium"][0],
        campaign=params["utm_campaign"][0],
        content=params["utm_content"][0],
        term=params["utm_term"][0],
    )


__all__ = ["UTMParams", "build_utm", "append_utm", "parse_utm"]


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    logger = logging.getLogger(__name__)

    p = UTMParams(
        source="Twitter",
        medium="thread",
        campaign="2026W19-thread01",
        content="donald_en",
        term="47pct_bot",
    )
    qs = build_utm(p)
    logger.info("build_utm -> %s", qs)
    merged = append_utm("https://taskon.xyz/benchmark-report?ref=newsletter#cta", p)
    logger.info("append_utm -> %s", merged)
    parsed = parse_utm(merged)
    logger.info("parse_utm -> %s", parsed)
    assert parsed is not None and parsed.campaign == "2026w19_thread01"
    logger.info("self-test passed")
