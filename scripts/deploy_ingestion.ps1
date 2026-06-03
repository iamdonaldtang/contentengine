# deploy_ingestion.ps1 · ingestion 唯一部署入口（重建 + 强制冒烟闸门）
# ===========================================================================
# 目的：把「拉代码 → 重建 → 端到端冒烟」焊成一条命令，让冒烟**不可能被跳过**。
#       从此引擎机部署 ingestion 只跑这一条，**不要再裸 `docker compose up --build`**
#       —— 裸跑会跳过冒烟，正是我们踩过的 COPY-CACHED 静默没生效 / 跑旧代码的坑。
#
# 用法（引擎机 D:\engine-host\taskon\engine）：
#   .\scripts\deploy_ingestion.ps1
#   .\scripts\deploy_ingestion.ps1 -EngineBase http://engine:5051   # 走内网代替公网
#
# 退出码：0 = 部署且冒烟全过；非0 = 任一闸门失败，视为部署未通过，需排查。
# ===========================================================================
[CmdletBinding()]
param(
  [string]$EngineBase = "https://ingest.taskon.xyz",
  [string]$Ref = "origin/main"
)
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."   # 切到 engine 仓根

Write-Host "== 1/5 拉取最新代码 ($Ref) =="
git fetch origin
git checkout $Ref -- ingestion/    # 取 ingestion 全量（admin_routes / app / media_routes ...）

Write-Host "== 2/5 重建 ingestion 容器 =="
docker compose up -d --build ingestion

Write-Host "== 3/5 等待并探一眼容器内代码 =="
Start-Sleep -Seconds 4
try {
  $probe = docker compose exec -T ingestion python -c "import ingestion.admin_routes as a; print('NEW' if hasattr(a,'admin_upload_asset') else 'OLD')" 2>$null
  Write-Host "  容器 admin_routes: $probe（仅参考；最终以冒烟为准）"
} catch { Write-Host "  （容器内探测跳过）" }

Write-Host "== 4/5 端到端冒烟（强制闸门）=="
$tokenLine = Select-String -Path .env -Pattern '^ADMIN_API_TOKEN=' | Select-Object -First 1
if (-not $tokenLine) { Write-Error ".env 缺 ADMIN_API_TOKEN，无法冒烟"; exit 1 }
$token = ($tokenLine.Line -replace '^ADMIN_API_TOKEN=', '').Trim()

$bash = Join-Path $env:ProgramFiles "Git\bin\bash.exe"
if (-not (Test-Path $bash)) { $bash = "bash" }   # 退回 PATH 上的 bash（git-bash / wsl）

$env:ADMIN_API_TOKEN = $token
$env:ENGINE_BASE = $EngineBase
& $bash "scripts/smoke_httpfirst.sh"
$code = $LASTEXITCODE
$env:ADMIN_API_TOKEN = $null   # 跑完即清，不留在会话环境里

Write-Host "== 5/5 结果 =="
if ($code -ne 0) {
  Write-Host "🔴 冒烟失败 (exit=$code) —— 部署视为未通过。" -ForegroundColor Red
  Write-Host "   排查：看上面 ✗ 行；多半是容器跑旧代码 / token 失效 / 某 job 挂。" -ForegroundColor Red
  Write-Host "   参考 docs/HTTP-first_方案A_部署与13步映射_v1.md §5.5(四层防线) + §8(部署链坑)。" -ForegroundColor Red
  exit $code
}
Write-Host "🟢 部署 + 冒烟全过，ingestion 上线确认。" -ForegroundColor Green
