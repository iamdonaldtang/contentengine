Set-Location 'D:\Taskon\marketing\engine'

Write-Host '=== STAGE 0: docker ps filter ==='
docker ps --format 'table {{.Names}}`t{{.Status}}' | Select-String 'engine|postiz|shlink'

Write-Host ''
Write-Host '=== sanity: required scripts exist? ==='
@('scripts\run_publish.ps1','scripts\run_metrics.ps1','scripts\run_health.ps1') |
  ForEach-Object { '{0}: {1}' -f $_, (Test-Path $_) }
