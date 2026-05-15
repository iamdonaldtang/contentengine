Set-Location 'D:\Taskon\marketing\engine'

Write-Host '=== A. flask_cors still installed (sanity) ==='
docker compose exec -T ingestion python -c "import flask_cors; print('flask_cors', flask_cors.__version__)" 2>&1
Write-Host ''

Write-Host '=== B. /health from CF ==='
curl.exe -s -i https://ingest.taskon.xyz/health 2>&1 | Select-Object -First 8
Write-Host ''

Write-Host '=== C. CORS preflight (origin: contentengine-landing.pages.dev) ==='
$resp = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1
$resp | Select-Object -First 1
$resp | Select-String -Pattern 'access-control'
Write-Host ''

Write-Host '=== D. CORS preflight (origin: taskon.xyz) ==='
$r2 = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://taskon.xyz" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1
$r2 | Select-Object -First 1
$r2 | Select-String -Pattern 'access-control'
Write-Host ''

Write-Host '=== E. CORS negative (origin: evil.com — must NOT echo origin) ==='
$r3 = curl.exe -s -X OPTIONS https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://evil.com" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: content-type" `
  -i 2>&1
$r3 | Select-Object -First 1
$r3 | Select-String -Pattern 'access-control'
Write-Host ''

Write-Host '=== F. POST /api/landing-signup smoke test (real round-trip) ==='
$body = '{"email":"regression-b@taskon.xyz","landing_path":"/free-diagnostic/","utm_campaign":"regression_test_b"}'
$post = curl.exe -s -X POST https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Content-Type: application/json" `
  -d $body `
  -i 2>&1
$post | Select-Object -First 1
$post | Select-String -Pattern 'access-control'
Write-Host 'body:'
$post | Select-Object -Last 5
Write-Host ''

Write-Host '=== G. confirm lead landed in state.db ==='
docker compose exec -T engine sqlite3 -header -column /app/runtime/state.db "SELECT id, email, first_utm_campaign FROM leads WHERE email='regression-b@taskon.xyz';"
