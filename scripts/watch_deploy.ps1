# watch_deploy.ps1 - ENGINE HOST: resident deploy watcher (self-deploy tier).
# ===========================================================================
# Runs on: ENGINE HOST (resident loop). Maintained in the laptop git repo and
#       synced here via reset, but it RUNS on the engine host.
# What: watch runtime/admin_tasks/deploy.signal (written by Cowork's
#       POST /admin/deploy). On a signal, run deploy_ingestion.ps1
#       (fetch + reset --hard + rebuild + smoke) and write deploy.result so
#       Cowork's GET /admin/deploy/status can read the outcome.
# Flow: LAPTOP push -> Cowork tk_deploy -> ENGINE HOST auto-deploy -> Cowork
#       tk_deploy_wait.
#
# Start (ENGINE HOST, pick one):
#   foreground (debug): powershell -ExecutionPolicy Bypass -File D:\engine-host\taskon\engine\scripts\watch_deploy.ps1
#   resident: register as a Task Scheduler at-startup task (see end of file)
# ===========================================================================
param(
  [string]$EngineDir = "D:\engine-host\taskon\engine",
  [int]$PollSeconds = 5
)
$ErrorActionPreference = "Continue"
$RuntimeTasks = Join-Path $EngineDir "runtime\admin_tasks"
$Sig    = Join-Path $RuntimeTasks "deploy.signal"
$Result = Join-Path $RuntimeTasks "deploy.result"

Write-Host "[watch_deploy] started, watching $Sig (every ${PollSeconds}s)"
while ($true) {
  if (Test-Path $Sig) {
    $ref = "origin/main"
    try { $ref = (Get-Content $Sig -Raw | ConvertFrom-Json).ref } catch {}
    Remove-Item $Sig -Force -ErrorAction SilentlyContinue   # consume first, avoid re-trigger
    Write-Host "[watch_deploy] deploy signal ref=$ref, running deploy_ingestion.ps1 ..."

    # write an in-progress result so a polling Cowork sees "running"
    (@{ ref=$ref; state="running"; started_at=(Get-Date).ToString("o") } | ConvertTo-Json) |
      Set-Content -Path $Result -Encoding UTF8

    Push-Location $EngineDir
    $log = & powershell -ExecutionPolicy Bypass -File (Join-Path $EngineDir "scripts\deploy_ingestion.ps1") -Ref $ref 2>&1 | Out-String
    $code = $LASTEXITCODE
    Pop-Location

    $tail = if ($log.Length -gt 3000) { $log.Substring($log.Length - 3000) } else { $log }
    (@{ ref=$ref; state="done"; exit=$code; ran_at=(Get-Date).ToString("o"); tail=$tail } | ConvertTo-Json -Depth 4) |
      Set-Content -Path $Result -Encoding UTF8
    Write-Host "[watch_deploy] done exit=$code"
  }
  Start-Sleep -Seconds $PollSeconds
}

# ---------------------------------------------------------------------------
# ENGINE HOST one-time: register as an at-startup resident task:
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#         -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File D:\engine-host\taskon\engine\scripts\watch_deploy.ps1"
#   $t = New-ScheduledTaskTrigger -AtStartup
#   Register-ScheduledTask -TaskName "TaskOn-WatchDeploy" -Action $a -Trigger $t -RunLevel Highest
#   Start-ScheduledTask -TaskName "TaskOn-WatchDeploy"
# ---------------------------------------------------------------------------
