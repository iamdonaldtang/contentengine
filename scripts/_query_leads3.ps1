Set-Location 'D:\Taskon\marketing\engine'
docker compose exec -T engine sqlite3 -header -column /app/runtime/state.db "SELECT id, email, datetime(first_seen_at, 'localtime') AS seen, first_utm_campaign FROM leads ORDER BY id DESC LIMIT 5;"
