"""Unit tests for ``lib.media_url`` — sign + verify helpers."""
from __future__ import annotations

import importlib
import sys
import time
from typing import Any

import pytest


_SECRET = "test-media-secret-32-bytes-xxxxxxxxxxxxx"
_BASE = "https://ingest.test.xyz"


def _fresh(monkeypatch: pytest.MonkeyPatch, *, base: str | None = _BASE, secret: str | None = _SECRET) -> Any:
    if base is None:
        monkeypatch.delenv("MEDIA_URL_BASE", raising=False)
    else:
        monkeypatch.setenv("MEDIA_URL_BASE", base)
    if secret is None:
        monkeypatch.delenv("MEDIA_URL_SECRET", raising=False)
    else:
        monkeypatch.setenv("MEDIA_URL_SECRET", secret)
    if "lib.media_url" in sys.modules:
        del sys.modules["lib.media_url"]
    return importlib.import_module("lib.media_url")


# --------------------------------------------------------------------------- #
# sign_media_url
# --------------------------------------------------------------------------- #


def test_sign_media_url_returns_full_url_with_expires_and_sig(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    url = mu.sign_media_url("2026W20-thread02", "shorts_60s.mp4", now=1715820000)
    assert url.startswith(f"{_BASE}/api/media/2026W20-thread02/shorts_60s.mp4")
    assert "expires=1715823600" in url  # 1715820000 + 3600
    assert "sig=" in url
    # sig must be 64-hex (sha256)
    sig_part = url.split("sig=", 1)[1]
    assert len(sig_part) == 64
    int(sig_part, 16)  # valid hex


def test_sign_media_url_deterministic_given_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    url1 = mu.sign_media_url("p1", "x.mp4", now=1000)
    url2 = mu.sign_media_url("p1", "x.mp4", now=1000)
    assert url1 == url2


def test_sign_media_url_respects_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    url = mu.sign_media_url("p1", "x.mp4", ttl_seconds=60, now=1000)
    assert "expires=1060" in url


def test_sign_media_url_no_base_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch, base=None)
    with pytest.raises(mu.MediaUrlConfigError, match="MEDIA_URL_BASE"):
        mu.sign_media_url("p1", "x.mp4")


def test_sign_media_url_no_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch, secret=None)
    with pytest.raises(mu.MediaUrlConfigError, match="MEDIA_URL_SECRET"):
        mu.sign_media_url("p1", "x.mp4")


def test_sign_media_url_bad_base_scheme_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch, base="ftp://wat.example.com")
    with pytest.raises(mu.MediaUrlConfigError, match="http://"):
        mu.sign_media_url("p1", "x.mp4")


@pytest.mark.parametrize("piece_id", ["", "../etc", "p/sub", ".hidden", "a/b"])
def test_sign_media_url_rejects_bad_piece_id(monkeypatch: pytest.MonkeyPatch, piece_id: str) -> None:
    mu = _fresh(monkeypatch)
    with pytest.raises(ValueError, match="piece_id"):
        mu.sign_media_url(piece_id, "x.mp4")


@pytest.mark.parametrize("filename", ["", "..", "a/b.mp4", ".hidden.mp4", "../etc/passwd"])
def test_sign_media_url_rejects_bad_filename(monkeypatch: pytest.MonkeyPatch, filename: str) -> None:
    mu = _fresh(monkeypatch)
    with pytest.raises(ValueError, match="filename"):
        mu.sign_media_url("piece", filename)


@pytest.mark.parametrize("ttl", [0, -1, -3600])
def test_sign_media_url_rejects_non_positive_ttl(monkeypatch: pytest.MonkeyPatch, ttl: int) -> None:
    mu = _fresh(monkeypatch)
    with pytest.raises(ValueError, match="ttl_seconds"):
        mu.sign_media_url("p1", "x.mp4", ttl_seconds=ttl)


# --------------------------------------------------------------------------- #
# verify_media_url
# --------------------------------------------------------------------------- #


def _extract_params(url: str) -> tuple[str, str]:
    """Pull expires + sig out of a freshly-signed URL."""
    from urllib.parse import parse_qs, urlparse

    parts = urlparse(url)
    qs = parse_qs(parts.query)
    return qs["expires"][0], qs["sig"][0]


def test_verify_round_trip_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    url = mu.sign_media_url("p1", "x.mp4", now=int(time.time()))
    expires, sig = _extract_params(url)
    ok, reason = mu.verify_media_url("p1", "x.mp4", expires, sig)
    assert ok, reason
    assert reason == "ok"


def test_verify_missing_params(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    ok, reason = mu.verify_media_url("p1", "x.mp4", None, None)
    assert ok is False
    assert reason == "missing_params"


def test_verify_bad_expires_format(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    ok, reason = mu.verify_media_url("p1", "x.mp4", "not-an-int", "deadbeef" * 8)
    assert ok is False
    assert reason == "bad_expires_format"


def test_verify_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    # Sign with expires that's already in the past relative to verify's "now".
    url = mu.sign_media_url("p1", "x.mp4", ttl_seconds=60, now=1000)
    expires, sig = _extract_params(url)
    ok, reason = mu.verify_media_url("p1", "x.mp4", expires, sig, now=2000)
    assert ok is False
    assert reason.startswith("expired_")


def test_verify_sig_mismatch_on_tampered_piece_id(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    url = mu.sign_media_url("p1", "x.mp4", now=1000)
    expires, sig = _extract_params(url)
    # Different piece_id with same sig must fail.
    ok, reason = mu.verify_media_url("p2-evil", "x.mp4", expires, sig, now=1000)
    assert ok is False
    assert reason == "sig_mismatch"


def test_verify_sig_mismatch_on_tampered_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    url = mu.sign_media_url("p1", "x.mp4", now=1000)
    expires, sig = _extract_params(url)
    ok, reason = mu.verify_media_url("p1", "y.mp4", expires, sig, now=1000)
    assert ok is False
    assert reason == "sig_mismatch"


def test_verify_sig_mismatch_on_tampered_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch)
    url = mu.sign_media_url("p1", "x.mp4", now=1000)
    expires, sig = _extract_params(url)
    # Bump expires by 1 → sig invalid (sig binds expires too).
    bumped = str(int(expires) + 1)
    ok, reason = mu.verify_media_url("p1", "x.mp4", bumped, sig, now=1000)
    assert ok is False
    assert reason == "sig_mismatch"


def test_verify_secret_rotation_old_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sign with secret A
    mu = _fresh(monkeypatch, secret="secret-AAAAA")
    url = mu.sign_media_url("p1", "x.mp4", now=1000)
    expires, sig = _extract_params(url)

    # Rotate to secret B
    mu2 = _fresh(monkeypatch, secret="secret-BBBBB")
    ok, reason = mu2.verify_media_url("p1", "x.mp4", expires, sig, now=1000)
    assert ok is False
    assert reason == "sig_mismatch"


def test_verify_no_secret_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    mu = _fresh(monkeypatch, secret=None)
    ok, reason = mu.verify_media_url("p1", "x.mp4", "9999999999", "deadbeef" * 8)
    assert ok is False
    assert reason == "no_secret"
