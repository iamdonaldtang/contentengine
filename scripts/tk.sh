# tk.sh · TaskOn 内容流水线 · Cowork HTTP-first 助手库（方案A · 2026-06-03）
# ===========================================================================
# 背景：Cowork sandbox 物理上写不了 Z:（网络盘）、连不到 Tailscale 引擎机、
#       跑不了 PowerShell/run_remote.ps1。唯一能用的两条通道是 ① 本地挂载盘
#       ② 公网 HTTPS。本助手把全流程操作手册 v3 的 13 步全部映射到引擎
#       ingestion 服务的公网 admin 端点（ingest.taskon.xyz），Cowork 只 curl，
#       端到端即时、无 5 分钟轮询、永不碰 Z:/SSH。
#
# 用法：
#   在 Cowork bash 里：
#     export ADMIN_API_TOKEN='<引擎机 .env 里的 ADMIN_API_TOKEN>'
#     source /sessions/<...>/mnt/Taskon/marketing/engine/scripts/tk.sh
#   然后按 13 步调函数（见每个函数头注释里的 “步骤N”）。
#
# 约定：piece_id 形如 20260603-01。所有写文件函数发原始 body；
#       所有 job/state 函数发 JSON。出错时 curl -f 让非 2xx 直接报错退出。
# ===========================================================================

: "${ENGINE_BASE:=https://ingest.taskon.xyz}"

_tk_need_token() {
  if [ -z "${ADMIN_API_TOKEN:-}" ]; then
    # 没有 env 就从本地 token 文件读（一次性放好，已 gitignore）。
    # 优先级：$ADMIN_API_TOKEN env → $TASKON_TOKEN_FILE → <tk.sh同目录>/.tk_token
    local tf="${TASKON_TOKEN_FILE:-$(dirname "${BASH_SOURCE[0]}")/.tk_token}"
    [ -f "$tf" ] && ADMIN_API_TOKEN="$(tr -d '\r\n' < "$tf")"
  fi
  if [ -z "${ADMIN_API_TOKEN:-}" ]; then
    echo "✗ 没拿到 token：export ADMIN_API_TOKEN=... 或把 token 写进 $(dirname "${BASH_SOURCE[0]}")/.tk_token（已 gitignore，绝不入库）" >&2
    return 2
  fi
}
_tk_auth()  { _tk_need_token || return 2; printf 'Authorization: Bearer %s' "$ADMIN_API_TOKEN"; }
# 带鉴权的 curl；$1=method 其余透传
_tk_req() {
  _tk_need_token || return 2
  local method="$1"; shift
  curl -fsS -X "$method" -H "Authorization: Bearer $ADMIN_API_TOKEN" "$@"
}

# --- 步骤 0 · 入场检查 -----------------------------------------------------
tk_health() {
  echo "# public /health"; curl -fsS "$ENGINE_BASE/health"; echo
  echo "# /admin/health/all"; _tk_req GET "$ENGINE_BASE/admin/health/all"; echo
}

# --- 步骤 1.1 · 写 hot_topics（runtime 根） --------------------------------
# tk_hot <filename.json> <本地文件路径>
#   例：tk_hot hot_topics_20260602.json ./hot_topics_20260602.json
tk_hot() {
  local name="$1" path="$2"
  [ -f "$path" ] || { echo "✗ 文件不存在: $path" >&2; return 1; }
  _tk_req POST "$ENGINE_BASE/admin/runtime-file/$name" \
    --data-binary "@$path"; echo
}

# --- 步骤 1.2 / 2 / 6(yaml) / 9 · 写草稿到 drafts/<piece>/ ----------------
# tk_write <piece> <filename> <本地文件路径>
#   例：tk_write 20260603-01 selection_card.yaml ./sc.yaml
#       tk_write 20260603-01 xthread_final.md ./xthread.md
#       tk_write 20260603-01 yt_metadata.yaml ./yt.yaml
tk_write() {
  local piece="$1" file="$2" path="$3"
  [ -f "$path" ] || { echo "✗ 文件不存在: $path" >&2; return 1; }
  _tk_req POST "$ENGINE_BASE/admin/drafts/$piece/$file" \
    --data-binary "@$path"; echo
}

# 读草稿（步 3/4/5/8/11 看 engine 产物）：tk_read <piece> <file>
tk_read() { _tk_req GET "$ENGINE_BASE/admin/drafts/$1/$2"; }
# 列 piece 文件 + state：tk_ls <piece>
tk_ls()   { _tk_req GET "$ENGINE_BASE/admin/drafts/$1"; echo; }

# --- 步骤 6 · 配图上传（phase2）：tk_img <piece> <文件名.png> <本地路径> --------
#   例：tk_img 20260603-01 x_main.png ./x_main.png
tk_img() {
  local piece="$1" file="$2" path="$3"
  [ -f "$path" ] || { echo "✗ 文件不存在: $path" >&2; return 1; }
  _tk_req POST "$ENGINE_BASE/admin/assets/$piece/$file" --data-binary "@$path"; echo
}

# --- 步骤 1.2 · 校验选题卡 → 置 selected（validate_selection 等价） -------
tk_select() {
  _tk_req POST "$ENGINE_BASE/admin/pieces/$1/select" \
    -H "Content-Type: application/json" -d '{}'; echo
}

# --- 通用 job 触发（异步，返回 task_id）-----------------------------------
# tk_job <job_name> <json_body>
tk_job() {
  local job="$1" body="${2:-{}}"
  _tk_req POST "$ENGINE_BASE/admin/jobs/$job" \
    -H "Content-Type: application/json" -d "$body"; echo
}
# 步 3 · 4 平台改写 + voice：     tk_adapt <piece>
tk_adapt()   { tk_job adapter_orchestrator "{\"piece_id\":\"$1\"}"; }
# 步 4 · 单平台 voice 复检：       tk_voice <piece> <platform> [file]
#   file 省略时按 platform 自动映射 adapter 实际文件名（T2 修复 2026-06-03）
tk_voice() {
  local piece="$1" platform="$2" file="$3"
  if [ -z "$file" ]; then
    case "$platform" in
      blog)              file=medium_long.md;;
      linkedin_post)     file=linkedin_post.md;;
      linkedin_carousel) file=carousel_10pages.md;;
      yt_shorts)         file=shorts_60s.md;;
      x_thread)          file=xthread_final.md;;
    esac
  fi
  tk_job voice_checker "{\"piece_id\":\"$piece\",\"platform\":\"$platform\",\"file\":\"$file\"}"
}
# ===========================================================================
# 流水线阶段口令（单篇运行时覆盖）· 2026-06-24
# ---------------------------------------------------------------------------
# 两个可选阶段：video（步7 短视频）/ cta（步8 UTM 外链）。优先级：
#   运行时口令(下面这些 video=/cta= 参数) > selection_card.yaml stages: > config.yaml pipeline_stages: > 默认 on
# 口令取值（大小写随意）：on/off · yes/no · true/false · 1/0
# 用法举例（单篇这一次跳过两个阶段）：
#   tk_dryrun  20260624-01 video=off cta=off     # 先 dry-run 看计划
#   tk_schedule 20260624-01 video=off cta=off    # 真发：不发视频平台、全程无外链
#   tk_schedule 20260624-01 cta=off              # 只关 CTA，视频照发
# 想给某篇“永久”设置 → 直接在该篇 selection_card.yaml 加：
#   stages:
#     video: false
#     cta: false
# 把 video=/cta= 参数拼成 JSON 片段（无参数则空）。
_tk_stage_frag() {
  local frag="" tok
  for tok in "$@"; do
    case "$tok" in
      video=*)  frag="$frag,\"video\":\"${tok#video=}\"";;
      cta=*)    frag="$frag,\"cta\":\"${tok#cta=}\"";;
      visual=*) frag="$frag,\"visual\":\"${tok#visual=}\"";;
    esac
  done
  printf '%s' "$frag"
}
# 步 7 · 短视频渲染（异步）：      tk_video <piece> [voice] [video=on|off]
#   注：video=off 等于不渲染（一般直接不调本函数即可）；video=on 可强制覆盖全局/单篇的 off。
tk_video()   {
  local p="$1"; shift
  local v="zh-CN-YunxiNeural-Male"
  if [ $# -gt 0 ] && [ "${1#*=}" = "$1" ]; then v="$1"; shift; fi   # 非 key=val 才当 voice
  tk_job mpt_runner "{\"piece_id\":\"$p\",\"voice\":\"$v\"$(_tk_stage_frag "$@")}"
}
# 步 7.5 · 配图引擎（同步·CPU·无 GPU）：tk_visual <piece> [visual=on|off] [force=1]
#   产 x_hero.png / yt_thumb.png / carousel.pdf+分页 PNG 到 runtime/drafts/<piece>/。
#   visual=off 跳过；force=1 即使源未变也重渲。缺图发布时文字照发（不崩）。
tk_visual()  {
  local p="$1"; shift
  local frag=""; for tok in "$@"; do case "$tok" in force=1|force=true) frag="$frag,\"force\":true";; esac; done
  tk_job visual_runner "{\"piece_id\":\"$p\"$frag$(_tk_stage_frag "$@")}"
}
# 步 8 · UTM 短链：                tk_utm <piece> <target_url(https://taskon.xyz/...)> <hook_type> [cta=on|off]
#   cta=off 时本步自动跳过（不生成 utm_links.json，不烧短链 slot）。
tk_utm()     {
  local p="$1" u="$2" h="$3"; shift 3 2>/dev/null || shift $#
  tk_job utm_generator "{\"piece_id\":\"$p\",\"target_url\":\"$u\",\"hook_type\":\"$h\"$(_tk_stage_frag "$@")}"
}
# 步 10 · Custom Slice KOL DM：    tk_slice <piece>
tk_slice()   { tk_job custom_slice_generator "{\"piece_id\":\"$1\"}"; }
# 晋级扫描（数据层·flag-only·不渲染）：tk_promote [dry_run=1] [days=7] [top_pct=0.2]
#   读 7d 指标→给互动前 top_pct 的篇 写 promotion.json。dry_run=1 只评分不落盘。
tk_promote() {
  local frag="" tok
  for tok in "$@"; do case "$tok" in
    dry_run=1|dry_run=true) frag="$frag,\"dry_run\":true";;
    force=1|force=true) frag="$frag,\"force\":true";;
    days=*) frag="$frag,\"days\":${tok#days=}";;
    top_pct=*) frag="$frag,\"top_pct\":${tok#top_pct=}";;
  esac; done
  tk_job promotion_scanner "{\"_\":1$frag}"
}
# 步 11 · 调度 dry-run：           tk_dryrun <piece> [video=on|off] [cta=on|off] [visual=on|off]
tk_dryrun()  { local p="$1"; shift; tk_job schedule_planner "{\"piece_id\":\"$p\",\"dry_run\":true$(_tk_stage_frag "$@")}"; }
# 步 12(路径A) · 真发排程：        tk_schedule <piece> [video=on|off] [cta=on|off] [visual=on|off]
tk_schedule(){ local p="$1"; shift; tk_job schedule_planner "{\"piece_id\":\"$p\"$(_tk_stage_frag "$@")}"; }
# 步 13 · 记 KOL DM：              tk_logdm <piece> <@kol> <kind> <tweet_url>
tk_logdm()   { tk_job kol_relation_tracker \
  "{\"subcommand\":\"log-dm\",\"piece_id\":\"$1\",\"kol\":\"$2\",\"kind\":\"$3\",\"tweet_url\":\"$4\"}"; }

# --- 步骤 12(路径B) · 急发：绕过错峰立刻发（公网 admin，已有端点）---------
# tk_publish <piece> <platforms逗号分隔> [offset_minutes] [skip_mpt] [skip_cta]
#   skip_mpt=1 跳过步7 视频渲染；skip_cta=1 跳过步8 UTM（无外链发布）。
tk_publish() {
  local piece="$1" platforms="${2:-linkedin_post,yt_shorts}" offset="${3:-10}"
  local skip_mpt="${4:-false}" skip_cta="${5:-false}"
  [ "$skip_mpt" = "1" ] && skip_mpt=true; [ "$skip_cta" = "1" ] && skip_cta=true
  _tk_req POST "$ENGINE_BASE/admin/run_publish" -H "Content-Type: application/json" \
    -d "{\"piece_id\":\"$piece\",\"platforms\":\"$platforms\",\"offset_minutes\":$offset,\"skip_mpt\":$skip_mpt,\"skip_cta\":$skip_cta}"; echo
}

# --- 步骤 5 · 数据关结果 ---------------------------------------------------
# 过：tk_state <piece> reviewed   ；砍（红线）：tk_kill <piece>
tk_state() { _tk_req POST "$ENGINE_BASE/admin/pieces/$1/state" \
  -H "Content-Type: application/json" -d "{\"state\":\"$2\"}"; echo; }
tk_kill()  { _tk_req POST "$ENGINE_BASE/admin/pieces/$1/kill" \
  -H "Content-Type: application/json" -d '{}'; echo; }

# --- 任务轮询（异步 job 看结果）：tk_poll <task_id> -----------------------
tk_poll() { _tk_req GET "$ENGINE_BASE/admin/tasks/$1"; echo; }

# é»å¡ç­å°ä»»å¡ç»æï¼tk_wait <task_id> [æå¤ç§æ°,é»è®¤600]
# 600s è¦ç adapter_orchestratorï¼5 å¹³å° x ~40s LLMï¼/ schedule_planner ç­é jobï¼
# MPT ç­è§é¢æ¸²ææ´æ¢ï¼è· tk_video æ¶æå¨ç»æ´å¤§ï¼tk_wait <id> 1200ã
# å¤æ´»å«åªç statusï¼tk_poll <id> ç log_bytes æ¯å¦å¨æ¶¨ = å¥åº·ï¼ä¸è¦æ¥ç killã
tk_wait() {
  local tid="$1" max="${2:-600}" waited=0
  while :; do
    local s; s="$(_tk_req GET "$ENGINE_BASE/admin/tasks/$tid" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)"
    echo "  task $tid: $s (${waited}s)"
    case "$s" in done:*|failed:*) return 0;; esac
    [ "$waited" -ge "$max" ] && { echo "  â è¶æ¶ ${max}s"; return 1; }
    sleep 3; waited=$((waited+3))
  done
}

# --- èªå©é¨ç½²ï¼ç¬¬1æ¡£ï¼ï¼tk_deploy è§¦åå¼ææºèªé¨ç½²ï¼tk_deploy_status çç»æ ----------
# åæï¼ç¬è®°æ¬å·² push å° originï¼å¼ææºç watch_deploy.ps1 å¨å¸¸é©»ã
tk_deploy()        { _tk_req POST "$ENGINE_BASE/admin/deploy" -H "Content-Type: application/json" -d "{"ref":"${1:-origin/main}"}"; echo; }
tk_deploy_status() { _tk_req GET "$ENGINE_BASE/admin/deploy/status"; echo; }
# é»å¡ç­é¨ç½²è·å®ï¼æå¤ ~180sï¼ï¼tk_deploy_wait
tk_deploy_wait() {
  local max="${1:-180}" waited=0 s
  while :; do
    s="$(_tk_req GET "$ENGINE_BASE/admin/deploy/status" 2>/dev/null | python3 -c 'import sys,json
try:
 d=json.load(sys.stdin); r=d.get("last_result") or {}; print(("PENDING" if d.get("pending") else r.get("state","?"))+":"+str(r.get("exit","")))
except Exception: print("?")' 2>/dev/null)"
    echo "  deploy: $s (${waited}s)"
    case "$s" in done:0) return 0;; done:*) return 1;; esac
    [ "$waited" -ge "$max" ] && { echo "  â è¶æ¶"; return 1; }
    sleep 5; waited=$((waited+5))
  done
}

echo "tk.sh loaded Â· ENGINE_BASE=$ENGINE_BASE Â· å½æ°: tk_health tk_hot tk_write tk_read tk_ls tk_select tk_adapt tk_voice tk_video tk_visual tk_utm tk_slice tk_dryrun tk_schedule tk_promote tk_publish tk_state tk_kill tk_logdm tk_job tk_poll tk_wait tk_deploy tk_deploy_status tk_deploy_wait"
