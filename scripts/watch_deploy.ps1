# watch_deploy.ps1 · 引擎机常驻部署 watcher（第1档自动化 · 2026-06-03）
# ===========================================================================
# 运行位置：⚙️ 引擎机（不是笔记本！）。文件本身在笔记本 git 仓里维护，随 reset 同步到引擎机，
#           但**在引擎机上常驻运行**。
# 作用：监听 runtime/admin_tasks/deploy.signal（由 Cowork 的 POST /admin/deploy 写入），
#       一旦出现就跑 deploy_ingestion.ps1（fetch + reset --hard + rebuild + 冒烟），
#       把结果写回 deploy.result 供 Cowork 的 GET /admin/deploy/status 读取。
# 于是：🖥️笔记本 push → Cowork tk_deploy → ⚙️引擎机自动部署 → Cowork tk_deploy_status 看结果。
#
# 启动（引擎机 · 二选一）：
#   前台调试： powershell -ExecutionPolicy Bypass -File D:\engine-host\taskon\engine\scripts\watch_deploy.ps1
#   常驻：     注册成 Task Scheduler 开机任务（见文件末尾命令），或塞进现有 watch_tunnel_health 巡检
# ===========================================================================
param(
  [string]$EngineDir = "D:\engine-host\taskon\engine",
  [int]$PollSeconds = 5
)
$ErrorActionPreference = "Continue"
$RuntimeTasks = Join-Path $EngineDir "runtime\admin_tasks"
$Sig    = Join-Path $RuntimeTasks "deploy.signal"
$Result = Join-Path $RuntimeTasks "deploy.result"

Write-Host "[watch_deploy] 启动，监听 $Sig（每 ${PollSeconds}s）"
while ($true) {
  if (Test-Path $Sig) {
    $ref = "origin/main"
    try { $ref = (Get-Content $Sig -Raw | ConvertFrom-Json).ref } catch {}
    Remove-Item $Sig -Force -ErrorAction SilentlyContinue   # 先消费，避免重复触发
    Write-Host "[watch_deploy] 收到部署信号 ref=$ref，开始 deploy_ingestion.ps1 ..."

    # 写一个 in-progress 结果，让 Cowork 轮询时能看到"进行中"
    (@{ ref=$ref; state="running"; started_at=(Get-Date).ToString("o") } | ConvertTo-Json) |
      Set-Content -Path $Result -Encoding UTF8

    Push-Location $EngineDir
    $log = & powershell -ExecutionPolicy Bypass -File (Join-Path $EngineDir "scripts\deploy_ingestion.ps1") -Ref $ref 2>&1 | Out-String
    $code = $LASTEXITCODE
    Pop-Location

    $tail = if ($log.Length -gt 3000) { $log.Substring($log.Length - 3000) } else { $log }
    (@{ ref=$ref; state="done"; exit=$code; ran_at=(Get-Date).ToString("o"); tail=$tail } | ConvertTo-Json -Depth 4) |
      Set-Content -Path $Result -Encoding UTF8
    Write-Host "[watch_deploy] 完成 exit=$code"
  }
  Start-Sleep -Seconds $PollSeconds
}

# ---------------------------------------------------------------------------
# ⚙️引擎机 一次性注册成开机常驻任务：
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#         -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File D:\engine-host\taskon\engine\scripts\watch_deploy.ps1"
#   $t = New-ScheduledTaskTrigger -AtStartup
#   Register-ScheduledTask -TaskName "TaskOn-WatchDeploy" -Action $a -Trigger $t -RunLevel Highest
#   Start-ScheduledTask -TaskName "TaskOn-WatchDeploy"
# ---------------------------------------------------------------------------
