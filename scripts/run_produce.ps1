# ============================================================
# run_produce.ps1 · 阶段 3 多平台适配 + Voice 自检
# ============================================================
# 用法: .\scripts\run_produce.ps1 <piece_id>
# 前置: xthread_final.md 已落到 runtime/drafts/<piece_id>/
# 输出: 4 平台 .md + voice_report*.md + state=drafted
# ============================================================

param(
    [Parameter(Mandatory=$true)][string]$PieceId
)

Set-Location D:\Taskon\marketing\engine
$ErrorActionPreference = "Continue"

$pieceDir = "D:\Taskon\marketing\engine\runtime\drafts\$PieceId"

if (-not (Test-Path "$pieceDir\xthread_final.md")) {
    Write-Host "✗ $pieceDir\xthread_final.md 不存在 · 先让 Cowork 起 X 主稿" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "$pieceDir\selection_card.yaml")) {
    Write-Host "✗ $pieceDir\selection_card.yaml 不存在 · 先让 Cowork 写选题卡" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== piece: $PieceId ===" -ForegroundColor Cyan

Write-Host "`n[1/3] adapter_orchestrator: 1 → 4 平台改写 + 自动跑 voice_checker..." -ForegroundColor Yellow
docker compose exec engine python -m jobs.adapter_orchestrator --piece-id $PieceId

Write-Host "`n[2/3] 总览产出文件..." -ForegroundColor Yellow
Get-ChildItem $pieceDir | Sort-Object Name | Format-Table Name,Length,LastWriteTime -AutoSize

Write-Host "`n[3/3] voice_report 汇总（4 平台禁词 + 长度 + CTA 占比）..." -ForegroundColor Yellow
Get-ChildItem "$pieceDir\voice_report*.md" | ForEach-Object {
    Write-Host "`n--- $($_.Name) ---" -ForegroundColor Magenta
    Get-Content $_.FullName
}

Write-Host "`n=== ✓ 产线完成 · 下一步: Cowork 跑 critic plugin + brand-review skill ===" -ForegroundColor Green
