Set-Location 'D:\Taskon\marketing\engine'

Write-Host '--- schema of leads table ---'
docker compose exec -T engine sqlite3 /app/runtime/state.db ".schema leads"

Write-Host ''
Write-Host '--- row count ---'
docker compose exec -T engine sqlite3 /app/runtime/state.db "SELECT COUNT(*) FROM leads;"

Write-Host ''
Write-Host '--- last 3 leads (headers + columns + box mode) ---'
docker compose exec -T engine sqlite3 -header -column /app/runtime/state.db "SELECT id, email, first_utm_campaign FROM leads ORDER BY id DESC LIMIT 3;"
