#!/usr/bin/env bash
# smoke_httpfirst.sh · HTTP-first 控制面端到端回归冒烟（2026-06-03）
# ===========================================================================
# 目的：一条命令把 13 步控制面的关键端点真实跑一遍（piece 生命周期 + 配图往返），
#       自动清理、任何一步失败即非零退出。两个用途：
#         1) 每次 docker compose up --build ingestion 之后的「部署闸门」——
#            不绿就别认为部署成功（我们踩过 COPY CACHED 静默没生效的坑）。
#         2) 定时探活（cron / Cowork scheduled task）——路由消失/鉴权坏/job 挂会立刻暴露。
#
# 依赖：同目录 tk.sh（取 ENGINE_BASE + token）。token 来源：$ADMIN_API_TOKEN 或 scripts/.tk_token。
# 用法：bash scripts/smoke_httpfirst.sh          # 默认打公网 ingest.taskon.xyz
#       ENGINE_BASE=http://engine:5051 bash scripts/smoke_httpfirst.sh   # 内网
# 退出码：0 全过 / 1 有失败 / 2 没拿到 token
# ===========================================================================
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/tk.sh" >/dev/null 2>&1 || { echo "✗ 无法 source tk.sh"; exit 2; }
_tk_need_token || exit 2
B="${ENGINE_BASE:-https://ingest.taskon.xyz}"
H=(-H "Authorization: Bearer $ADMIN_API_TOKEN")
J=(-H "Content-Type: application/json")
P="SMOKE-$(date +%H%M%S)-$$"
PASS=0; FAIL=0
ck(){ if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  ✓ $1 ($3)"; else FAIL=$((FAIL+1)); echo "  ✗ $1  期望[$2] 实得[$3]"; fi; }
code(){ curl -s -m 15 -o /dev/null -w '%{http_code}' "$@"; }

echo "== smoke @ $B  piece=$P =="

# 0 公网/内网健康
ck "health 200" 200 "$(code "$B/health")"
ck "admin auth 401(无token)" 401 "$(code -X POST "$B/admin/jobs/adapter_orchestrator")"

# 1 文件契约：写选题卡 + 正稿
SC=$'id: '"$P"$'\ntitle_hypothesis: smoke\nhook_type: smoke\n'
ck "write selection_card 201" 201 "$(printf '%s' "$SC" | code "${H[@]}" -X POST "$B/admin/drafts/$P/selection_card.yaml" --data-binary @-)"
ck "write xthread 201" 201 "$(printf '# hook\nbody\n' | code "${H[@]}" -X POST "$B/admin/drafts/$P/xthread_final.md" --data-binary @-)"

# 2 select + 状态机
ck "select 200" 200 "$(code "${H[@]}" "${J[@]}" -X POST "$B/admin/pieces/$P/select" -d '{}')"
ST=$(curl -s "${H[@]}" "$B/admin/drafts/$P" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
ck "state==selected" "selected" "$ST"
ck "state->reviewed 200" 200 "$(code "${H[@]}" "${J[@]}" -X POST "$B/admin/pieces/$P/state" -d '{"state":"reviewed"}')"

# 3 配图往返（魔数 + 字节一致）
python3 -c "import struct,zlib;ih=struct.pack('>IIBBBBB',1,1,8,2,0,0,0);open('/tmp/_s.png','wb').write(b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+ih+struct.pack('>I',zlib.crc32(b'IHDR'+ih)&0xffffffff))"
ck "asset upload 201" 201 "$(code "${H[@]}" -X POST "$B/admin/assets/$P/x_main.png" --data-binary @/tmp/_s.png)"
ck "asset fake-image 400" 400 "$(printf 'notimg' | code "${H[@]}" -X POST "$B/admin/assets/$P/fake.png" --data-binary @-)"
curl -s "${H[@]}" "$B/admin/assets/$P/x_main.png" -o /tmp/_s_back.png 2>/dev/null
if cmp -s /tmp/_s.png /tmp/_s_back.png; then PASS=$((PASS+1)); echo "  ✓ asset 读回字节一致"; else FAIL=$((FAIL+1)); echo "  ✗ asset 读回字节不一致"; fi

# 4 job 通道（dry-run 安全，不发布）
TID=$(curl -s "${H[@]}" "${J[@]}" -X POST "$B/admin/jobs/schedule_planner" -d "{\"piece_id\":\"$P\",\"dry_run\":true}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("task_id",""))' 2>/dev/null)
if [ -n "$TID" ]; then
  for _ in 1 2 3 4 5 6; do
    S=$(curl -s "${H[@]}" "$B/admin/tasks/$TID" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
    case "$S" in done:*|failed:*) break;; esac; sleep 3
  done
  case "$S" in done:exit=0) PASS=$((PASS+1)); echo "  ✓ job schedule_planner $S";; *) FAIL=$((FAIL+1)); echo "  ✗ job schedule_planner 结束态=$S";; esac
else FAIL=$((FAIL+1)); echo "  ✗ job 未返回 task_id"; fi

# 5 清理（红线 kill）
ck "kill 200" 200 "$(code "${H[@]}" "${J[@]}" -X POST "$B/admin/pieces/$P/kill" -d '{}')"
ck "piece 已删 404" 404 "$(code "${H[@]}" "$B/admin/drafts/$P")"

echo "== 结果: PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" -eq 0 ] || { echo "SMOKE FAILED"; exit 1; }
echo "SMOKE OK"; exit 0
