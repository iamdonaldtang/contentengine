Set-Location 'D:\Taskon\marketing\engine'

Write-Host '--- CORS preflight (origin: contentengine-landing.pages.dev) ---'
$resp = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1

Write-Host 'status line:'
$resp | Select-Object -First 1
Write-Host ''
Write-Host 'Access-Control-* headers:'
$resp | Select-String -Pattern 'Access-Control'

Write-Host ''
Write-Host '--- direct localhost check (bypass CF) ---'
$local = curl.exe -s -X OPTIONS http://localhost:5051/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1

Write-Host 'status line:'
$local | Select-Object -First 1
Write-Host ''
Write-Host 'Access-Control-* headers:'
$local | Select-String -Pattern 'Access-Control'
