# ============================================================
# watch_tunnel_health.ps1 · V1 本地 + CF Tunnel 健康哨兵
# ============================================================
# 用途: 每 5 分钟巡检 cloudflared service + 公网入口存活性,挂了发 Lark P0 告警
#
# 监控 3 层:
#   L1 · cloudflared Windows service (Get-Service)
#   L2 · 公网 ingest.taskon.xyz/health 可达 + 200
#   L3 · 公网 l.taskon.xyz/rest/v3/health 可达 + 200
#
# 任一层挂掉 → 立即 Lark 告警 + 写 state 文件 (避免每 5min 重复轰炸告警)
# 恢复正常后 → 发"已恢复" 通知
#
# 用法:
#   交互测试:  .\scripts\watch_tunnel_health.ps1
#   注册为 Scheduled Task: .\scripts\watch_tunnel_health.ps1 -InstallScheduledTask
# ============================================================

param(
    [switch]$InstallScheduledTask
)

$ErrorActionPreference = "Continue"

# 路径自适应 · 优先环境变量 → 从脚本位置推导 → fallback 主笔记本默认
if ($env:ENGINE_ROOT) {
    $EngineRoot = $env:ENGINE_ROOT
} elseif ($PSScriptRoot -and (Test-Path "$PSScriptRoot\..\runtime")) {
    # 脚本在 <engine_root>/scripts/ 下，runtime 在 <engine_root>/runtime/
    $EngineRoot = (Resolve-Path "$PSScriptRoot\..").Path
} elseif (Test-Path "D:\engine-host\taskon\engine\runtime") {
    # 引擎机默认路径
    $EngineRoot = "D:\engine-host\taskon\engine"
} else {
    # 主笔记本默认路径
    $EngineRoot = "D:\Taskon\marketing\engine"
}

$StateFile  = "$EngineRoot\runtime\tunnel_health_state.txt"
$LogFile    = "$EngineRoot\runtime\logs\tunnel_health.log"

# ---------- Install · Scheduled Task ----------
if ($InstallScheduledTask) {
    $TaskName = "TaskOn-TunnelHealth"
    $TaskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $TaskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $TaskName `
        -Action $TaskAction -Trigger $TaskTrigger `
        -Settings $TaskSettings -Principal $TaskPrincipal `
        -Description "TaskOn V1 · cloudflared + ingest + shlink 健康哨兵 (5min 间隔)" `
        -Force | Out-Null

    Write-Host "✓ Scheduled Task 已注册: $TaskName" -ForegroundColor Green
    Write-Host "  每 5min 自动跑 · SYSTEM 账号 · 开机自启"
    Write-Host "  查看: Get-ScheduledTask -TaskName $TaskName"
    Write-Host "  删除: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
    exit 0
}

# ---------- 主巡检逻辑 ----------

# 加载 .env 读 LARK_WEBHOOK_URL
$LarkWebhook = $null
$EnvPath = "$EngineRoot\.env"
if (Test-Path $EnvPath) {
    Get-Content $EnvPath | ForEach-Object {
        if ($_ -match '^\s*LARK_WEBHOOK_URL\s*=\s*(.+?)\s*$') {
            $LarkWebhook = $matches[1].Trim('"').Trim("'")
        }
    }
}

function Write-LogLine {
    param([string]$Severity, [string]$Message)
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Severity, $Message
    Write-Host $line
    $logDir = Split-Path $LogFile -Parent
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Send-LarkAlert {
    param([string]$Severity, [string]$Title, [hashtable]$Details)
    if (-not $LarkWebhook) {
        Write-LogLine "WARN" "LARK_WEBHOOK_URL 未配置 · 跳过告警"
        return
    }
    $color = switch ($Severity) {
        "P0" { "red" }
        "P1" { "yellow" }
        default { "grey" }
    }
    $detailLines = $Details.GetEnumerator() | ForEach-Object { "**$($_.Key)**: $($_.Value)" }
    $detailMd = ($detailLines -join "`n")
    $body = @{
        msg_type = "interactive"
        card = @{
            header = @{
                title = @{ tag = "plain_text"; content = "[$Severity] $Title" }
                template = $color
            }
            elements = @(
                @{ tag = "div"; text = @{ tag = "lark_md"; content = $detailMd } }
                @{ tag = "div"; text = @{ tag = "lark_md"; content = "_TaskOn 告警 · alert · engine · notification · tunnel-watcher_" } }
            )
        }
    }
    $json = $body | ConvertTo-Json -Depth 6 -Compress
    try {
        $r = Invoke-RestMethod -Uri $LarkWebhook -Method Post -Body $json -ContentType "application/json" -TimeoutSec 10
        Write-LogLine "INFO" "Lark sent · code=$($r.code) msg=$($r.msg)"
    } catch {
        Write-LogLine "ERR" "Lark post failed: $($_.Exception.Message)"
    }
}

# ---------- 3 层巡检 ----------
$failures = @()

# L1 · cloudflared service
try {
    $svc = Get-Service cloudflared -ErrorAction Stop
    if ($svc.Status -ne "Running") {
        $failures += "L1 · cloudflared service is $($svc.Status) (expected Running)"
    }
} catch {
    $failures += "L1 · cloudflared service not found · $($_.Exception.Message)"
}

# L2 · ingest.taskon.xyz/health
try {
    $r = Invoke-WebRequest -Uri "https://ingest.taskon.xyz/health" -Method Get -TimeoutSec 8 -UseBasicParsing
    if ($r.StatusCode -ne 200) {
        $failures += "L2 · ingest.taskon.xyz/health returned $($r.StatusCode)"
    } elseif ($r.Content -notmatch '"status"\s*:\s*"ok"') {
        $failures += "L2 · ingest.taskon.xyz/health body unexpected: $($r.Content.Substring(0, [Math]::Min(120, $r.Content.Length)))"
    }
} catch {
    $failures += "L2 · ingest.taskon.xyz unreachable · $($_.Exception.Message)"
}

# L3 · l.taskon.xyz/rest/v3/health
try {
    $r = Invoke-WebRequest -Uri "https://l.taskon.xyz/rest/v3/health" -Method Get -TimeoutSec 8 -UseBasicParsing
    if ($r.StatusCode -ne 200) {
        $failures += "L3 · l.taskon.xyz health returned $($r.StatusCode)"
    }
} catch {
    $failures += "L3 · l.taskon.xyz unreachable · $($_.Exception.Message)"
}

# ---------- 状态机 · 避免每 5min 重复告警 ----------
$currentState = if ($failures.Count -eq 0) { "ok" } else { "fail" }
$prevState = if (Test-Path $StateFile) { (Get-Content $StateFile -Raw).Trim() } else { "ok" }

if ($currentState -ne $prevState) {
    # 状态切换 · 必发告警
    if ($currentState -eq "fail") {
        Write-LogLine "ERR" "状态切换: ok → fail · failures=$($failures.Count)"
        Send-LarkAlert "P0" "TaskOn V1 Tunnel/Ingest DOWN" @{
            "Failures"    = ($failures -join " | ")
            "Host"        = $env:COMPUTERNAME
            "Time"        = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
            "Next Action" = "在桌面检查 docker compose ps + Get-Service cloudflared · 必要时重启"
        }
    } else {
        Write-LogLine "INFO" "状态切换: fail → ok · 已恢复"
        Send-LarkAlert "P2" "TaskOn V1 Tunnel/Ingest RECOVERED" @{
            "Host" = $env:COMPUTERNAME
            "Time" = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
            "Note" = "所有 3 层 (cloudflared service / ingest.taskon.xyz / l.taskon.xyz) 已恢复 200"
        }
    }
    Set-Content -Path $StateFile -Value $currentState -Encoding UTF8
} else {
    # 状态稳定 · 仅写日志不告警
    if ($currentState -eq "ok") {
        Write-LogLine "INFO" "OK · 3 层巡检全通"
    } else {
        Write-LogLine "WARN" "持续 fail · 跳过告警 (避免轰炸) · failures=$($failures -join ' | ')"
    }
}

# ---------- 输出 (交互模式可见) ----------
Write-Host ""
Write-Host "═══ TaskOn V1 Tunnel Health · $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ═══" -ForegroundColor Cyan
if ($currentState -eq "ok") {
    Write-Host "  L1 cloudflared service   ✓" -ForegroundColor Green
    Write-Host "  L2 ingest.taskon.xyz     ✓" -ForegroundColor Green
    Write-Host "  L3 l.taskon.xyz          ✓" -ForegroundColor Green
} else {
    foreach ($f in $failures) {
        Write-Host "  ✗ $f" -ForegroundColor Red
    }
}
Write-Host "  状态: $prevState → $currentState  · 日志: $LogFile"
Write-Host ""
