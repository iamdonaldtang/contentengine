"""Split an X (Twitter) thread draft into individual tweets for Postiz.

The content engine authors X threads as Markdown with ``## Tweet N`` section
headers (config ``schedule_planner.draft_filenames.x_thread`` = xthread_final.md).
Postiz's ``posts[0].value`` array IS the thread: one entry per tweet. The engine
used to send the whole markdown as a SINGLE value entry and let Postiz auto-split
it by character count — which broke tweets mid-sentence (1 main + ugly replies).

This module pre-splits at the author's semantic ``## Tweet N`` boundaries so
Postiz posts each tweet verbatim. Free X accounts cap at 280 weighted chars per
tweet; X Premium allows up to 25,000 (declared per account in config.yaml
``postiz.x_premium`` — Premium status can't be reliably auto-detected through the
browser-automation integration, so it is a config flag, not runtime detection).

Public API:
    split_x_thread(markdown, *, premium=False, cta_url=None) -> list[str]
    weighted_len(s) -> int
"""
from __future__ import annotations

import re

FREE_LIMIT = 280
PREMIUM_LIMIT = 25_000

# X weighted length: most codepoints count as 1, but CJK ideographs / kana /
# hangul / fullwidth forms and emoji count as 2. Approximation that errs toward
# counting MORE (safer — keeps us under the real limit).
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF), (0x2E80, 0x2EFF), (0x2F00, 0x2FDF), (0x3000, 0x303F),
    (0x3040, 0x30FF), (0x3130, 0x318F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
    (0xA000, 0xA4CF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF), (0x20000, 0x2FA1F),
)


def _is_wide(cp: int) -> bool:
    if cp >= 0x1F000:  # emoji, pictographs, symbol planes
        return True
    return any(a <= cp <= b for a, b in _CJK_RANGES)


def weighted_len(s: str) -> int:
    """X-style weighted length (CJK + emoji count as 2)."""
    return sum(2 if _is_wide(ord(c)) else 1 for c in s)


# Any level-2 header (## Tweet N, ## Reply (CTA link), ...) is a tweet boundary.
# The header LINE itself is dropped — only the body text becomes the tweet, so
# labels like "## Tweet 1 (Hook)" / "## Reply (CTA link)" never leak into a post.
_H2_HEADER = re.compile(r"^##\s+\S.*$", re.MULTILINE)
# Sentence enders (Latin + CJK) and hard newlines — preferred split points.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？\n])\s+")


def _parse_sections(markdown: str) -> list[str]:
    """Body text of each ``## ...`` section, in order.

    Splits on every level-2 header (## Tweet N, ## Reply, ...) and drops the
    header line. Leading single-``#`` metadata lines (before the first ``##``)
    are ignored. If there are no headers at all, the whole text (minus ``#``
    comment lines) is returned as one block for length-splitting.
    """
    headers = list(_H2_HEADER.finditer(markdown))
    if not headers:
        body = "\n".join(
            ln for ln in markdown.splitlines() if not ln.lstrip().startswith("#")
        ).strip()
        return [body] if body else []
    sections: list[str] = []
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown)
        body = markdown[start:end].strip()
        if body:
            sections.append(body)
    return sections


def _length_split(text: str, limit: int) -> list[str]:
    """Greedily pack an over-limit tweet into <=limit chunks at sentence /
    newline boundaries; hard-wrap on words only as a last resort."""
    atoms = [a for a in _SENTENCE_SPLIT.split(text.strip()) if a]
    chunks: list[str] = []
    cur = ""
    for atom in atoms:
        cand = f"{cur} {atom}".strip() if cur else atom
        if weighted_len(cand) <= limit:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if weighted_len(atom) <= limit:
            cur = atom
            continue
        # A single sentence still too long → hard-wrap on whitespace.
        word = ""
        for w in atom.split():
            cand2 = f"{word} {w}".strip() if word else w
            if weighted_len(cand2) <= limit:
                word = cand2
            else:
                if word:
                    chunks.append(word)
                word = w if weighted_len(w) <= limit else w  # oversize word kept as-is
        if word:
            cur = word
    if cur:
        chunks.append(cur)
    return chunks or [text.strip()]


def split_x_thread(
    markdown: str,
    *,
    premium: bool = False,
    cta_url: str | None = None,
    free_limit: int = FREE_LIMIT,
    premium_limit: int = PREMIUM_LIMIT,
) -> list[str]:
    """Return the thread as a list of tweet strings, each within the tier limit.

    Args:
        markdown: xthread_final.md content (``## Tweet N`` sections).
        premium: True if the target X account is X Premium (25k/tweet); else
            free (280/tweet, over-limit sections are sentence-split).
        cta_url: If given, appended as its OWN trailing reply tweet
            (主推无外链 · CTA 下沉到 reply). Never merged into a content tweet.
    """
    limit = premium_limit if premium else free_limit
    # If the draft carries a {{CTA_URL}} placeholder (typically inside a
    # "## Reply (CTA link)" section), bind the tracked URL there before splitting.
    if cta_url and "{{CTA_URL}}" in markdown:
        markdown = markdown.replace("{{CTA_URL}}", cta_url)
    tweets: list[str] = []
    for section in _parse_sections(markdown):
        if weighted_len(section) <= limit:
            tweets.append(section)
        else:
            tweets.extend(_length_split(section, limit))
    tweets = [t.strip() for t in tweets if t.strip()]
    # Append the tracked CTA as its OWN trailing reply only when the thread has
    # no link yet. If the author already wrote a "## Reply (CTA link)" tweet
    # with a URL (or a placeholder was just filled), don't double up.
    if cta_url and not any("http" in t.lower() for t in tweets):
        tweets.append(cta_url.strip())
    return tweets or [markdown.strip()[:limit]]
