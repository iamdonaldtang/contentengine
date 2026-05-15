Set-Location 'D:\Taskon\marketing\engine'

Write-Host '=== F2. POST with JSON-from-file (proper escaping) ==='
$post = curl.exe -s -X POST https://ingest.taskon.xyz/api/landing-signup `
  -H "Origin: https://contentengine-landing.pages.dev" `
  -H "Content-Type: application/json" `
  --data-binary "@scripts/_regression_post.json" `
  -i 2>&1
$post | Select-Object -First 1
$post | Select-String -Pattern 'access-control'
Write-Host 'body:'
$post | Select-Object -Last 3

Write-Host ''
Write-Host '=== G2. confirm new lead in state.db ==='
docker compose exec -T engine sqlite3 -header -column /app/runtime/state.db "SELECT id, email, datetime(first_seen_at,'localtime') AS seen, first_utm_campaign FROM leads WHERE email='regression-b@taskon.xyz';"
