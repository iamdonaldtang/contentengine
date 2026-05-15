#!/usr/bin/env bash
# Container entrypoint:
#   1. Print effective config (with secrets redacted) for `docker logs` visibility
#   2. Ensure runtime/ subdirs exist (volume might be empty on first run)
#   3. Initialize / verify the SQLite schema (idempotent)
#   4. Exec the supplied command (supercronic by default)
set -euo pipefail

echo "=========================================================="
echo " TaskOn Marketing Engine container"
echo " Boot at  : $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo " ENGINE   : ${ENGINE_ROOT:-/app}"
echo " DB       : ${SQLITE_PATH:-/app/runtime/state.db}"
echo " TZ       : ${TZ:-Asia/Shanghai}"
echo " LLM      : MiniMaxi primary @ ${MINIMAXI_BASE_URL:-(empty!)}"
echo "          : Anthropic fallback model = ${ANTHROPIC_FALLBACK_MODEL:-claude-opus-4-7}"
echo " ALERTS   : ENABLE_LARK_ALERTS=${ENABLE_LARK_ALERTS:-true}"
echo "          : DRY_RUN=${DRY_RUN:-false}"
echo "=========================================================="

# ---- 1. Ensure runtime dirs ----
mkdir -p /app/runtime/logs /app/runtime/drafts /app/runtime/backups

# ---- 2. Light pre-flight checks (non-fatal, just warn) ----
_minimaxi_any="${MINIMAXI_API_KEY:-}${MINIMAXI_API_KEY_1:-}${MINIMAXI_API_KEY_2:-}${MINIMAXI_API_KEY_3:-}${MINIMAX_API_KEY:-}${MINIMAX_API_KEY_1:-}${MINIMAX_API_KEY_2:-}${MINIMAX_API_KEY_3:-}"
if [[ -z "${_minimaxi_any}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "WARNING: no LLM keys configured (MiniMaxi/MiniMax variants + Anthropic all empty)."
    echo "         Container will start but LLM-dependent jobs will P1-alert and exit non-zero."
fi
unset _minimaxi_any
if [[ -z "${LARK_WEBHOOK_URL:-}" ]]; then
    echo "WARNING: LARK_WEBHOOK_URL is empty — P0/P1/P2 alerts will only be logged."
fi
if [[ -z "${POSTIZ_API_KEY:-}" ]]; then
    echo "WARNING: POSTIZ_API_KEY empty — metrics_collector will skip Postiz source."
fi

# Twikit cookie pool sanity (KOL Watch §2.10 fallback path).
_twikit_pool_path="${TWIKIT_ACCOUNTS_PATH:-}"
if [[ -n "${_twikit_pool_path}" && -f "${_twikit_pool_path}" ]]; then
    # Count top-level JSON array entries — count "username" keys as a proxy.
    _twikit_pool_size=$(grep -c '"username"' "${_twikit_pool_path}" || true)
    if [[ "${_twikit_pool_size}" -lt 3 ]]; then
        echo "WARNING: Twikit pool at ${_twikit_pool_path} has only ${_twikit_pool_size} account(s) — recommend >= 3 for rotation."
    else
        echo "INFO: Twikit pool loaded with ${_twikit_pool_size} accounts."
    fi
elif [[ -n "${TWIKIT_USERNAME:-}" && -n "${TWIKIT_PASSWORD:-}" ]]; then
    echo "INFO: Twikit single-account mode (no pool configured)."
else
    echo "WARNING: KOL Watch fallback path disabled — no TWIKIT_ACCOUNTS_PATH and no TWIKIT_USERNAME/PASSWORD set. Only X API primary will be used."
fi
unset _twikit_pool_path _twikit_pool_size

# ---- 3. Initialize / verify SQLite schema ----
echo ">>> init_db (idempotent)..."
python -m scripts.init_db
echo ">>> init_db done."

# ---- 4. Hand off to supercronic (or override via `docker compose run`) ----
echo ">>> exec: $*"
exec "$@"
