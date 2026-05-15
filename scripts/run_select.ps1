# ============================================================
# run_select.ps1 · 阶段 1 双路选题（engine + crypto-news-aggregator 并跑）
# ============================================================
# 用法: .\scripts\run_select.ps1 [WeekTag]
# 默认 WeekTag = 当前 ISO 周 (如 2026W20)
# 输出: runtime/selection_<week>.md (Top10) + candidates 表 ≥10 条
# ============================================================

param(
    [string]$WeekTag = $null
)

Set-Location D:\Taskon\marketing\engine
$ErrorActionPreference = "Continue"

# 自动算 ISO 周 (PS 5.1 兼容: 不用 [ISOWeek],用 -UFormat %V)
if (-not $WeekTag) {
    $now = Get-Date
    $iso = [int](Get-Date $now -UFormat %V)
    $WeekTag = "{0}W{1:D2}" -f $now.Year, $iso
}
Write-Host "`n=== 选题周: $WeekTag ===" -ForegroundColor Cyan

Write-Host "`n[1/3] 跑 kol_watch（X API + Twikit fallback）..." -ForegroundColor Yellow
docker compose exec engine python -m jobs.kol_watch --week $WeekTag

Write-Host "`n[2/3] 跑 topic_ranker（5 维评分 + 历史回流调权）..." -ForegroundColor Yellow
docker compose exec engine python -m jobs.topic_ranker --week $WeekTag

Write-Host "`n[3/3] 看选题报告 selection_$WeekTag.md ..." -ForegroundColor Yellow
$selectionFile = "D:\Taskon\marketing\engine\runtime\selection_$WeekTag.md"
if (Test-Path $selectionFile) {
    Get-Content $selectionFile
    Write-Host "`n=== ✓ 选题完成 · 报告路径: $selectionFile ===" -ForegroundColor Green
    Write-Host "下一步: 把 Top10 念给 Cowork，由 Donald 5min 拍板 1 条" -ForegroundColor Magenta
} else {
    Write-Host "`n=== ✗ selection_$WeekTag.md 未生成，检查 docker logs engine ===" -ForegroundColor Red
}

# candidate stats display skipped - PS quote-escape issue with inline Python.
# To see candidates: docker compose exec engine sqlite3 /app/runtime/engine.db "SELECT source_route,status,count(*) FROM candidates GROUP BY source_route,status"
Write-Host "`n=== candidates stats skipped ===" -ForegroundColor DarkGray
