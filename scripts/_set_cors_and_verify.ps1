$ErrorActionPreference = 'Stop'
Set-Location 'D:\Taskon\marketing\engine'

# 2. add CORS_ALLOWED_ORIGINS (default taskon.xyz + .pages.dev preview)
Write-Host '--- step 2: appending CORS_ALLOWED_ORIGINS to .env ---'
Add-Content -Path .env -Value "`nCORS_ALLOWED_ORIGINS=https://taskon.xyz,https://contentengine-landing.pages.dev"
Write-Host 'appended. tail of .env:'
Get-Content .env -Tail 3

# 3. restart ingestion
Write-Host ''
Write-Host '--- step 3: docker compose restart ingestion ---'
docker compose restart ingestion

# 4. wait 5s, send CORS preflight
Write-Host ''
Write-Host '--- step 4: CORS preflight ---'
Start-Sleep 5
curl.exe -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1 | Select-Object -First 20
