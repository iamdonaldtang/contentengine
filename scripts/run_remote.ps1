<#
.SYNOPSIS
TaskOn engine remote command wrapper - call engine on remote host from main laptop.

.DESCRIPTION
Original command (under v3 architecture, main laptop calls engine on remote host) is long:

  ssh donald@engine "cd D:/engine-host/taskon/engine && docker compose exec -T ${EngineService} python -m jobs.adapter_orchestrator --piece-id 20260602-01"

Wrapped:

  .\run_remote.ps1 -Job adapter_orchestrator -PieceId 20260602-01

Dependencies (one-time setup, then permanent):
1. Tailscale connected on both machines (main laptop + engine host, same account)
2. Engine host has Windows OpenSSH Server installed and sshd service running
3. Main laptop SSH key configured in engine host administrators_authorized_keys (passwordless)
4. SMB share \\engine\runtime mounted as Z: (let Cowork read/write file contracts)

See: D:\TaskOn\infra\enable_tailscale_ssh_and_smb.md (one-time setup SOP)

5 Action modes (mutually exclusive):

  -Job       <name>      Run engine python -m jobs.<name>, with -PieceId / -ExtraArgs
  -Script    <name>      Run engine python -m scripts.<name> (e.g. validate_selection)
  -Sqlite    <SQL>       Run docker compose exec engine sqlite3 /app/runtime/state.db "<SQL>"
  -Compose   <subcmd>    Run docker compose <subcmd> (e.g. ps / logs / restart)
  -Raw       <cmd>       Run command as-is, no cd or docker compose exec wrapping

.PARAMETER PieceId
piece_id, used with -Job / -Script. Auto-converted to --piece-id <id> argument.

.PARAMETER ExtraArgs
Extra args appended to command as-is.

.PARAMETER RemoteHost
Tailscale remote hostname, default: engine

.PARAMETER RemoteUser
SSH remote username, default: donald

.PARAMETER RemoteEngineDir
Absolute path of engine project on remote host, default: D:/engine-host/taskon/engine
Override via env var REMOTE_ENGINE_DIR or -RemoteEngineDir parameter.

.PARAMETER Local
Force local execution (skip SSH). Use when script runs on engine host itself.
Auto-detected: if local has docker compose and engine service running, auto-switches to Local mode.

.PARAMETER DryRun
Print command only, don't execute. Useful for inspecting equivalent commands.

.EXAMPLE
.\run_remote.ps1 -Job adapter_orchestrator -PieceId 20260602-01
# 4-platform fan-out

.EXAMPLE
.\run_remote.ps1 -Job schedule_planner -PieceId 20260602-01 -ExtraArgs "--dry-run"

.EXAMPLE
.\run_remote.ps1 -Script validate_selection -PieceId 20260602-01

.EXAMPLE
.\run_remote.ps1 -Sqlite "SELECT id,email FROM leads ORDER BY id DESC LIMIT 5"

.EXAMPLE
.\run_remote.ps1 -Compose "ps"

.EXAMPLE
.\run_remote.ps1 -Compose "logs --tail 50 engine"

.EXAMPLE
.\run_remote.ps1 -Raw "Get-Date"

.EXAMPLE
.\run_remote.ps1 -Job adapter_orchestrator -PieceId 20260602-01 -DryRun
# Print only, don't run

.NOTES
Used together with v3 collaboration model - standard entrypoint for main laptop calling engine host.
Designed to be idempotent + path-adaptive.
#>

[CmdletBinding(DefaultParameterSetName='Job')]
param(
    [Parameter(Mandatory=$true, ParameterSetName='Job')]
    [string]$Job,

    [Parameter(Mandatory=$true, ParameterSetName='Script')]
    [string]$Script,

    [Parameter(Mandatory=$true, ParameterSetName='Sqlite')]
    [string]$Sqlite,

    [Parameter(Mandatory=$true, ParameterSetName='Compose')]
    [string]$Compose,

    [Parameter(Mandatory=$true, ParameterSetName='Raw')]
    [string]$Raw,

    [Parameter(ParameterSetName='Job')]
    [Parameter(ParameterSetName='Script')]
    [string]$PieceId = '',

    [Parameter(ParameterSetName='Job')]
    [Parameter(ParameterSetName='Script')]
    [Parameter(ParameterSetName='Compose')]
    [string]$ExtraArgs = '',

    [string]$RemoteHost = 'engine',
    [string]$RemoteUser = 'donald',
    [string]$RemoteEngineDir = $(if ($env:REMOTE_ENGINE_DIR) { $env:REMOTE_ENGINE_DIR } else { 'D:/engine-host/taskon/engine' }),

    # docker compose 服务名（不是容器显示名，也不是 Tailscale 主机名）。
    # 2026-06-03 起 TaskOn/Pliven 共存，`docker compose config --services` 实测为
    # taskon-engine（原 engine）。可用 -EngineService 或 $env:ENGINE_SERVICE 覆盖。
    [string]$EngineService = $(if ($env:ENGINE_SERVICE) { $env:ENGINE_SERVICE } else { 'taskon-engine' }),

    [switch]$Local,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ============================================================================
# Auto-detect: if local has engine docker running, switch to Local mode
# ============================================================================
function Test-LocalEngine {
    try {
        $dockerCheck = docker ps --filter "name=taskon-engine" --format "{{.Names}}" 2>$null
        if ($dockerCheck -match "taskon-engine") {
            return $true
        }
    } catch {
        # docker not in PATH or not installed
    }
    return $false
}

if (-not $Local -and (Test-LocalEngine)) {
    Write-Host "[info] Detected local taskon-engine container, auto-switching to Local mode" -ForegroundColor DarkGray
    $Local = $true
}

# ============================================================================
# Build inner command (cd + docker compose exec subcommand)
# ============================================================================
function Build-InnerCommand {
    param([string]$ParameterSet)

    $pieceArg = if ($PieceId) { "--piece-id $PieceId" } else { '' }

    switch ($ParameterSet) {
        'Job' {
            return "docker compose exec -T ${EngineService} python -m jobs.$Job $pieceArg $ExtraArgs".Trim()
        }
        'Script' {
            return "docker compose exec -T ${EngineService} python -m scripts.$Script $pieceArg $ExtraArgs".Trim()
        }
        'Sqlite' {
            # Escape double quotes inside SQL
            $sqlEscaped = $Sqlite -replace '"', '\"'
            return "docker compose exec -T ${EngineService} sqlite3 /app/runtime/state.db `"$sqlEscaped`""
        }
        'Compose' {
            return "docker compose $Compose $ExtraArgs".Trim()
        }
        'Raw' {
            return $Raw
        }
    }
}

# ============================================================================
# Build full command (with cd prefix for non-Raw modes)
# ============================================================================
$inner = Build-InnerCommand -ParameterSet $PSCmdlet.ParameterSetName

if ($PSCmdlet.ParameterSetName -eq 'Raw') {
    $fullCmd = $inner
} else {
    if ($Local) {
        # Local mode: cd using PowerShell syntax
        $fullCmd = "cd `"$RemoteEngineDir`"; $inner"
    } else {
        # Remote mode: cmd.exe (Windows OpenSSH default shell)
        # Note: cmd "cd" without /d does NOT switch drive letter, only remembers
        # the directory on that drive. Use "cd /d" to switch both drive + path.
        $dirForCmd = $RemoteEngineDir.Replace('/', '\')
        $fullCmd = "cd /d $dirForCmd && $inner"
    }
}

# ============================================================================
# Execute
# ============================================================================
$banner = "-" * 70
Write-Host "`n$banner" -ForegroundColor Cyan
Write-Host " Mode: $(if ($Local) { 'LOCAL' } else { "REMOTE -> $RemoteUser@$RemoteHost" })" -ForegroundColor Cyan
Write-Host " Action: $($PSCmdlet.ParameterSetName)" -ForegroundColor Cyan
Write-Host "$banner" -ForegroundColor Cyan

if ($Local) {
    Write-Host "[cmd] $fullCmd" -ForegroundColor DarkGray
    if ($DryRun) {
        Write-Host "[dry-run] Not executing, printed command only" -ForegroundColor Yellow
        exit 0
    }
    Push-Location
    try {
        Invoke-Expression $fullCmd
    } finally {
        Pop-Location
    }
} else {
    # Remote mode: Windows OpenSSH (sshd on Windows + Tailscale network)
    # Wrap: ssh user@host "<cmd>"
    $cmdEscaped = $fullCmd -replace '"', '\"'

    Write-Host "[ssh] ssh $RemoteUser@$RemoteHost" -ForegroundColor DarkGray
    Write-Host "[cmd] $fullCmd" -ForegroundColor DarkGray

    if ($DryRun) {
        Write-Host "[dry-run] Not executing, printed command only" -ForegroundColor Yellow
        Write-Host "`nEquivalent manual command:" -ForegroundColor DarkGray
        Write-Host "  ssh ${RemoteUser}@${RemoteHost} `"$fullCmd`"" -ForegroundColor White
        exit 0
    }

    & ssh "${RemoteUser}@${RemoteHost}" $cmdEscaped
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Host "`n[X] Remote command failed with exit code $exitCode" -ForegroundColor Red
        exit $exitCode
    }
}

Write-Host "`n$banner" -ForegroundColor Cyan
Write-Host " [OK] Done" -ForegroundColor Green
Write-Host "$banner`n" -ForegroundColor Cyan
