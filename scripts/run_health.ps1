# ============================================================
# run_health.ps1 · 阶段 0 健康检查
# ============================================================
# 用法: cd D:\Taskon\marketing\engine; .\scripts\run_health.ps1
# 输出: 4 docker 容器健康 + 18 cron 心跳 + env 关键键
# ============================================================

Set-Location D:\Taskon\marketing\engine
$ErrorActionPreference = "Continue"

Write-Host "`n=== [1/5] engine + ingestion 容器健康 ===" -ForegroundColor Cyan
docker compose ps

Write-Host "`n=== [2/5] Postiz / MPT / shlink 容器健康 ===" -ForegroundColor Cyan
docker ps --format "table {{.Names}}\t{{.Status}}" | Select-String -Pattern "postiz|moneyprinter|shlink|engine|ingestion"

Write-Host "`n=== [3/5] ingestion HTTP /health ===" -ForegroundColor Cyan
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:5051/health" -TimeoutSec 5
    Write-Host "ingestion: OK · $($r | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch { Write-Host "ingestion: 不可达 · $($_.Exception.Message)" -ForegroundColor Red }

Write-Host "`n=== [4/5] LLM 通信测试 ===" -ForegroundColor Cyan
docker compose exec engine python -c "from lib.llm_client import llm; print('LLM:', llm.complete('test','say hi in 3 chars'))" 2>&1

Write-Host "`n=== [5/5] env check ===" -ForegroundColor Cyan
$envCheck = @'
for k in POSTIZ_API_KEY X_BEARER_TOKEN SHLINK_API_KEY LARK_WEBHOOK_URL MINIMAX_API_KEY TWIKIT_USERNAME TWIKIT_PASSWORD; do
  v=$(printenv $k)
  if [ -n "$v" ]; then
    echo "  [OK]  $k=<set, ${#v} chars>"
  else
    echo "  [!!]  $k=<MISSING>"
  fi
done
'@
docker compose exec engine sh -c $envCheck

Write-Host "`n=== heartbeat last 10 jobs ===" -ForegroundColor Cyan
$heartbeatQuery = @'
from lib.db import db
rows = db.fetchall("SELECT job_name, last_run_at, status FROM heartbeat ORDER BY last_run_at DESC LIMIT 10")
for r in rows:
    print(f"  {r['job_name']:<28} status={r['status']:<8} last={r['last_run_at']}")
'@
docker compose exec engine python -c $heartbeatQuery

Write-Host "`n=== 完成 ===" -ForegroundColor Green
