Set-Location 'D:\Taskon\marketing\engine'

Write-Host '--- CORS preflight (origin: contentengine-landing.pages.dev) ---'
$resp = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i
$resp | Select-Object -First 20

Write-Host ''
Write-Host '--- CORS preflight (origin: taskon.xyz, sanity check) ---'
$resp2 = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://taskon.xyz" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i
$resp2 | Select-Object -First 20

Write-Host ''
Write-Host '--- CORS preflight (origin: evil.com, negative test) ---'
$resp3 = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://evil.com" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i
$resp3 | Select-Object -First 20
