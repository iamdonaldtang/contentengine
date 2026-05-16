"""Unit tests for ``lib.content_inject`` — CTA URL placeholder substitution."""
from __future__ import annotations

import logging

import pytest

from lib.content_inject import (
    PLACEHOLDER,
    MissingPlaceholderError,
    count_placeholder,
    has_placeholder,
    inject_cta,
)


_URL = "https://taskon.xyz/free-diagnostic?utm_source=youtube&utm_medium=video"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_has_placeholder_detects() -> None:
    assert has_placeholder(f"foo {PLACEHOLDER} bar") is True
    assert has_placeholder("foo bar") is False


def test_count_placeholder_counts() -> None:
    assert count_placeholder(f"foo {PLACEHOLDER} bar {PLACEHOLDER} baz") == 2
    assert count_placeholder(f"foo {PLACEHOLDER} bar") == 1
    assert count_placeholder("nope") == 0


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_inject_single_placeholder_replaces() -> None:
    content = f"评论告诉我 -> {PLACEHOLDER}"
    out = inject_cta(content, _URL)
    assert PLACEHOLDER not in out
    assert _URL in out
    assert out == f"评论告诉我 -> {_URL}"


def test_inject_multiple_placeholders_replaces_all(caplog: pytest.LogCaptureFixture) -> None:
    content = f"see {PLACEHOLDER} and also {PLACEHOLDER}"
    with caplog.at_level(logging.WARNING, logger="lib.content_inject"):
        out = inject_cta(content, _URL)
    assert out == f"see {_URL} and also {_URL}"
    # Warning logged because count > 1 is unusual.
    assert any("multiple" not in r.message and "placeholders" in r.message.lower()
               or "%d" in r.message for r in caplog.records)


def test_inject_preserves_surrounding_whitespace() -> None:
    content = f"line1\n\n{PLACEHOLDER}\n\nline3"
    out = inject_cta(content, _URL)
    assert out == f"line1\n\n{_URL}\n\nline3"


# --------------------------------------------------------------------------- #
# Fallback (no placeholder)
# --------------------------------------------------------------------------- #


def test_inject_no_placeholder_appends_in_fallback_mode(caplog: pytest.LogCaptureFixture) -> None:
    content = "plain content without placeholder"
    with caplog.at_level(logging.WARNING, logger="lib.content_inject"):
        out = inject_cta(content, _URL, strict=False)
    assert out.endswith(_URL + "\n")
    assert "plain content without placeholder" in out
    assert any("no" in r.message.lower() and "placeholder" in r.message.lower() for r in caplog.records)


def test_inject_no_placeholder_strict_raises() -> None:
    with pytest.raises(MissingPlaceholderError, match=r"\{\{CTA_URL\}\}"):
        inject_cta("plain content", _URL, strict=True)


def test_inject_fallback_strips_trailing_whitespace_before_appending() -> None:
    content = "trailing\n   \n  "
    out = inject_cta(content, _URL, strict=False)
    # rstrip() then "\n\n" + URL + "\n"
    assert out == f"trailing\n\n{_URL}\n"


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_inject_empty_url_raises() -> None:
    with pytest.raises(ValueError, match="cta_url"):
        inject_cta("any", "")


def test_inject_non_http_url_raises() -> None:
    with pytest.raises(ValueError, match="http"):
        inject_cta("any", "ftp://no.example/")


def test_inject_url_with_only_https_scheme_accepted() -> None:
    out = inject_cta(f"see {PLACEHOLDER}", "https://x.example/y")
    assert out == "see https://x.example/y"


# --------------------------------------------------------------------------- #
# Curly brace edge cases
# --------------------------------------------------------------------------- #


def test_inject_does_not_match_partial_placeholder() -> None:
    """Single-brace patterns must not be substituted."""
    content = "{CTA_URL} should not match this"
    out = inject_cta(content, _URL, strict=False)
    assert "{CTA_URL}" in out  # unchanged in body
    assert out.endswith(_URL + "\n")  # fallback append


def test_inject_does_not_match_different_placeholder_name() -> None:
    """Only ``{{CTA_URL}}`` triggers — typos like ``{{URL}}`` stay literal."""
    content = "click {{URL}} for more"
    out = inject_cta(content, _URL, strict=False)
    assert "{{URL}}" in out
    assert out.endswith(_URL + "\n")
