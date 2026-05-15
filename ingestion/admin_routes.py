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
ADMIN_TASK_DIR = Path("/app/runtime/admin_tasks")
ADMIN_TASK_DIR.mkdir(parents=True, exist_ok=True)

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
                f"{postiz_url}/api/public/v1/posts",
                headers={"Authorization": f"Bearer {postiz_key}"},
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
