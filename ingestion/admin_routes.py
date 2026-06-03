"""Admin API · 把本地 PowerShell 操作变成 HTTPS 触发，让 Cowork bash 直接调.

设计目标
--------
PowerShell 端的 `run_publish.ps1 / run_metrics.ps1 / docker logs` 等动作
原本只能在 Donald 桌面上手跑。本模块把这些动作包成 Flask Blueprint，挂到
ingestion 主 app (端口 5051)，通过 cloudflared 暴露成 `https://engine.taskon.xyz`，
Cowork bash sandbox 用 `curl -H "Authorization: Bearer <token>"` 直接触发。

安全模型（V1）
--------------
* Bearer Token 单令牌 (env: ADMIN_API_TOKEN)。空 = admin 全局禁用 (默认安全)
* 所有 admin 请求记 audit log 到 root logger (JSON formatter 已配)
* 没有做 IP 白名单——Cowork sandbox 出口 IP 不固定，做不到。靠 token 强度 + 轮换
* Token 泄露后旋转流程：改 .env → docker compose up -d --build engine → 旧 token 立失效

异步执行模型
------------
publish_immediate 完整链路要 30s-5min (mpt_runner 渲染 60s mp4 是大头)，
超出 CF Tunnel 默认同步窗口。所以走异步：
* POST /admin/run_publish → 立即返回 task_id (202 Accepted)
* 后台 threading + subprocess.Popen 跑命令，stdout 重定向到 task log file
* GET /admin/tasks/<task_id> → 返回 status + log tail (Cowork 轮询)

文件系统作为跨 worker 状态：gunicorn 多 worker 时，task log 和 status 存
/app/runtime/admin_tasks/，所有 worker 都能读，无需 Redis。

Hard rules
----------
* 不返回 stderr/exception 详情到响应体 (avoid info leak)，详情进日志
* subprocess timeout 必配 (15min 兜底 kill)
* piece_id 必须 alphanumeric + 横线下划线 (路径注入防御)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Blueprint, Response, jsonify, request

from lib.db import db


logger = logging.getLogger("admin")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ADMIN_TOKEN_ENV = "ADMIN_API_TOKEN"
ADMIN_TASK_DIR = Path(os.environ.get("ADMIN_TASK_DIR") or "/app/runtime/admin_tasks")
try:
    ADMIN_TASK_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # 容器外（CI / 本地 pytest）/app 不可写时不致命；运行期真写 task 文件若失败另有清晰日志。
    pass

# 15 min subprocess hard timeout — publish_immediate 包 mpt_runner 最坏 ~5min
_SUBPROC_TIMEOUT_S = 15 * 60

_PIECE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_TASK_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
_SERVICE_NAMES = {"shlink", "postiz", "mpt", "engine"}
_PLATFORMS_RE = re.compile(r"^[a-z_,]+$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(status: int, code: str, message: str, **extra: Any) -> tuple[Response, int]:
    body: dict[str, Any] = {"status": "error", "code": code, "message": message}
    body.update(extra)
    return jsonify(body), status


def _verify_admin_token() -> bool:
    """Bearer token check. Returns True if valid.

    If ADMIN_API_TOKEN env var is empty/unset, admin is fully disabled —
    return False for every request (safer default than open).
    """
    expected = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not expected:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    received = header[7:].strip()
    if not received:
        return False
    try:
        return hmac.compare_digest(received, expected)
    except Exception:
        logger.exception("admin token compare unexpected failure")
        return False


def _require_auth():
    """Returns error tuple if auth fails, else None.

    Audit-logs every request (success or fail) with masked source.
    """
    ok = _verify_admin_token()
    logger.info(
        "admin auth %s path=%s remote=%s",
        "ok" if ok else "FAIL",
        request.path,
        request.remote_addr,
    )
    if not ok:
        return _err(401, "unauthorized", "missing or invalid Bearer token")
    return None


def _spawn_task(task_id: str, cmd: list[str]) -> None:
    """Run subprocess in daemon thread, capture combined stdout/stderr to file.

    Status transitions: pending → running → done:exit=N | failed:<reason>
    """
    log_file = ADMIN_TASK_DIR / f"{task_id}.log"
    status_file = ADMIN_TASK_DIR / f"{task_id}.status"
    meta_file = ADMIN_TASK_DIR / f"{task_id}.meta.json"

    meta_file.write_text(
        json.dumps(
            {"task_id": task_id, "cmd": cmd, "started_at": _utc_now_iso()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    status_file.write_text("pending", encoding="utf-8")

    def _runner() -> None:
        status_file.write_text("running", encoding="utf-8")
        try:
            with log_file.open("w", encoding="utf-8") as f:
                f.write(f"$ {' '.join(cmd)}\n")
                f.write(f"started_at: {_utc_now_iso()}\n")
                f.write("---\n")
                f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    cwd="/app",
                    bufsize=1,
                )
                try:
                    rc = proc.wait(timeout=_SUBPROC_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    f.write(f"\n--- KILLED · timeout after {_SUBPROC_TIMEOUT_S}s ---\n")
                    status_file.write_text("failed:timeout", encoding="utf-8")
                    return
            status_file.write_text(f"done:exit={rc}", encoding="utf-8")
            logger.info("admin task %s done exit=%s", task_id, rc)
        except Exception as exc:
            logger.exception("admin task %s runner crashed", task_id)
            try:
                log_file.write_text(
                    log_file.read_text(encoding="utf-8") + f"\nrunner exception: {exc}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            status_file.write_text(f"failed:{type(exc).__name__}", encoding="utf-8")

    t = threading.Thread(target=_runner, daemon=True, name=f"admin-task-{task_id}")
    t.start()


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@admin_bp.post("/run_publish")
def admin_run_publish() -> tuple[Response, int]:
    """Trigger run_publish.ps1 equivalent pipeline: utm_generator → mpt_runner → publish_immediate.

    Request JSON:
      piece_id        (required) — e.g. "2026W20-thread01"
      target_url      (optional, default https://taskon.xyz/free-diagnostic)
      hook_type       (optional, default "default")
      offset_minutes  (optional, default 10)
      platforms       (optional, default "linkedin_post,yt_shorts")
      skip_mpt        (optional bool, default false) — skip MPT step if piece has no shorts_60s.md

    Response 202: {status: accepted, task_id, poll_url}
    """
    err = _require_auth()
    if err:
        return err

    payload = request.get_json(silent=True) or {}
    piece_id = (payload.get("piece_id") or "").strip()
    target_url = (payload.get("target_url") or "https://taskon.xyz/free-diagnostic").strip()
    hook_type = (payload.get("hook_type") or "default").strip()
    offset_minutes = int(payload.get("offset_minutes", 10))
    platforms = (payload.get("platforms") or "linkedin_post,yt_shorts").strip()
    skip_mpt = bool(payload.get("skip_mpt", False))

    # Validate inputs (defense against path / shell injection)
    if not _PIECE_ID_RE.match(piece_id):
        return _err(400, "bad_piece_id", "piece_id must match [A-Za-z0-9_-]{3,64}")
    if not _PLATFORMS_RE.match(platforms):
        return _err(400, "bad_platforms", "platforms must be lowercase a-z_, list")
    if not (0 <= offset_minutes <= 1440):
        return _err(400, "bad_offset", "offset_minutes must be 0-1440")
    # target_url must start with https:// and host in known set
    if not target_url.startswith("https://taskon.xyz/"):
        return _err(400, "bad_target_url", "target_url must be under https://taskon.xyz/")
    if not re.match(r"^[a-z_0-9]{1,32}$", hook_type):
        return _err(400, "bad_hook_type", "hook_type must be a-z_0-9 up to 32 chars")

    piece_dir = Path("/app/runtime/drafts") / piece_id
    if not piece_dir.exists():
        return _err(404, "piece_not_found", f"piece directory not found: {piece_id}")

    task_id = uuid.uuid4().hex[:12]

    # Build the 3-step pipeline (matches run_publish.ps1 logic).
    # We chain commands via bash so a single task log captures all output.
    mpt_step = (
        f"echo '[2/3] mpt_runner skipped (skip_mpt=true)'"
        if skip_mpt
        else (
            "echo '[2/3] mpt_runner...'; "
            f"python -m jobs.mpt_runner --piece-id {piece_id} --voice zh-CN-YunxiNeural-Male --timeout 900 "
            "|| echo '[mpt skipped or failed - non-fatal]'"
        )
    )

    bash_script = (
        "set -e\n"
        f"echo '=== piece: {piece_id} ==='\n"
        f"echo 'target_url: {target_url}'\n"
        f"echo 'hook_type: {hook_type}'\n"
        f"echo 'offset_minutes: {offset_minutes}'\n"
        f"echo 'platforms: {platforms}'\n"
        "echo ''\n"
        "echo '[1/3] utm_generator...'\n"
        f"python -m jobs.utm_generator --piece-id {piece_id} --target-url '{target_url}' "
        f"--platforms twitter,linkedin,youtube --accounts donald_en,taskon_official --hook-type {hook_type}\n"
        "echo ''\n"
        f"{mpt_step}\n"
        "echo ''\n"
        "echo '[3/3] publish_immediate...'\n"
        f"python -m scripts.publish_immediate --piece-id {piece_id} "
        f"--platforms {platforms} --offset-minutes {offset_minutes}\n"
        "echo ''\n"
        "echo '=== done ==='\n"
    )

    _spawn_task(task_id, ["bash", "-c", bash_script])

    logger.info(
        "admin run_publish accepted task=%s piece_id=%s platforms=%s offset=%d",
        task_id, piece_id, platforms, offset_minutes,
    )
    return jsonify({
        "status": "accepted",
        "task_id": task_id,
        "piece_id": piece_id,
        "poll_url": f"/admin/tasks/{task_id}",
    }), 202


@admin_bp.post("/run_metrics")
def admin_run_metrics() -> tuple[Response, int]:
    """Trigger run_metrics.ps1 equivalent — collect 24h post metrics for a piece.

    NOTE: 真实 jobs module 名等 publish 跑通后再确认. 第一版用 jobs.metrics_collector
    作为占位 — 如实际不存在会立即 fail 且日志清晰，不会静默无响应。
    """
    err = _require_auth()
    if err:
        return err

    payload = request.get_json(silent=True) or {}
    piece_id = (payload.get("piece_id") or "").strip()
    if not _PIECE_ID_RE.match(piece_id):
        return _err(400, "bad_piece_id", "piece_id must match [A-Za-z0-9_-]{3,64}")

    task_id = uuid.uuid4().hex[:12]
    _spawn_task(task_id, ["python", "-m", "jobs.metrics_collector", "--piece-id", piece_id])

    logger.info("admin run_metrics accepted task=%s piece_id=%s", task_id, piece_id)
    return jsonify({
        "status": "accepted",
        "task_id": task_id,
        "piece_id": piece_id,
        "poll_url": f"/admin/tasks/{task_id}",
    }), 202


@admin_bp.get("/tasks/<task_id>")
def admin_task_status(task_id: str) -> tuple[Response, int]:
    err = _require_auth()
    if err:
        return err

    if not _TASK_ID_RE.match(task_id):
        return _err(400, "bad_task_id", "task_id format invalid")

    status_file = ADMIN_TASK_DIR / f"{task_id}.status"
    log_file = ADMIN_TASK_DIR / f"{task_id}.log"

    if not status_file.exists():
        return _err(404, "task_not_found", "no such task_id")

    status = status_file.read_text(encoding="utf-8").strip()
    log_tail = ""
    log_bytes = 0
    if log_file.exists():
        try:
            content = log_file.read_text(encoding="utf-8")
            log_bytes = len(content.encode("utf-8"))
            log_tail = content[-8000:]  # last ~8KB
        except Exception:
            logger.exception("admin task %s log read failed", task_id)
            log_tail = "(log read failed)"

    return jsonify({
        "task_id": task_id,
        "status": status,
        "log_bytes": log_bytes,
        "log_tail": log_tail,
    }), 200


@admin_bp.get("/health/all")
def admin_health_all() -> tuple[Response, int]:
    """Aggregated health for engine + postiz + shlink + (optional MPT).

    Each subsystem reports ok/fail/unknown + detail. all_green = true iff
    all required subsystems are ok.
    """
    err = _require_auth()
    if err:
        return err

    out: dict[str, Any] = {"checked_at": _utc_now_iso()}

    # ---- engine self (SQLite reachable) ---- #
    try:
        row = db.fetchone("SELECT 1 AS ok")
        out["engine"] = "ok" if row and row["ok"] == 1 else "fail"
    except Exception as exc:
        logger.exception("admin health: engine db fail")
        out["engine"] = f"fail:{type(exc).__name__}"

    # ---- postiz (public posts endpoint, expect non-5xx) ---- #
    postiz_url = os.environ.get("POSTIZ_BASE_URL", "").rstrip("/")
    postiz_key = os.environ.get("POSTIZ_API_KEY", "")
    if not postiz_url or not postiz_key:
        out["postiz"] = "fail:not_configured"
    else:
        try:
            r = requests.get(
                # /integrations 用裸 key 返 200（/posts 缺查询参数会 400，虽 <500 算 ok 但不干净）。
                # 裸 key：Postiz 自托管要裸 key，加 Bearer 会 401（2026-06-03 实测），与 sources/postiz.py 一致。
                f"{postiz_url}/api/public/v1/integrations",
                headers={"Authorization": postiz_key},
                timeout=5,
            )
            out["postiz"] = f"ok:{r.status_code}" if r.status_code < 500 else f"fail:{r.status_code}"
        except Exception as exc:
            out["postiz"] = f"fail:{type(exc).__name__}"

    # ---- shlink (rest health endpoint expects status=pass) ---- #
    shlink_url = os.environ.get("SHLINK_BASE_URL", "").rstrip("/")
    if not shlink_url:
        out["shlink"] = "fail:not_configured"
    else:
        try:
            r = requests.get(f"{shlink_url}/rest/v3/health", timeout=5)
            if r.status_code == 200:
                try:
                    j = r.json()
                    out["shlink"] = "ok" if j.get("status") == "pass" else f"fail:status={j.get('status')}"
                except Exception:
                    out["shlink"] = "fail:non_json"
            else:
                out["shlink"] = f"fail:{r.status_code}"
        except Exception as exc:
            out["shlink"] = f"fail:{type(exc).__name__}"

    # ---- MoneyPrinterTurbo (optional · 本机 8090) ---- #
    mpt_url = os.environ.get("MPT_BASE_URL", "").rstrip("/")
    if mpt_url:
        try:
            r = requests.get(f"{mpt_url}/", timeout=5)
            out["mpt"] = f"ok:{r.status_code}" if r.status_code < 500 else f"fail:{r.status_code}"
        except Exception as exc:
            out["mpt"] = f"fail:{type(exc).__name__}"
    else:
        out["mpt"] = "skip:not_configured"

    out["all_green"] = (
        out["engine"] == "ok"
        and out["postiz"].startswith("ok")
        and out["shlink"] == "ok"
    )
    return jsonify(out), 200


@admin_bp.post("/restart_signal")
def admin_restart_signal() -> tuple[Response, int]:
    """Write a sentinel file requesting host to restart a service.

    本容器内部无法直接 `docker compose restart` (没 mount docker socket，
    且 mount socket 安全风险高)。改用 sentinel 文件 + 桌面 watch_tunnel_health.ps1
    监听 → 检测到 sentinel 后调 docker 重启。Sentinel 写完后桌面脚本若 60s 内
    未消费则视为 watcher 未运行，Donald 需手动重启。

    Request JSON: {"service": "shlink|postiz|mpt|engine"}
    Response 202: {status, service, sentinel, note}
    """
    err = _require_auth()
    if err:
        return err

    payload = request.get_json(silent=True) or {}
    service = (payload.get("service") or "").strip().lower()
    if service not in _SERVICE_NAMES:
        return _err(400, "bad_service", f"service must be one of: {sorted(_SERVICE_NAMES)}")

    sentinel = ADMIN_TASK_DIR / f"restart_{service}.signal"
    sentinel.write_text(_utc_now_iso(), encoding="utf-8")

    logger.warning("admin restart_signal written service=%s file=%s", service, sentinel)
    return jsonify({
        "status": "signal_written",
        "service": service,
        "sentinel": str(sentinel),
        "note": "host watch_tunnel_health.ps1 must pick up signal within 60s; otherwise manual docker compose restart needed",
    }), 202


# =========================================================================== #
# HTTP-first pipeline surface (2026-06-03 · 方案A)
# ---------------------------------------------------------------------------
# 让 Cowork sandbox 纯靠公网 HTTPS 跑完全流程操作手册 v3 的 13 步，
# 彻底不依赖 SMB(Z:) 写盘 / Tailscale SSH —— 二者在 Cowork sandbox 里物理不可达。
#
#   文件契约（替代 Z:\drafts 读写）
#     POST /admin/runtime-file/<name>     写 hot_topics_*.json 等 runtime 根文件   (步1.1)
#     POST /admin/drafts/<piece>/<file>   写文本草稿 (.md/.yaml/.json/.txt)         (步1.2/2/9)
#     GET  /admin/drafts/<piece>/<file>   读文本草稿                                (步3/4/5/8/11)
#     GET  /admin/drafts/<piece>          列 piece 文件 + state                     (审稿)
#   Job 触发（替代 run_remote.ps1 -Job → docker exec）
#     POST /admin/jobs/<job_name>         白名单派发 → _spawn_task (复用)           (步3/7/8/10/11/12/13)
#   状态迁移（替代裸 -Sqlite UPDATE）
#     POST /admin/pieces/<piece>/state    枚举校验 + 参数化 update_state            (步1.2/5)
#     POST /admin/pieces/<piece>/select   校验 selection_card → 置 selected         (步1.2 validate)
#     POST /admin/pieces/<piece>/kill     数据关失败=砍：删行 + 删目录(红线)        (步5 fail)
#
# 安全：全部 Bearer (_require_auth)；piece_id/filename 正则 + resolve().relative_to
# 防路径穿越；job 白名单 + 逐 job 参数校验；list-exec(无 shell) 杜绝命令注入；
# 写扩展名白名单（禁二进制/可执行，图片上传留 phase2）。
# =========================================================================== #

_DRAFTS_ROOT = Path(os.environ.get("DRAFTS_DIR") or "/app/runtime/drafts")
_RUNTIME_ROOT = Path(os.environ.get("RUNTIME_DIR") or "/app/runtime")

_DRAFT_TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".json", ".txt"}
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_RUNTIME_FILE_RE = re.compile(r"^(hot_topics|candidates_pool)_[A-Za-z0-9_-]{1,40}\.json$")

_PIECE_STATES = {
    "candidate", "selected", "drafted",
    "reviewed", "scheduled", "published", "measured",
}

# 平台 key（voice_checker / utm 用）
_PLATFORM_KEY_RE = re.compile(r"^[a-z0-9_]{1,20}$")
_ACCOUNTS_RE = re.compile(r"^[a-z0-9_,]{1,80}$")
_HOOK_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_VOICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,40}$")
_KOL_RE = re.compile(r"^@?[A-Za-z0-9_]{1,30}$")
_KIND_RE = re.compile(r"^[a-z_]{1,20}$")


def _safe_piece_dir(piece_id: str) -> Path | None:
    """Resolve drafts/<piece_id>, guaranteed inside _DRAFTS_ROOT, else None."""
    if not _PIECE_ID_RE.match(piece_id):
        return None
    root = _DRAFTS_ROOT.resolve()
    d = (root / piece_id).resolve()
    try:
        d.relative_to(root)
    except ValueError:
        return None
    return d


def _safe_draft_file(piece_id: str, filename: str) -> Path | None:
    """Resolve drafts/<piece_id>/<filename>, path-traversal safe, else None."""
    if not _FILENAME_RE.match(filename) or filename.startswith("."):
        return None
    base = _safe_piece_dir(piece_id)
    if base is None:
        return None
    p = (base / filename).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None
    return p


# --------------------------------------------------------------------------- #
# File contract endpoints
# --------------------------------------------------------------------------- #


@admin_bp.post("/runtime-file/<filename>")
def admin_write_runtime_file(filename: str) -> tuple[Response, int]:
    """Write a whitelisted runtime-root json (hot_topics_* / candidates_pool_*)."""
    err = _require_auth()
    if err:
        return err
    if not _RUNTIME_FILE_RE.match(filename):
        return _err(400, "bad_filename",
                    "filename must match hot_topics_<tag>.json or candidates_pool_<tag>.json")
    data = request.get_data(cache=False)
    if not data:
        return _err(400, "empty_body", "request body is empty")
    try:
        json.loads(data.decode("utf-8"))
    except Exception as exc:
        return _err(400, "bad_json", f"body is not valid UTF-8 JSON: {type(exc).__name__}")
    root = _RUNTIME_ROOT.resolve()
    target = (root / filename).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return _err(400, "bad_path", "resolved path escapes runtime root")
    target.write_bytes(data)
    logger.info("admin runtime-file written name=%s bytes=%d", filename, len(data))
    return jsonify({"status": "written", "path": f"runtime/{filename}", "bytes": len(data)}), 201


@admin_bp.post("/drafts/<piece_id>/<filename>")
def admin_write_draft(piece_id: str, filename: str) -> tuple[Response, int]:
    """Write a text draft into drafts/<piece_id>/ (creates dir). Text suffixes only."""
    err = _require_auth()
    if err:
        return err
    p = _safe_draft_file(piece_id, filename)
    if p is None:
        return _err(400, "bad_path", "invalid piece_id or filename")
    if p.suffix.lower() not in _DRAFT_TEXT_SUFFIXES:
        return _err(400, "bad_suffix",
                    f"writable suffixes: {sorted(_DRAFT_TEXT_SUFFIXES)} (binaries via phase2 asset upload)")
    data = request.get_data(cache=False)
    if not data:
        return _err(400, "empty_body", "request body is empty")
    if p.suffix.lower() == ".json":
        try:
            json.loads(data.decode("utf-8"))
        except Exception as exc:
            return _err(400, "bad_json", f"invalid JSON: {type(exc).__name__}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    logger.info("admin draft written piece=%s file=%s bytes=%d", piece_id, filename, len(data))
    return jsonify({
        "status": "written", "piece_id": piece_id, "file": filename, "bytes": len(data),
    }), 201


@admin_bp.get("/drafts/<piece_id>/<filename>")
def admin_read_draft(piece_id: str, filename: str) -> Any:
    """Read a text draft as text/plain. Binaries are served by media_bp (signed)."""
    err = _require_auth()
    if err:
        return err
    p = _safe_draft_file(piece_id, filename)
    if p is None:
        return _err(400, "bad_path", "invalid piece_id or filename")
    if p.suffix.lower() not in _DRAFT_TEXT_SUFFIXES:
        return _err(400, "bad_suffix", "only text drafts readable here; binaries via media route")
    if not p.exists():
        return _err(404, "not_found", f"{filename} not found in {piece_id}")
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:
        logger.exception("admin read draft failed piece=%s file=%s", piece_id, filename)
        return _err(500, "read_failed", f"{type(exc).__name__}")
    return Response(text, mimetype="text/plain; charset=utf-8")


@admin_bp.get("/drafts/<piece_id>")
def admin_list_draft(piece_id: str) -> tuple[Response, int]:
    """List files in drafts/<piece_id>/ plus the piece's DB state."""
    err = _require_auth()
    if err:
        return err
    d = _safe_piece_dir(piece_id)
    if d is None:
        return _err(400, "bad_piece_id", "invalid piece_id")
    if not d.exists():
        return _err(404, "not_found", f"piece dir not found: {piece_id}")
    files = sorted(f.name for f in d.iterdir() if f.is_file())
    state = None
    try:
        row = db.fetchone("SELECT state FROM pieces WHERE id = ?", (piece_id,))
        state = row["state"] if row else None
    except Exception:
        logger.exception("admin list draft: state lookup failed piece=%s", piece_id)
    return jsonify({"piece_id": piece_id, "state": state, "files": files}), 200


# --------------------------------------------------------------------------- #
# Job dispatch — curated whitelist (list-exec, no shell)
# --------------------------------------------------------------------------- #

_ALLOWED_JOBS = {
    "adapter_orchestrator", "voice_checker", "custom_slice_generator",
    "mpt_runner", "utm_generator", "schedule_planner", "kol_relation_tracker",
}


def _job_argv(job: str, payload: dict[str, Any]) -> list[str] | None:
    """Build a validated argv for a whitelisted job, or None if args invalid.

    list-exec means shell metacharacters are inert; validation here is for
    correctness + path/flag hygiene, not shell-escaping.
    """
    pid = (payload.get("piece_id") or "").strip()
    pid_ok = bool(_PIECE_ID_RE.match(pid))

    if job == "adapter_orchestrator":
        if not pid_ok:
            return None
        return ["python", "-m", "jobs.adapter_orchestrator", "--piece-id", pid]

    if job == "voice_checker":
        if not pid_ok:
            return None
        platform = (payload.get("platform") or "").strip()
        if not _PLATFORM_KEY_RE.match(platform):
            return None
        argv = ["python", "-m", "jobs.voice_checker", "--piece-id", pid, "--platform", platform]
        # 可选 file：覆盖 voice_checker 的 <platform>_final.md 自动解析（T2 修复 2026-06-03）。
        # adapter 写的是 medium_long.md / carousel_10pages.md / shorts_60s.md 等，名字对不上，
        # 改稿后独立复检必须显式传 file。
        fname = (payload.get("file") or "").strip()
        if fname:
            if not _FILENAME_RE.match(fname) or fname.startswith("."):
                return None
            argv += ["--file", str(_DRAFTS_ROOT / pid / fname)]
        return argv

    if job == "custom_slice_generator":
        if not pid_ok:
            return None
        argv = ["python", "-m", "jobs.custom_slice_generator", "--piece-id", pid]
        top_n = payload.get("top_n")
        if top_n is not None:
            if not isinstance(top_n, int) or not (1 <= top_n <= 20):
                return None
            argv += ["--top-n", str(top_n)]
        if bool(payload.get("force", False)):
            argv.append("--force")
        return argv

    if job == "mpt_runner":
        if not pid_ok:
            return None
        voice = (payload.get("voice") or "zh-CN-YunxiNeural-Male").strip()
        if not _VOICE_RE.match(voice):
            return None
        argv = ["python", "-m", "jobs.mpt_runner", "--piece-id", pid, "--voice", voice]
        if bool(payload.get("force", False)):
            argv.append("--force")
        return argv

    if job == "utm_generator":
        if not pid_ok:
            return None
        target = (payload.get("target_url") or "").strip()
        if not target.startswith("https://taskon.xyz/"):
            return None
        platforms = (payload.get("platforms") or "twitter,linkedin,youtube").strip()
        if not _PLATFORMS_RE.match(platforms):
            return None
        accounts = (payload.get("accounts") or "donald_en,taskon_official").strip()
        if not _ACCOUNTS_RE.match(accounts):
            return None
        hook = (payload.get("hook_type") or "default").strip()
        if not _HOOK_RE.match(hook):
            return None
        return ["python", "-m", "jobs.utm_generator", "--piece-id", pid,
                "--target-url", target, "--platforms", platforms,
                "--accounts", accounts, "--hook-type", hook]

    if job == "schedule_planner":
        if not pid_ok:
            return None
        argv = ["python", "-m", "jobs.schedule_planner", "--piece-id", pid]
        if bool(payload.get("dry_run", False)):
            argv.append("--dry-run")
        return argv

    if job == "kol_relation_tracker":
        if (payload.get("subcommand") or "").strip() != "log-dm":
            return None
        kol = (payload.get("kol") or "").strip()
        if not _KOL_RE.match(kol):
            return None
        kind = (payload.get("kind") or "reply").strip()
        if not _KIND_RE.match(kind):
            return None
        argv = ["python", "-m", "jobs.kol_relation_tracker", "log-dm", "--kol", kol, "--kind", kind]
        tweet = (payload.get("tweet_url") or "").strip()
        if tweet:
            if not tweet.startswith("https://"):
                return None
            argv += ["--tweet-url", tweet]
        if pid_ok:
            argv += ["--piece-id", pid]
        notes = (payload.get("notes") or "").strip()
        if notes:
            argv += ["--notes", notes[:200]]
        return argv

    return None


@admin_bp.post("/jobs/<job_name>")
def admin_run_job(job_name: str) -> tuple[Response, int]:
    """Trigger a whitelisted engine job inside the container (async task)."""
    err = _require_auth()
    if err:
        return err
    if job_name not in _ALLOWED_JOBS:
        return _err(400, "unknown_job", f"job not in whitelist: {sorted(_ALLOWED_JOBS)}")
    payload = request.get_json(silent=True) or {}
    argv = _job_argv(job_name, payload)
    if argv is None:
        return _err(400, "bad_args", f"missing/invalid args for job {job_name}")
    pid = (payload.get("piece_id") or "").strip()
    if pid and _PIECE_ID_RE.match(pid):
        d = _safe_piece_dir(pid)
        if d is None or not d.exists():
            return _err(404, "piece_not_found", f"piece dir not found: {pid}")
    task_id = uuid.uuid4().hex[:12]
    _spawn_task(task_id, argv)
    logger.info("admin job accepted task=%s job=%s piece=%s", task_id, job_name, pid or "-")
    return jsonify({
        "status": "accepted", "task_id": task_id, "job": job_name,
        "piece_id": pid or None, "poll_url": f"/admin/tasks/{task_id}",
    }), 202


# --------------------------------------------------------------------------- #
# Piece state transitions — safe, no raw SQL surface
# --------------------------------------------------------------------------- #


@admin_bp.post("/pieces/<piece_id>/state")
def admin_set_state(piece_id: str) -> tuple[Response, int]:
    """Set pieces.state via parameterized update_state (enum-validated)."""
    err = _require_auth()
    if err:
        return err
    if not _PIECE_ID_RE.match(piece_id):
        return _err(400, "bad_piece_id", "invalid piece_id")
    payload = request.get_json(silent=True) or {}
    state = (payload.get("state") or "").strip()
    if state not in _PIECE_STATES:
        return _err(400, "bad_state", f"state must be one of: {sorted(_PIECE_STATES)}")
    actor = (payload.get("actor") or "cowork").strip()[:40] or "cowork"
    notes = payload.get("notes")
    if not db.pieces.get(piece_id):
        return _err(404, "piece_not_found", f"no piece row: {piece_id}")
    try:
        db.pieces.update_state(piece_id, state, actor=actor, notes=notes)
    except Exception as exc:
        logger.exception("admin set_state failed piece=%s state=%s", piece_id, state)
        return _err(500, "update_failed", f"{type(exc).__name__}")
    logger.info("admin state piece=%s -> %s actor=%s", piece_id, state, actor)
    return jsonify({"status": "updated", "piece_id": piece_id, "state": state}), 200


@admin_bp.post("/pieces/<piece_id>/select")
def admin_select_piece(piece_id: str) -> tuple[Response, int]:
    """validate_selection equivalent: check selection_card.yaml → upsert state='selected'."""
    err = _require_auth()
    if err:
        return err
    if not _PIECE_ID_RE.match(piece_id):
        return _err(400, "bad_piece_id", "invalid piece_id")
    card = _safe_draft_file(piece_id, "selection_card.yaml")
    if card is None or not card.exists():
        return _err(404, "card_not_found", "selection_card.yaml not found; write it first")
    try:
        import yaml
        data = yaml.safe_load(card.read_text(encoding="utf-8"))
    except Exception as exc:
        return _err(400, "bad_yaml", f"selection_card.yaml not parseable: {type(exc).__name__}")
    if not isinstance(data, dict):
        return _err(400, "bad_card", "selection_card.yaml must be a mapping")
    missing = [k for k in ("id", "title_hypothesis", "hook_type") if not data.get(k)]
    if missing:
        return _err(400, "missing_fields", f"selection_card missing: {missing}")
    if str(data.get("id")).strip() != piece_id:
        return _err(400, "id_mismatch", f"card id {data.get('id')!r} != {piece_id!r}")
    try:
        if db.pieces.get(piece_id):
            db.pieces.update_state(piece_id, "selected", actor="cowork", notes="validated via /select")
        else:
            db.pieces.create(piece_id, card.read_text(encoding="utf-8"), actor="cowork")
            db.pieces.update_state(piece_id, "selected", actor="cowork", notes="validated via /select")
    except Exception as exc:
        logger.exception("admin select upsert failed piece=%s", piece_id)
        return _err(500, "upsert_failed", f"{type(exc).__name__}")
    logger.info("admin select piece=%s -> selected", piece_id)
    return jsonify({"status": "selected", "piece_id": piece_id}), 200


@admin_bp.post("/pieces/<piece_id>/kill")
def admin_kill_piece(piece_id: str) -> tuple[Response, int]:
    """数据关失败 = 砍（红线）：delete piece row + draft dir. Irreversible."""
    err = _require_auth()
    if err:
        return err
    if not _PIECE_ID_RE.match(piece_id):
        return _err(400, "bad_piece_id", "invalid piece_id")
    # pieces 有 FK 子表 (state_events / publishings / mpt_tasks ... NO ACTION)，
    # 裸 DELETE 会 FK 报错。运行时发现所有带 piece_id 列的表，先删子行再删 piece，
    # 单事务保证原子性。表名来自 sqlite_master（可信，非用户输入），piece_id 参数化。
    try:
        with db.transaction() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tables:
                if t == "pieces":
                    continue
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                if "piece_id" in cols:
                    conn.execute(f"DELETE FROM {t} WHERE piece_id = ?", (piece_id,))
            conn.execute("DELETE FROM pieces WHERE id = ?", (piece_id,))
    except Exception as exc:
        logger.exception("admin kill: db cascade delete failed piece=%s", piece_id)
        return _err(500, "delete_failed", f"{type(exc).__name__}")
    d = _safe_piece_dir(piece_id)
    dir_removed = False
    if d and d.exists():
        import shutil
        try:
            shutil.rmtree(d)
            dir_removed = True
        except Exception:
            logger.exception("admin kill: rmtree failed piece=%s", piece_id)
    logger.warning("admin kill piece=%s dir_removed=%s", piece_id, dir_removed)
    return jsonify({"status": "killed", "piece_id": piece_id, "dir_removed": dir_removed}), 200


# --------------------------------------------------------------------------- #
# 步骤 6 配图 · 二进制资产上传/读取（phase2 · 2026-06-03）
# Cowork 生成的 AI 配图（png/jpg/webp/gif）经此送进引擎，落在 drafts/<piece>/<file>
# （与 shorts_60s.mp4 同级，便于已有签名媒体路由 /api/media 对 Postiz 暴露）。
# 上传体可达 app MAX_CONTENT_LENGTH（已提到 16 MiB）。魔数校验防把任意二进制冒充图片。
# --------------------------------------------------------------------------- #

_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _looks_like_image(suffix: str, data: bytes) -> bool:
    """轻量魔数校验：确认 body 确实是声明的图片类型。"""
    if suffix == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    sigs = {
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".gif": (b"GIF87a", b"GIF89a"),
    }
    pats = sigs.get(suffix)
    return bool(pats) and any(data.startswith(p) for p in pats)


def _safe_asset_file(piece_id: str, filename: str) -> Path | None:
    """drafts/<piece>/<filename> 安全解析 + 图片扩展名白名单。"""
    p = _safe_draft_file(piece_id, filename)
    if p is None or p.suffix.lower() not in _ASSET_SUFFIXES:
        return None
    return p


@admin_bp.post("/assets/<piece_id>/<filename>")
def admin_upload_asset(piece_id: str, filename: str) -> tuple[Response, int]:
    """上传一张配图到 drafts/<piece_id>/<filename>（png/jpg/jpeg/webp/gif）。"""
    err = _require_auth()
    if err:
        return err
    p = _safe_asset_file(piece_id, filename)
    if p is None:
        return _err(400, "bad_path",
                    f"invalid piece_id/filename or suffix (allowed: {sorted(_ASSET_SUFFIXES)})")
    data = request.get_data(cache=False)
    if not data:
        return _err(400, "empty_body", "request body is empty")
    if not _looks_like_image(p.suffix.lower(), data):
        return _err(400, "not_an_image", "body does not match PNG/JPEG/WebP/GIF signature")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    logger.info("admin asset uploaded piece=%s file=%s bytes=%d", piece_id, filename, len(data))
    return jsonify({
        "status": "written", "piece_id": piece_id, "file": filename, "bytes": len(data),
    }), 201


@admin_bp.get("/assets/<piece_id>/<filename>")
def admin_get_asset(piece_id: str, filename: str) -> Any:
    """Bearer 读回配图（验证用）。Postiz 公网取图走 /api/media 签名 URL。"""
    err = _require_auth()
    if err:
        return err
    p = _safe_asset_file(piece_id, filename)
    if p is None:
        return _err(400, "bad_path", "invalid piece_id/filename or suffix")
    if not p.exists():
        return _err(404, "not_found", f"{filename} not found in {piece_id}")
    from flask import send_file
    return send_file(str(p))


# --------------------------------------------------------------------------- #
# 自助部署（第1档自动化 · 2026-06-03）
# Cowork 没法直跑引擎机 PowerShell/git/docker，所以走 sentinel 模式（同 restart_signal）：
#   POST /admin/deploy        → 写 runtime/admin_tasks/deploy.signal
#   引擎机常驻 watch_deploy.ps1 → 看到 signal 就跑 deploy_ingestion.ps1（fetch+reset--hard+rebuild+冒烟）
#   GET  /admin/deploy/status → 读 deploy.result（watcher 写回的结果）
# 流程：笔记本 push → Cowork tk_deploy → 引擎机自部署 → Cowork tk_deploy_status 看结果。
# --------------------------------------------------------------------------- #

_DEPLOY_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,64}$")


@admin_bp.post("/deploy")
def admin_deploy() -> tuple[Response, int]:
    """写部署 sentinel，让引擎机 host watcher 跑 deploy_ingestion.ps1。"""
    err = _require_auth()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    ref = (payload.get("ref") or "origin/main").strip()
    if not _DEPLOY_REF_RE.match(ref):
        return _err(400, "bad_ref", "ref must match [A-Za-z0-9_./-]{1,64}")
    sig = ADMIN_TASK_DIR / "deploy.signal"
    sig.write_text(
        json.dumps({"ref": ref, "requested_at": _utc_now_iso()}, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.warning("admin deploy signal written ref=%s file=%s", ref, sig)
    return jsonify({
        "status": "signal_written", "ref": ref,
        "note": "engine host watch_deploy.ps1 须在跑；轮询 /admin/deploy/status 看结果",
        "status_url": "/admin/deploy/status",
    }), 202


@admin_bp.get("/deploy/status")
def admin_deploy_status() -> tuple[Response, int]:
    """读 watcher 写回的最近一次部署结果 + 是否还有未消费的 signal。"""
    err = _require_auth()
    if err:
        return err
    sig = ADMIN_TASK_DIR / "deploy.signal"
    res = ADMIN_TASK_DIR / "deploy.result"
    out: dict[str, Any] = {"pending": sig.exists()}
    if res.exists():
        try:
            out["last_result"] = json.loads(res.read_text(encoding="utf-8"))
        except Exception:
            out["last_result"] = {"raw": res.read_text(encoding="utf-8")[-4000:]}
    return jsonify(out), 200
