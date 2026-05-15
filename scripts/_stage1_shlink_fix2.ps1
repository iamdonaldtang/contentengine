Set-Location 'D:\Taskon\marketing\engine'

Write-Host '=== STAGE 1.2 (retry): direct docker restart taskon-shlink ==='
docker restart taskon-shlink

Write-Host ''
Write-Host '=== STAGE 1.3: wait + poll /rest/v2/health and /health (correct paths for shlink 5.x) ==='
Start-Sleep 15
$pass = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
  $v2  = curl.exe -s -m 5 http://localhost:8085/rest/v2/health 2>$null
  $v3  = curl.exe -s -m 5 http://localhost:8085/rest/v3/health 2>$null
  $alt = curl.exe -s -m 5 http://localhost:8085/health 2>$null
  Write-Host ('  v2: {0,-50}  v3: {1,-20}  alt: {2}' -f $v2.Substring(0,[Math]::Min(50,$v2.Length)), $v3.Substring(0,[Math]::Min(20,$v3.Length)), $alt.Substring(0,[Math]::Min(20,$alt.Length)))
  if ($v2 -match '"status"\s*:\s*"pass"' -or $v3 -match '"status"\s*:\s*"pass"' -or $alt -match '"status"\s*:\s*"pass"') { $pass = $true; break }
  Start-Sleep 5
}

Write-Host ''
Write-Host '=== container state after restart ==='
docker ps --filter name=taskon-shlink --format 'table {{.Names}}`t{{.Status}}`t{{.Ports}}'

Write-Host ''
if ($pass) {
  Write-Host '=== STAGE 1: PASS — shlink returns status=pass on one of the health endpoints ==='
  exit 0
} else {
  Write-Host '=== STAGE 1: STILL FAILING ==='
  docker inspect taskon-shlink --format '{{json .State.Health}}' 2>&1 | Out-String
  exit 1
}
