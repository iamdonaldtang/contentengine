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

# ---------- Install · Scheduled Task (use schtasks.exe - more reliable than PowerShell Register-ScheduledTask) ----------
if ($InstallScheduledTask) {
    $TaskName = "TaskOn-DailyBackup"
    $taskCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""

    $schtasksArgs = @(
        "/create",
        "/SC", "DAILY",
        "/ST", "03:00",
        "/TN", $TaskName,
        "/TR", $taskCmd,
        "/RU", "SYSTEM",
        "/RL", "HIGHEST",
        "/F"
    )

    & schtasks.exe @schtasksArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[X] schtasks failed with exit code $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }

    Write-Host "[OK] Scheduled Task registered: $TaskName" -ForegroundColor Green
    Write-Host "  Schedule: Daily 03:00 (SYSTEM account, HIGHEST RL)"
    Write-Host "  View:     schtasks /query /TN $TaskName"
    Write-Host "  Remove:   schtasks /delete /TN $TaskName /F"
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
    # Strategy: Try Python's sqlite3.backup() inside container (safest, handles WAL).
    # Fallback to docker cp (file copy, WAL slightly less safe but works).
    $usedPython = $false
    $pythonScript = "import sqlite3; src=sqlite3.connect('/app/runtime/state.db'); dst=sqlite3.connect('/tmp/state-backup.db'); src.backup(dst); src.close(); dst.close()"
    docker exec -T taskon-engine python -c $pythonScript 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $usedPython = $true
        docker cp taskon-engine:/tmp/state-backup.db $taskonStateBackup 2>&1 | Out-Null
        docker exec -T taskon-engine rm -f /tmp/state-backup.db 2>&1 | Out-Null
    } else {
        # Fallback: docker cp state.db directly (less safe with WAL but works)
        Write-LogLine "WARN" "Python sqlite3.backup failed, fallback to docker cp"
        docker cp taskon-engine:/app/runtime/state.db $taskonStateBackup 2>&1 | Out-Null
    }

    if (Test-Path $taskonStateBackup) {
        $sizeMB = [math]::Round((Get-Item $taskonStateBackup).Length / 1MB, 2)
        $method = if ($usedPython) { "python-backup" } else { "docker-cp" }
        Write-LogLine "OK"  "TaskOn state.db backup -> $taskonStateBackup ($sizeMB MB, method=$method)"
        $results += "TaskOn state.db: OK ($sizeMB MB)"
    } else {
        Write-LogLine "ERR" "TaskOn state.db backup file not created"
        $results += "TaskOn state.db: FAIL"
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
        # Postiz default credentials per docker-compose.yaml.
        # Try to auto-detect from container env first; fallback to known defaults.
        # NOTE: For security, future improvement: read from PASS_FILE or vault.
        $pgUser = "$(docker exec -T $postizPgContainer printenv POSTGRES_USER 2>$null)".Trim()
        if (-not $pgUser) { $pgUser = "postiz-user" }       # Postiz fork default

        $pgDb = "$(docker exec -T $postizPgContainer printenv POSTGRES_DB 2>$null)".Trim()
        if (-not $pgDb) { $pgDb = "postiz-db-local" }       # Postiz fork default

        $pgPass = "$(docker exec -T $postizPgContainer printenv POSTGRES_PASSWORD 2>$null)".Trim()
        if (-not $pgPass) { $pgPass = "postiz-password" }   # Postiz fork default

        Write-LogLine "INFO" "Postiz pg dump · user=$pgUser db=$pgDb container=$postizPgContainer"

        # pg_dump with PGPASSWORD env (passed via docker exec -e)
        docker exec -T -e "PGPASSWORD=$pgPass" $postizPgContainer pg_dump -U $pgUser -d $pgDb > $postizPgBackup 2>&1

        if ($LASTEXITCODE -eq 0 -and (Test-Path $postizPgBackup) -and (Get-Item $postizPgBackup).Length -gt 0) {
            $sizeMB = [math]::Round((Get-Item $postizPgBackup).Length / 1MB, 2)
            Write-LogLine "OK" "Postiz pg_dump -> $postizPgBackup ($sizeMB MB)"
            $results += "Postiz pg: OK ($sizeMB MB)"
        } else {
            Write-LogLine "ERR" "Postiz pg_dump failed (user=$pgUser db=$pgDb)"
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
