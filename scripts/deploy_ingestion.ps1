# deploy_ingestion.ps1 - ENGINE HOST: the single deploy entry for ingestion.
# ===========================================================================
# Runs on: ENGINE HOST (D:\engine-host\taskon\engine).
# What: hard-sync to origin, rebuild, then a MANDATORY end-to-end smoke gate.
#       Always deploy with this; do NOT run a bare `docker compose up --build`
#       (that skips the smoke and was the source of silent "stale image" bugs).
#
# Discipline: the engine host is a pure mirror of origin. Never edit locally,
#       never commit locally. Deploy = `git reset --hard origin/main`.
#       .env / runtime/ / scripts/.tk_token are gitignored, untouched by reset.
#
# Usage (ENGINE HOST):
#   .\scripts\deploy_ingestion.ps1
#   .\scripts\deploy_ingestion.ps1 -EngineBase http://engine:5051
#
# Exit code: 0 = deployed + smoke green; non-zero = a gate failed (treat as
#       deploy NOT done).
# ===========================================================================
[CmdletBinding()]
param(
  [string]$EngineBase = "https://ingest.taskon.xyz",
  [string]$Ref = "origin/main",
  # docker compose service name (NOT the container_name, NOT the tailscale host).
  # Source of truth = origin docker-compose.yml: services are engine/ingestion;
  # container_name is taskon-*. Pliven copies pass their own -Service.
  [string]$Service = "ingestion"
)
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."   # engine repo root

# Native CLIs (git, docker, bash) print normal progress to stderr. Under
# $ErrorActionPreference='Stop', PowerShell 5.1 raises a terminating
# NativeCommandError on that stderr even when the command SUCCEEDS, and a bare
# 2>&1 redirect does NOT suppress it. Run native commands through this helper:
# it relaxes the preference for just that one call, echoes all output, and
# returns the real process exit code so callers gate on the code, not stderr.
function Invoke-Native {
  param([Parameter(Mandatory=$true)][scriptblock]$Cmd)
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Cmd 2>&1 | ForEach-Object { Write-Host $_ } }
  finally { $ErrorActionPreference = $old }
  return $LASTEXITCODE
}

Write-Host "== 1/5 hard-sync to $Ref (engine host = origin mirror) =="
$code = Invoke-Native { git fetch origin }
if ($code -ne 0) { Write-Error "git fetch failed (exit=$code)"; exit 1 }
$code = Invoke-Native { git reset --hard $Ref }
if ($code -ne 0) { Write-Error "git reset failed (exit=$code)"; exit 1 }

Write-Host "== 2/5 rebuild ingestion container =="
$code = Invoke-Native { docker compose up -d --build $Service }
if ($code -ne 0) { Write-Error "docker build/up failed (exit=$code)"; exit 1 }

Write-Host "== 3/5 peek code inside the container =="
Start-Sleep -Seconds 4
try {
  $probe = docker compose exec -T $Service python -c "import ingestion.admin_routes as a; print('NEW' if hasattr(a,'admin_upload_asset') else 'OLD')" 2>$null
  Write-Host "  container admin_routes: $probe (informational; smoke is the gate)"
} catch { Write-Host "  (in-container probe skipped)" }

Write-Host "== 4/5 end-to-end smoke (mandatory gate, runs INSIDE the container) =="
$tokenLine = Select-String -Path .env -Pattern '^ADMIN_API_TOKEN=' | Select-Object -First 1
if (-not $tokenLine) { Write-Error ".env missing ADMIN_API_TOKEN, cannot smoke"; exit 1 }
$token = ($tokenLine.Line -replace '^ADMIN_API_TOKEN=', '').Trim()

# Run the smoke INSIDE the ingestion container, not host Git-bash. The engine
# host's MINGW bash has no python3, so every python3-based assertion (postiz
# health, state read, job task_id, the PNG fixture) false-failed 5/14 even
# though the engine was green (verified 14/14 from a python3 box). The image
# ships python3.12 + curl + bash, so this gates on the real service rather than
# host tooling. ENGINE_BASE stays the public URL to also prove public path+auth.
# Strip any CRLF from the shell scripts INSIDE the container right before
# running. The engine host's git (autocrlf) checks *.sh out as CRLF, and a
# `git reset --hard` will NOT re-normalize a working file whose blob is
# unchanged, so .gitattributes alone can't fix an already-CRLF tree. The Linux
# bash in the image then dies on the trailing \r ("set: pipefail: invalid
# option", "case ... in\r"). sed makes the gate immune regardless of checkout
# EOL. Container is ephemeral (rebuilt every deploy), so editing in place is safe.
$code = Invoke-Native {
  docker compose exec -T -e ADMIN_API_TOKEN=$token -e ENGINE_BASE=$EngineBase $Service bash -c 'sed -i "s/\r$//" scripts/smoke_httpfirst.sh scripts/tk.sh && bash scripts/smoke_httpfirst.sh'
}

Write-Host "== 5/5 result =="
if ($code -ne 0) {
  Write-Host "SMOKE FAILED (exit=$code) - deploy NOT considered done." -ForegroundColor Red
  Write-Host "  Triage: see the failing lines above; likely stale image / bad token / a job failed." -ForegroundColor Red
  Write-Host "  Ref: docs/HTTP-first_FangAnA_*.md sec 5.5 + the checklist T5/W1/R1." -ForegroundColor Red
  exit $code
}
Write-Host "Deploy + smoke green. ingestion is live." -ForegroundColor Green
