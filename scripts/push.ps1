# push.ps1 - LAPTOP: one-shot commit + push of the engine repo to origin.
# ===========================================================================
# Runs on: LAPTOP (not the engine host).
# What: stage + commit + push everything Cowork edited in the laptop repo to
#       origin/main. After pushing, run `tk_deploy` in Cowork to trigger the
#       engine host self-deploy (see watch_deploy.ps1).
# Discipline: origin is the single source of truth. The engine host is never
#       edited locally; it deploys via `git reset --hard origin/main`.
#       .env / runtime/ / scripts/.tk_token are gitignored and never committed.
#
# Usage (LAPTOP PowerShell):
#   .\scripts\push.ps1                       # auto commit message
#   .\scripts\push.ps1 -Message "fix: xxx"   # custom message
#   .\scripts\push.ps1 -DryRun               # preview only, no commit
# ===========================================================================
param(
  [string]$Message,
  [switch]$DryRun
)
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."   # engine repo root

# 1) Any changes?
$changed = @(git status --porcelain | Where-Object { $_ -ne "" })
if ($changed.Count -eq 0) {
  Write-Host "Clean working tree, nothing to push." -ForegroundColor Green
  exit 0
}

Write-Host "== Pending changes ($($changed.Count)) ==" -ForegroundColor Cyan
git status --short

# 2) Behind-origin guard (origin should only ever advance from this laptop)
git fetch origin --quiet
$behind = (git rev-list --count HEAD..origin/main 2>$null)
if ($behind -and ([int]$behind) -gt 0) {
  Write-Warning "Local is behind origin/main by $behind commit(s)."
  Write-Warning "Per the discipline, origin should only advance from this laptop."
  Write-Warning "Someone may have committed on the engine host by mistake. Reconcile before pushing."
  exit 1
}

# 3) Commit message
if (-not $Message) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
  $count = $changed.Count
  $Message = "chore(cowork): update $stamp [$count files]"
}

if ($DryRun) {
  Write-Host ""
  Write-Host "[DryRun] would run:" -ForegroundColor Yellow
  Write-Host "  git add -A"
  Write-Host "  git commit -m `"$Message`""
  Write-Host "  git push origin main"
  Write-Host "[DryRun] nothing committed."
  exit 0
}

# 4) add + commit + push
git add -A
git commit -m $Message
git push origin main

Write-Host ""
Write-Host "Pushed to origin/main: $Message" -ForegroundColor Green
Write-Host "Next: in Cowork run  tk_deploy  to trigger the engine self-deploy, then  tk_deploy_wait." -ForegroundColor Green
