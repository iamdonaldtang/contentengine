# ============================================================
# daily-backup.ps1 · TaskOn engine daily backup
# ============================================================
# Backs up:
#   1. SQLite state.db (TaskOn engine · uses .backup safe command)
#   2. Pliven engine state.db (if exists)
#   3. Postiz Postgres dump (if docker container exists)
#   4. shlink Postgres dump (if docker container exists)
#
# Retention: 14 days (older files auto-deleted)
#
# Backup destination: D:\engine-host\infra\backups\
#
# Usage:
#   .\scripts\daily-backup.ps1                          # Run once
#   .\scripts\daily-backup.ps1 -InstallScheduledTask    # Register Scheduled Task (daily 03:00)
# ============================================================

param(
    [switch]$InstallScheduledTask
)

$ErrorActionPreference = "Continue"

# ---------- Path auto-adaptive ----------
if ($env:ENGINE_HOST_ROOT) {
    $HostRoot = $env:ENGINE_HOST_ROOT
} elseif (Test-Path "D:\engine-host") {
    $HostRoot = "D:\engine-host"
} else {
    Write-Error "Cannot locate D:\engine-host. This script must run on engine host."
    exit 1
}

$BackupDir = "$HostRoot\infra\backups"
$LogFile   = "$HostRoot\infra\logs\daily-backup.log"
$RetentionDays = 14

# ---------- Install · Scheduled Task ----------
if ($InstallScheduledTask) {
    $TaskName = "TaskOn-DailyBackup"
    $TaskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $TaskTrigger = New-ScheduledTaskTrigger -Daily -At "03:00"
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $TaskName `
        -Action $TaskAction -Trigger $TaskTrigger `
        -Settings $TaskSettings -Principal $TaskPrincipal `
        -Description "TaskOn engine daily backup (state.db + Postiz pg + 14d retention)" `
        -Force | Out-Null

    Write-Host "[OK] Scheduled Task registered: $TaskName" -ForegroundColor Green
    Write-Host "  Schedule: Daily 03:00 (SYSTEM account)"
    Write-Host "  View:     Get-ScheduledTask -TaskName $TaskName"
    Write-Host "  Remove:   Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
    exit 0
}

# ---------- Logging ----------
function Write-LogLine {
    param([string]$Severity, [string]$Message)
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Severity, $Message
    Write-Host $line
    $logDir = Split-Path $LogFile -Parent
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# ---------- Ensure backup dir ----------
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
}

$date = Get-Date -Format "yyyy-MM-dd"
$stamp = Get-Date -Format "yyyy-MM-dd-HHmmss"

Write-LogLine "INFO" "=== Daily backup started · $stamp ==="

$results = @()

# ---------- 1. TaskOn engine state.db ----------
$taskonStateBackup = "$BackupDir\taskon-state-$date.db"
try {
    # Use sqlite3 .backup command via docker exec (safe for WAL mode)
    docker exec -T taskon-engine sqlite3 /app/runtime/state.db ".backup '/tmp/state-backup.db'" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        docker cp taskon-engine:/tmp/state-backup.db $taskonStateBackup 2>&1 | Out-Null
        if (Test-Path $taskonStateBackup) {
            $sizeMB = [math]::Round((Get-Item $taskonStateBackup).Length / 1MB, 2)
            Write-LogLine "OK"  "TaskOn state.db backup -> $taskonStateBackup ($sizeMB MB)"
            $results += "TaskOn state.db: OK ($sizeMB MB)"
        } else {
            Write-LogLine "ERR" "TaskOn state.db copy failed (file not found after docker cp)"
            $results += "TaskOn state.db: FAIL (copy)"
        }
    } else {
        Write-LogLine "ERR" "TaskOn sqlite3 .backup failed"
        $results += "TaskOn state.db: FAIL (sqlite3)"
    }
} catch {
    Write-LogLine "ERR" "TaskOn state.db: $($_.Exception.Message)"
    $results += "TaskOn state.db: FAIL ($($_.Exception.Message))"
}

# ---------- 2. Pliven engine state.db (optional) ----------
$plivenStateBackup = "$BackupDir\pliven-state-$date.db"
$plivenContainer = (docker ps --filter "name=pliven-engine" --format "{{.Names}}" 2>$null) -join ""
if ($plivenContainer -match "pliven-engine") {
    try {
        docker exec -T pliven-engine sqlite3 /app/runtime/state.db ".backup '/tmp/state-backup.db'" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            docker cp pliven-engine:/tmp/state-backup.db $plivenStateBackup 2>&1 | Out-Null
            if (Test-Path $plivenStateBackup) {
                $sizeMB = [math]::Round((Get-Item $plivenStateBackup).Length / 1MB, 2)
                Write-LogLine "OK" "Pliven state.db backup -> $plivenStateBackup ($sizeMB MB)"
                $results += "Pliven state.db: OK ($sizeMB MB)"
            }
        }
    } catch {
        Write-LogLine "WARN" "Pliven state.db: $($_.Exception.Message) (non-fatal)"
        $results += "Pliven state.db: SKIP"
    }
} else {
    Write-LogLine "INFO" "Pliven engine container not found, skipping"
    $results += "Pliven state.db: SKIP (no container)"
}

# ---------- 3. Postiz Postgres dump ----------
# Try common Postiz PG container names
$postizPgContainer = $null
foreach ($name in @("postiz-postgres", "postiz-app-db-1", "postiz-app-postgres-1", "postiz-pg")) {
    $found = (docker ps --filter "name=$name" --format "{{.Names}}" 2>$null) -join ""
    if ($found -match $name) {
        $postizPgContainer = $found
        break
    }
}

if ($postizPgContainer) {
    $postizPgBackup = "$BackupDir\postiz-pg-$date.sql"
    try {
        docker exec -T $postizPgContainer pg_dumpall -U postgres > $postizPgBackup 2>&1
        if ($LASTEXITCODE -eq 0 -and (Test-Path $postizPgBackup)) {
            $sizeMB = [math]::Round((Get-Item $postizPgBackup).Length / 1MB, 2)
            Write-LogLine "OK" "Postiz pg dump -> $postizPgBackup ($sizeMB MB)"
            $results += "Postiz pg: OK ($sizeMB MB)"
        } else {
            Write-LogLine "ERR" "Postiz pg_dumpall failed"
            $results += "Postiz pg: FAIL"
        }
    } catch {
        Write-LogLine "ERR" "Postiz pg: $($_.Exception.Message)"
        $results += "Postiz pg: FAIL ($($_.Exception.Message))"
    }
} else {
    Write-LogLine "WARN" "Postiz Postgres container not found (tried postiz-postgres / postiz-app-db-1 / postiz-app-postgres-1 / postiz-pg)"
    $results += "Postiz pg: SKIP (no container)"
}

# ---------- 4. Cleanup · retention 14 days ----------
$cutoff = (Get-Date).AddDays(-$RetentionDays)
$cleaned = 0
foreach ($pattern in @("*.db", "*.sql", "*.rdb")) {
    Get-ChildItem $BackupDir -Filter $pattern -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object {
            Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
            $cleaned++
            Write-LogLine "INFO" "Cleaned old backup: $($_.Name)"
        }
}
if ($cleaned -gt 0) {
    $results += "Cleanup: $cleaned old files removed"
}

# ---------- Summary ----------
$summaryDir = (Get-ChildItem $BackupDir -ErrorAction SilentlyContinue | Measure-Object Length -Sum)
$totalMB = [math]::Round($summaryDir.Sum / 1MB, 2)
$totalCount = $summaryDir.Count

Write-LogLine "INFO" "=== Backup summary ==="
Write-LogLine "INFO" "  Total: $totalCount files, $totalMB MB"
foreach ($r in $results) {
    Write-LogLine "INFO" "  $r"
}
Write-LogLine "INFO" "=== Daily backup ended ==="
