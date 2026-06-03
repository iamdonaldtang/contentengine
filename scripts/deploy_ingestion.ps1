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

Write-Host "== 1/5 hard-sync to $Ref (engine host = origin mirror) =="
git fetch origin
git reset --hard $Ref

Write-Host "== 2/5 rebuild ingestion container =="
docker compose up -d --build $Service

Write-Host "== 3/5 peek code inside the container =="
Start-Sleep -Seconds 4
try {
  $probe = docker compose exec -T $Service python -c "import ingestion.admin_routes as a; print('NEW' if hasattr(a,'admin_upload_asset') else 'OLD')" 2>$null
  Write-Host "  container admin_routes: $probe (informational; smoke is the gate)"
} catch { Write-Host "  (in-container probe skipped)" }

Write-Host "== 4/5 end-to-end smoke (mandatory gate) =="
$tokenLine = Select-String -Path .env -Pattern '^ADMIN_API_TOKEN=' | Select-Object -First 1
if (-not $tokenLine) { Write-Error ".env missing ADMIN_API_TOKEN, cannot smoke"; exit 1 }
$token = ($tokenLine.Line -replace '^ADMIN_API_TOKEN=', '').Trim()

$bash = Join-Path $env:ProgramFiles "Git\bin\bash.exe"
if (-not (Test-Path $bash)) { $bash = "bash" }   # fall back to bash on PATH

$env:ADMIN_API_TOKEN = $token
$env:ENGINE_BASE = $EngineBase
& $bash "scripts/smoke_httpfirst.sh"
$code = $LASTEXITCODE
$env:ADMIN_API_TOKEN = $null   # clear after run

Write-Host "== 5/5 result =="
if ($code -ne 0) {
  Write-Host "SMOKE FAILED (exit=$code) - deploy NOT considered done." -ForegroundColor Red
  Write-Host "  Triage: see the failing lines above; likely stale image / bad token / a job failed." -ForegroundColor Red
  Write-Host "  Ref: docs/HTTP-first_FangAnA_*.md sec 5.5 + the checklist T5/W1/R1." -ForegroundColor Red
  exit $code
}
Write-Host "Deploy + smoke green. ingestion is live." -ForegroundColor Green
