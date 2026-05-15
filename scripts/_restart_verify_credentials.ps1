Set-Location 'D:\Taskon\marketing\engine'

Write-Host '--- 1. restart ingestion ---'
docker compose restart ingestion

Write-Host ''
Write-Host '--- 2. wait 5s for boot ---'
Start-Sleep 5

Write-Host ''
Write-Host '--- 3. CORS preflight · grep Access-Control headers ---'
$resp = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1
$resp | Select-String -Pattern "Access-Control"

Write-Host ''
Write-Host '--- 4. full status line + container health ---'
($resp | Select-Object -First 1)
docker compose ps ingestion
