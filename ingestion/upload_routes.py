"""Public, password-gated asset upload surface for non-Cowork helpers (step 6 配图).

WHY
---
A part-time helper needs to deliver images into a piece without Tailscale,
without Cowork, without the admin token. This blueprint serves a tiny public
HTML page at ``GET /upload`` and accepts image uploads at
``POST /upload/asset/<piece_id>/<filename>`` gated by a SEPARATE weak token
(env ``UPLOAD_API_TOKEN``) that can ONLY upload images — never touches the
admin surface. Even if the helper's password leaks, blast radius = image
uploads into drafts/<piece>/ only.

SECURITY
--------
* ``UPLOAD_API_TOKEN`` env, empty/unset => upload disabled (safe default).
  Keep it DIFFERENT from ``ADMIN_API_TOKEN``.
* Path-traversal defense on piece_id + filename (resolve().relative_to root).
* Image-only: extension whitelist + magic-number sniff. No executables/text.
* Size capped by app MAX_CONTENT_LENGTH (16 MiB).
"""
from __future__ import annotations

import hmac
import logging
import os
import re
from pathlib import Path

from flask import Blueprint, Response, jsonify, request


logger = logging.getLogger("upload")

upload_bp = Blueprint("upload", __name__)

_UPLOAD_TOKEN_ENV = "UPLOAD_API_TOKEN"
_DRAFTS_ROOT = Path(os.environ.get("DRAFTS_DIR") or "/app/runtime/drafts")
_PIECE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _err(status: int, code: str, message: str):
    return jsonify({"status": "error", "code": code, "message": message}), status


def _verify_upload_token() -> bool:
    expected = os.environ.get(_UPLOAD_TOKEN_ENV, "").strip()
    if not expected:
        return False
    header = request.headers.get("Authorization", "")
    received = header[7:].strip() if header.startswith("Bearer ") else header.strip()
    if not received:
        return False
    try:
        return hmac.compare_digest(received, expected)
    except Exception:
        logger.exception("upload token compare failed")
        return False


def _looks_like_image(suffix: str, data: bytes) -> bool:
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


@upload_bp.post("/upload/asset/<piece_id>/<filename>")
def upload_asset(piece_id: str, filename: str):
    if not _verify_upload_token():
        return _err(401, "unauthorized", "wrong or missing password")
    if not _PIECE_ID_RE.match(piece_id):
        return _err(400, "bad_piece_id", "piece id format invalid")
    if not _FILENAME_RE.match(filename) or filename.startswith("."):
        return _err(400, "bad_filename", "filename format invalid")
    root = _DRAFTS_ROOT.resolve()
    p = (root / piece_id / filename).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return _err(400, "bad_path", "path escapes drafts root")
    if p.suffix.lower() not in _ASSET_SUFFIXES:
        return _err(400, "bad_suffix", f"image only: {sorted(_ASSET_SUFFIXES)}")
    data = request.get_data(cache=False)
    if not data:
        return _err(400, "empty_body", "no file data")
    if not _looks_like_image(p.suffix.lower(), data):
        return _err(400, "not_an_image", "body is not a real PNG/JPEG/WebP/GIF")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    logger.info("upload asset piece=%s file=%s bytes=%d", piece_id, filename, len(data))
    return jsonify({"status": "written", "piece_id": piece_id, "file": filename, "bytes": len(data)}), 201


_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TaskOn 配图上传</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto;padding:20px;color:#1a1a1a;background:#fafafa}
  h1{font-size:20px}
  label{display:block;margin:14px 0 6px;font-weight:600;font-size:14px}
  input[type=text],input[type=password]{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:15px;box-sizing:border-box}
  .drop{margin-top:8px;border:2px dashed #bbb;border-radius:12px;padding:26px;text-align:center;color:#777;background:#fff;cursor:pointer}
  .drop.hover{border-color:#3b82f6;color:#3b82f6;background:#eff6ff}
  button{margin-top:18px;width:100%;padding:12px;font-size:16px;border:0;border-radius:10px;background:#111;color:#fff;cursor:pointer}
  button:disabled{opacity:.5}
  .row{margin-top:14px;font-size:13px;padding:8px 10px;border-radius:8px}
  .ok{background:#ecfdf5;color:#065f46}
  .err{background:#fef2f2;color:#991b1b}
  .hint{color:#888;font-size:12px;margin-top:4px}
</style></head><body>
<h1>TaskOn 配图上传</h1>
<p class="hint">把图片拖进下面方框（或点击选择），填好 piece 编号和口令，点上传。仅支持 png/jpg/webp/gif。</p>
<label>piece 编号</label>
<input id="piece" type="text" placeholder="例如 20260603-01" autocomplete="off">
<label>口令</label>
<input id="pwd" type="password" placeholder="Donald 给你的上传口令" autocomplete="off">
<label>图片</label>
<div class="drop" id="drop">拖图到这里，或点击选择（可多张）</div>
<input id="file" type="file" accept="image/*" multiple style="display:none">
<button id="go" disabled>上传</button>
<div id="log"></div>
<script>
const drop=document.getElementById('drop'),fileEl=document.getElementById('file'),
  go=document.getElementById('go'),log=document.getElementById('log');
let files=[];
function refresh(){go.disabled=files.length===0;drop.textContent=files.length?(files.length+' 张已选：'+files.map(f=>f.name).join(', ')):'拖图到这里，或点击选择（可多张）';}
drop.onclick=()=>fileEl.click();
fileEl.onchange=()=>{files=[...fileEl.files];refresh();};
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('hover');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('hover');}));
drop.addEventListener('drop',ev=>{files=[...ev.dataTransfer.files].filter(f=>f.type.startsWith('image/'));refresh();});
function line(cls,txt){const d=document.createElement('div');d.className='row '+cls;d.textContent=txt;log.appendChild(d);}
go.onclick=async()=>{
  const piece=document.getElementById('piece').value.trim();
  const pwd=document.getElementById('pwd').value.trim();
  if(!piece||!pwd){line('err','请填 piece 编号和口令');return;}
  go.disabled=true;log.innerHTML='';
  for(const f of files){
    const safe=f.name.replace(/[^A-Za-z0-9._-]/g,'_');
    try{
      const r=await fetch('/upload/asset/'+encodeURIComponent(piece)+'/'+encodeURIComponent(safe),
        {method:'POST',headers:{'Authorization':'Bearer '+pwd},body:f});
      const j=await r.json();
      if(r.status===201)line('ok','✓ '+safe+' ('+j.bytes+' 字节)');
      else line('err','✗ '+safe+'：'+(j.message||j.code||r.status));
    }catch(e){line('err','✗ '+safe+'：'+e.message);}
  }
  go.disabled=false;
};
</script></body></html>"""


@upload_bp.get("/upload")
def upload_page():
    return Response(_HTML, mimetype="text/html; charset=utf-8")
