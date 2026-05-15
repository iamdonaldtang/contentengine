Set-Location 'D:\Taskon\marketing\engine'

Write-Host '=== STAGE 1.1: shlink logs (last 50) ==='
docker logs taskon-shlink --tail 50 2>&1

Write-Host ''
Write-Host '=== STAGE 1.2: docker compose restart shlink ==='
docker compose -f D:\TaskOn\marketing\engine\docker-compose.yml restart shlink

Write-Host ''
Write-Host '=== STAGE 1.3: wait 15s, then poll until status=pass (up to 60s total) ==='
$deadline = (Get-Date).AddSeconds(60)
Start-Sleep 15
$pass = $false
while ((Get-Date) -lt $deadline) {
  try {
    $resp = curl.exe -s -m 5 http://localhost:8085/rest/v3/health 2>$null
    Write-Host "  ping: $resp"
    if ($resp -match '"status"\s*:\s*"pass"') { $pass = $true; break }
  } catch { Write-Host "  (curl error: $_)" }
  Start-Sleep 5
}

Write-Host ''
if ($pass) {
  Write-Host '=== STAGE 1: PASS — shlink healthy, ok to enter STAGE 2 ==='
  exit 0
} else {
  Write-Host '=== STAGE 1: FAIL — shlink did not return status=pass within 60s ==='
  Write-Host '=== container state right now: ==='
  docker compose ps shlink
  Write-Host '=== latest logs: ==='
  docker logs taskon-shlink --tail 30 2>&1
  exit 1
}
