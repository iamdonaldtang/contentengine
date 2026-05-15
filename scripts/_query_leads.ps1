Set-Location 'D:\Taskon\marketing\engine'

Write-Host '--- 1. engine container status ---'
docker compose ps engine

Write-Host ''
Write-Host '--- 2. check sqlite3 availability in container ---'
docker compose exec -T engine which sqlite3 2>&1
docker compose exec -T engine sqlite3 --version 2>&1

Write-Host ''
Write-Host '--- 3. check state.db path ---'
docker compose exec -T engine ls -la /app/runtime/state.db 2>&1

Write-Host ''
Write-Host '--- 4. try python sqlite3 module instead (always available in py3 image) ---'
$pythonQuery = 'import sqlite3; c=sqlite3.connect("/app/runtime/state.db"); print(*c.execute("SELECT id, email, first_utm_campaign FROM leads ORDER BY id DESC LIMIT 3"), sep="\n")'
docker compose exec -T engine python -c $pythonQuery 2>&1
