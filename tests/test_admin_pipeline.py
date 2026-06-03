"""Integration tests for the HTTP-first pipeline surface in ``ingestion.admin_routes``.

第 3 层防线（改前回归）：把方案A 13 步控制面的所有 admin 端点行为固化，随 CI 跑，
任何改动破坏端点会在合并前红。覆盖：Bearer 鉴权、路径穿越、文件契约（runtime-file /
drafts 读写列）、job 白名单与参数校验、pieces state 枚举、select 校验、kill 的 FK 级联、
配图 assets（魔数 + 后缀 + 往返）。

设计：自包含 fixture——设 SQLITE_PATH / DRAFTS_DIR / RUNTIME_DIR / ADMIN_TASK_DIR /
ADMIN_API_TOKEN 到 tmp_path 后重载 ingestion.app，拿 Flask test client。不触网、不碰
真实 runtime。与 test_media_routes.py 同范式。
"""
from __future__ import annotations

import importlib
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Iterator

import pytest


_TOKEN = "testtoken-admin-pipeline"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BAD = {"Authorization": "Bearer wrong"}
_JSON = {"Content-Type": "application/json"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Flask test client with admin env pointed at tmp_path (fresh DB + drafts)."""
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ADMIN_TASK_DIR", str(tmp_path / "runtime" / "admin_tasks"))
    monkeypatch.setenv("ADMIN_API_TOKEN", _TOKEN)
    monkeypatch.setenv("ENABLE_LARK_ALERTS", "false")
    (tmp_path / "drafts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime" / "admin_tasks").mkdir(parents=True, exist_ok=True)
    # Drop cached imports so the fresh env (DB path, dirs) takes effect.
    for mod in (
        "ingestion.app", "ingestion.wsgi", "ingestion.admin_routes",
        "ingestion.media_routes", "ingestion.mpt_callback",
        "lib.lark", "lib.db", "ingestion",
    ):
        sys.modules.pop(mod, None)
    app_module = importlib.import_module("ingestion.app")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _png() -> bytes:
    ih = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ih
            + struct.pack(">I", zlib.crc32(b"IHDR" + ih) & 0xFFFFFFFF))


def _mkpiece(client: Any, piece_id: str) -> None:
    """Write a valid selection_card so a piece dir exists + select it."""
    card = f"id: {piece_id}\ntitle_hypothesis: t\nhook_type: smoke\n"
    r = client.post(f"/admin/drafts/{piece_id}/selection_card.yaml", headers=_AUTH, data=card)
    assert r.status_code == 201


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def test_auth_missing_token_401(client: Any) -> None:
    assert client.post("/admin/drafts/P-01/x.md", data="hi").status_code == 401


def test_auth_wrong_token_401(client: Any) -> None:
    assert client.post("/admin/drafts/P-01/x.md", headers=_BAD, data="hi").status_code == 401


# --------------------------------------------------------------------------- #
# runtime-file
# --------------------------------------------------------------------------- #

def test_runtime_file_bad_name_400(client: Any) -> None:
    assert client.post("/admin/runtime-file/evil.json", headers=_AUTH, data="{}").status_code == 400


def test_runtime_file_bad_json_400(client: Any) -> None:
    assert client.post("/admin/runtime-file/hot_topics_x.json", headers=_AUTH, data="{bad}").status_code == 400


def test_runtime_file_ok_201(client: Any) -> None:
    r = client.post("/admin/runtime-file/hot_topics_20260602.json", headers=_AUTH, data='{"a":1}')
    assert r.status_code == 201 and r.get_json()["bytes"] == 7


# --------------------------------------------------------------------------- #
# drafts: write / read / list  + path traversal
# --------------------------------------------------------------------------- #

def test_draft_bad_suffix_400(client: Any) -> None:
    assert client.post("/admin/drafts/P-01/run.exe", headers=_AUTH, data="x").status_code == 400


def test_draft_bad_json_400(client: Any) -> None:
    assert client.post("/admin/drafts/P-01/bad.json", headers=_AUTH, data="{nope}").status_code == 400


def test_draft_write_read_list(client: Any) -> None:
    assert client.post("/admin/drafts/P-01/xthread_final.md", headers=_AUTH, data="# hook\nbody").status_code == 201
    r = client.get("/admin/drafts/P-01/xthread_final.md", headers=_AUTH)
    assert r.status_code == 200 and b"hook" in r.data
    r = client.get("/admin/drafts/P-01", headers=_AUTH)
    assert r.status_code == 200 and "xthread_final.md" in r.get_json()["files"]


def test_draft_read_missing_404(client: Any) -> None:
    assert client.get("/admin/drafts/P-01/missing.md", headers=_AUTH).status_code == 404


def test_draft_path_traversal_blocked(client: Any) -> None:
    # Flask routing rejects '/' in a segment → 404; either way never escapes.
    assert client.post("/admin/drafts/P-01/..%2f..%2fevil.md", headers=_AUTH, data="x").status_code in (400, 404)


# --------------------------------------------------------------------------- #
# jobs dispatch
# --------------------------------------------------------------------------- #

def test_job_unknown_400(client: Any) -> None:
    assert client.post("/admin/jobs/rm_rf", headers=_AUTH, json={}).status_code == 400


def test_job_missing_piece_id_400(client: Any) -> None:
    assert client.post("/admin/jobs/adapter_orchestrator", headers=_AUTH, json={}).status_code == 400


def test_job_voice_checker_requires_platform_400(client: Any) -> None:
    assert client.post("/admin/jobs/voice_checker", headers=_AUTH, json={"piece_id": "P-01"}).status_code == 400


def test_job_utm_bad_target_url_400(client: Any) -> None:
    r = client.post("/admin/jobs/utm_generator", headers=_AUTH,
                    json={"piece_id": "P-01", "target_url": "http://evil.com", "hook_type": "x"})
    assert r.status_code == 400


def test_job_valid_args_missing_piece_dir_404(client: Any) -> None:
    r = client.post("/admin/jobs/adapter_orchestrator", headers=_AUTH, json={"piece_id": "99999999-99"})
    assert r.status_code == 404


def test_job_argv_no_shell_injection(client: Any) -> None:
    # list-exec: arguments are passed without a shell; verify argv shape.
    import ingestion.admin_routes as ar
    argv = ar._job_argv("schedule_planner", {"piece_id": "P-01", "dry_run": True})
    assert argv == ["python", "-m", "jobs.schedule_planner", "--piece-id", "P-01", "--dry-run"]


def test_job_argv_voice_checker_file(client: Any) -> None:
    """T2: voice_checker 支持显式 file，覆盖 <platform>_final.md 自动解析。"""
    import ingestion.admin_routes as ar
    argv = ar._job_argv("voice_checker", {"piece_id": "P-01", "platform": "blog", "file": "medium_long.md"})
    assert argv[:6] == ["python", "-m", "jobs.voice_checker", "--piece-id", "P-01", "--platform"]
    assert "--file" in argv and argv[-1].endswith("/P-01/medium_long.md")
    # 没传 file 时不带 --file（保持向后兼容）
    argv2 = ar._job_argv("voice_checker", {"piece_id": "P-01", "platform": "blog"})
    assert "--file" not in argv2
    # 路径穿越的 file 被拒
    assert ar._job_argv("voice_checker", {"piece_id": "P-01", "platform": "blog", "file": "../etc/passwd"}) is None


# --------------------------------------------------------------------------- #
# pieces: state / select / kill
# --------------------------------------------------------------------------- #

def test_state_bad_enum_400(client: Any) -> None:
    assert client.post("/admin/pieces/P-01/state", headers=_AUTH, json={"state": "banana"}).status_code == 400


def test_state_missing_row_404(client: Any) -> None:
    assert client.post("/admin/pieces/P-01/state", headers=_AUTH, json={"state": "reviewed"}).status_code == 404


def test_select_no_card_404(client: Any) -> None:
    assert client.post("/admin/pieces/NOPIECE-01/select", headers=_AUTH, json={}).status_code == 404


def test_select_id_mismatch_400(client: Any) -> None:
    client.post("/admin/drafts/P-01/selection_card.yaml", headers=_AUTH,
                data="id: WRONG\ntitle_hypothesis: h\nhook_type: x\n")
    assert client.post("/admin/pieces/P-01/select", headers=_AUTH, json={}).status_code == 400


def test_select_then_state_flow(client: Any) -> None:
    _mkpiece(client, "P-01")
    r = client.post("/admin/pieces/P-01/select", headers=_AUTH, json={})
    assert r.status_code == 200 and r.get_json()["status"] == "selected"
    assert client.get("/admin/drafts/P-01", headers=_AUTH).get_json()["state"] == "selected"
    assert client.post("/admin/pieces/P-01/state", headers=_AUTH, json={"state": "reviewed"}).status_code == 200


def test_kill_cascades_fk_children(client: Any) -> None:
    """select + state 会产生 state_events 子行；kill 必须级联删，不被 FK 挡。"""
    _mkpiece(client, "P-01")
    client.post("/admin/pieces/P-01/select", headers=_AUTH, json={})
    client.post("/admin/pieces/P-01/state", headers=_AUTH, json={"state": "reviewed"})
    import lib.db as dbm
    assert dbm.db.fetchone("SELECT COUNT(*) n FROM state_events WHERE piece_id=?", ("P-01",))["n"] > 0
    r = client.post("/admin/pieces/P-01/kill", headers=_AUTH, json={})
    assert r.status_code == 200 and r.get_json()["dir_removed"] is True
    assert dbm.db.pieces.get("P-01") is None
    assert dbm.db.fetchone("SELECT COUNT(*) n FROM state_events WHERE piece_id=?", ("P-01",))["n"] == 0
    assert client.get("/admin/drafts/P-01", headers=_AUTH).status_code == 404


# --------------------------------------------------------------------------- #
# assets (配图 phase2)
# --------------------------------------------------------------------------- #

def test_asset_unauth_401(client: Any) -> None:
    assert client.post("/admin/assets/P-01/x.png", data=_png()).status_code == 401


def test_asset_bad_suffix_400(client: Any) -> None:
    assert client.post("/admin/assets/P-01/x.txt", headers=_AUTH, data=_png()).status_code == 400


def test_asset_not_an_image_400(client: Any) -> None:
    assert client.post("/admin/assets/P-01/x.png", headers=_AUTH, data=b"not an image").status_code == 400


def test_asset_upload_read_roundtrip(client: Any) -> None:
    png = _png()
    r = client.post("/admin/assets/P-01/x_main.png", headers=_AUTH, data=png)
    assert r.status_code == 201 and r.get_json()["bytes"] == len(png)
    back = client.get("/admin/assets/P-01/x_main.png", headers=_AUTH)
    assert back.status_code == 200 and back.data == png


def test_asset_missing_404(client: Any) -> None:
    assert client.get("/admin/assets/P-01/nope.png", headers=_AUTH).status_code == 404
