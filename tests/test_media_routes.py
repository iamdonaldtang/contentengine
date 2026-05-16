"""Integration tests for ``ingestion.media_routes`` — signed-URL media endpoint."""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import pytest


_SECRET = "test-media-secret-32-bytes-xxxxxxxxxxxxx"
_BASE = "https://ingest.test.xyz"


@pytest.fixture()
def client(tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Flask test client with media env + DRAFTS_DIR pointed at tmp_path."""
    monkeypatch.setenv("MEDIA_URL_BASE", _BASE)
    monkeypatch.setenv("MEDIA_URL_SECRET", _SECRET)
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    for mod in (
        "ingestion.app",
        "ingestion.wsgi",
        "ingestion.media_routes",
        "ingestion.mpt_callback",
        "lib.media_url",
        "ingestion",
    ):
        if mod in sys.modules:
            del sys.modules[mod]
    app_module = importlib.import_module("ingestion.app")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c._tmp_path = tmp_path  # type: ignore[attr-defined]
        yield c


def _seed_mp4(tmp_path: Path, piece_id: str, filename: str = "shorts_60s.mp4", size: int = 1024) -> Path:
    target = tmp_path / "drafts" / piece_id / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00" * size)
    return target


def _signed(piece_id: str, filename: str, *, ttl: int = 3600) -> tuple[str, str]:
    """Build expires+sig query params using the test secret."""
    import hashlib
    import hmac

    expires = int(time.time()) + ttl
    canonical = f"{piece_id}/{filename}.{expires}"
    sig = hmac.new(_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return str(expires), sig


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_serve_media_happy_path_streams_file(client: Any) -> None:
    _seed_mp4(client._tmp_path, "2026W20-thread02", size=4096)
    expires, sig = _signed("2026W20-thread02", "shorts_60s.mp4")
    resp = client.get(
        f"/api/media/2026W20-thread02/shorts_60s.mp4?expires={expires}&sig={sig}"
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.headers["Content-Type"].startswith("video/mp4")
    assert resp.headers.get("Content-Length") == "4096"


def test_serve_media_supports_mp3_mimetype(client: Any, tmp_path: Path) -> None:
    _seed_mp4(client._tmp_path, "p1", filename="track.mp3", size=512)
    expires, sig = _signed("p1", "track.mp3")
    resp = client.get(f"/api/media/p1/track.mp3?expires={expires}&sig={sig}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("audio/mpeg")


# --------------------------------------------------------------------------- #
# Auth failures
# --------------------------------------------------------------------------- #


def test_serve_media_missing_sig_401(client: Any) -> None:
    _seed_mp4(client._tmp_path, "p1")
    resp = client.get("/api/media/p1/shorts_60s.mp4")
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "unauthorized"


def test_serve_media_bad_sig_401(client: Any) -> None:
    _seed_mp4(client._tmp_path, "p1")
    expires = str(int(time.time()) + 3600)
    resp = client.get(f"/api/media/p1/shorts_60s.mp4?expires={expires}&sig=" + "0" * 64)
    assert resp.status_code == 401


def test_serve_media_expired_401(client: Any) -> None:
    _seed_mp4(client._tmp_path, "p1")
    # Sign an already-past expires.
    import hashlib
    import hmac

    expires = str(int(time.time()) - 100)
    canonical = f"p1/shorts_60s.mp4.{expires}"
    sig = hmac.new(_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    resp = client.get(f"/api/media/p1/shorts_60s.mp4?expires={expires}&sig={sig}")
    assert resp.status_code == 401
    assert "expired" in resp.get_json()["message"]


def test_serve_media_tampered_piece_id_401(client: Any) -> None:
    _seed_mp4(client._tmp_path, "p1")
    _seed_mp4(client._tmp_path, "p2-evil")
    # Sig was issued for p1; request a different piece's file.
    expires, sig = _signed("p1", "shorts_60s.mp4")
    resp = client.get(f"/api/media/p2-evil/shorts_60s.mp4?expires={expires}&sig={sig}")
    assert resp.status_code == 401
    assert "sig_mismatch" in resp.get_json()["message"]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_serve_media_bad_suffix_400(client: Any) -> None:
    # Even with valid sig, .md is not in the whitelist.
    target = client._tmp_path / "drafts" / "p1" / "selection_card.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("payload")
    expires, sig = _signed("p1", "selection_card.yaml")
    resp = client.get(f"/api/media/p1/selection_card.yaml?expires={expires}&sig={sig}")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "bad_suffix"


def test_serve_media_not_found_returns_404(client: Any) -> None:
    # No file exists at this path.
    expires, sig = _signed("ghost-piece", "shorts_60s.mp4")
    resp = client.get(f"/api/media/ghost-piece/shorts_60s.mp4?expires={expires}&sig={sig}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Disabled by missing secret
# --------------------------------------------------------------------------- #


def test_serve_media_no_secret_returns_401(
    tmp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If MEDIA_URL_SECRET is unset, every request gets 401 — endpoint
    effectively disabled (safe default)."""
    monkeypatch.setenv("MEDIA_URL_BASE", _BASE)
    monkeypatch.delenv("MEDIA_URL_SECRET", raising=False)
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    for mod in (
        "ingestion.app",
        "ingestion.wsgi",
        "ingestion.media_routes",
        "ingestion.mpt_callback",
        "lib.media_url",
        "ingestion",
    ):
        if mod in sys.modules:
            del sys.modules[mod]
    app_module = importlib.import_module("ingestion.app")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        _seed_mp4(tmp_path, "p1")
        # Even with what would be a "valid" sig were the secret set:
        resp = c.get(
            "/api/media/p1/shorts_60s.mp4?expires=9999999999&sig=" + "a" * 64
        )
        assert resp.status_code == 401
        assert "no_secret" in resp.get_json()["message"]
