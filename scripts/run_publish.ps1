# ============================================================
# run_publish.ps1 · 阶段 5 + 6 UTM + MPT + 立即发布到 Postiz
# ============================================================
# 用法: .\scripts\run_publish.ps1 <piece_id> [target_url] [hook_type] [offset_minutes]
# 默认: target_url=https://taskon.xyz/free-diagnostic, offset_minutes=5
# 输出: utm_links.json + shorts_60s.mp4 + Postiz 3 平台真排程
# ============================================================

param(
    [Parameter(Mandatory=$true)][string]$PieceId,
    [string]$TargetUrl = "https://taskon.xyz/free-diagnostic",
    [string]$HookType = "47pct_bot",
    [int]$OffsetMinutes = 5
)

Set-Location D:\Taskon\marketing\engine
$ErrorActionPreference = "Continue"

$pieceDir = "D:\Taskon\marketing\engine\runtime\drafts\$PieceId"

Write-Host "`n=== piece: $PieceId ===" -ForegroundColor Cyan
Write-Host "    CTA URL:        $TargetUrl" -ForegroundColor Cyan
Write-Host "    Hook Type:      $HookType" -ForegroundColor Cyan
Write-Host "    Publish offset: now + $OffsetMinutes min" -ForegroundColor Cyan

Write-Host "`n[1/4] utm_generator: 5 平台 UTM + shlink 短链..." -ForegroundColor Yellow
docker compose exec engine python -m jobs.utm_generator `
    --piece-id $PieceId `
    --target-url $TargetUrl `
    --platforms twitter,linkedin,youtube `
    --accounts donald_en,taskon_official `
    --hook-type $HookType

if (Test-Path "$pieceDir\utm_links.json") {
    Write-Host "  ✓ utm_links.json: $((Get-Content "$pieceDir\utm_links.json" | ConvertFrom-Json).PSObject.Properties.Count) 条短链" -ForegroundColor Green
} else {
    Write-Host "  ✗ utm_links.json 未生成 · 中止" -ForegroundColor Red
    exit 1
}

Write-Host "`n[2/4] mpt_runner: 渲染 shorts_60s.mp4 (2-5 min)..." -ForegroundColor Yellow
docker compose exec engine python -m jobs.mpt_runner `
    --piece-id $PieceId `
    --voice zh-CN-YunxiNeural-Male `
    --timeout 900

if (Test-Path "$pieceDir\shorts_60s.mp4") {
    $mp4Size = [math]::Round((Get-Item "$pieceDir\shorts_60s.mp4").Length / 1MB, 1)
    Write-Host "  ✓ shorts_60s.mp4 · $mp4Size MB" -ForegroundColor Green
} else {
    Write-Host "  ⚠ shorts_60s.mp4 未生成 · YT Shorts 会自动 skip（不阻塞 LinkedIn）" -ForegroundColor Yellow
}

Write-Host "`n[3/4] publish_immediate: 绕过错峰，scheduled_at = now + $OffsetMinutes min（仅 LinkedIn + YT Shorts）..." -ForegroundColor Yellow
docker compose exec engine python -m scripts.publish_immediate `
    --piece-id $PieceId `
    --platforms linkedin_post,yt_shorts `
    --offset-minutes $OffsetMinutes

Write-Host "`n[4/4] publishings 表确认..." -ForegroundColor Yellow
docker compose exec engine python -c @"
from lib.db import db
rows = db.fetchall('SELECT platform,postiz_post_id,published_at,utm_campaign FROM publishings WHERE piece_id=? ORDER BY id DESC', ('$PieceId',))
for r in rows:
    print(f'  {r[\"platform\"]:<20} published_at={r[\"published_at\"] or \"(pending Postiz)\"} utm={r[\"utm_campaign\"] or \"-\"} postiz_id={r[\"postiz_post_id\"]}')
"@

Write-Host "`n=== ✓ 真排程完成 · 5min 后 Postiz 真发 ===" -ForegroundColor Green
Write-Host "下一步:" -ForegroundColor Magenta
Write-Host "  - 等 5min Postiz 真发到 LinkedIn + YouTube" -ForegroundColor Magenta
Write-Host "  - 你手发 X Thread（从 $pieceDir\xthread_final.md 复制）" -ForegroundColor Magenta
Write-Host "  - 30min 后跑 .\scripts\run_metrics.ps1 $PieceId" -ForegroundColor Magenta
