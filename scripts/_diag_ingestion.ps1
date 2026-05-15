Set-Location 'D:\Taskon\marketing\engine'

Write-Host '--- docker compose ps ingestion ---'
docker compose ps ingestion

Write-Host ''
Write-Host '--- docker compose logs ingestion --tail=40 ---'
docker compose logs ingestion --tail=40

Write-Host ''
Write-Host '--- INGESTION_PORT from .env ---'
Select-String -Path .env -Pattern '^INGESTION_PORT' | Select-Object -First 1

Write-Host ''
Write-Host '--- local healthcheck (curl localhost) ---'
$port = (Select-String -Path .env -Pattern '^INGESTION_PORT=(\d+)' | Select-Object -First 1).Matches[0].Groups[1].Value
if (-not $port) { $port = '5051' }
Write-Host "trying http://localhost:$port/healthz ..."
curl.exe -s -m 5 -i "http://localhost:$port/healthz" 2>&1 | Select-Object -First 15

Write-Host ''
Write-Host '--- local CORS preflight (bypass CF, hit container directly) ---'
curl.exe -s -m 5 -X OPTIONS "http://localhost:$port/api/landing-signup" `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1 | Select-Object -First 20
