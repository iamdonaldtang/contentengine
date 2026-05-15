Set-Location 'D:\Taskon\marketing\engine'

Write-Host '=== 1. shlink container · inspect labels (which compose project?) ==='
docker inspect taskon-shlink --format '{{json .Config.Labels}}' 2>&1

Write-Host ''
Write-Host '=== 2. shlink port bindings on host ==='
docker inspect taskon-shlink --format '{{json .NetworkSettings.Ports}}' 2>&1

Write-Host ''
Write-Host '=== 3. shlink configured healthcheck ==='
docker inspect taskon-shlink --format '{{json .Config.Healthcheck}}' 2>&1

Write-Host ''
Write-Host '=== 4. recent healthcheck results from docker ==='
docker inspect taskon-shlink --format '{{json .State.Health}}' 2>&1

Write-Host ''
Write-Host '=== 5. probe candidate health endpoints from host ==='
$ports = @(8085, 8080, 8000)
$paths = @('/rest/v3/health', '/rest/v2/health', '/health')
foreach ($p in $ports) {
  foreach ($path in $paths) {
    $url = "http://localhost:$p$path"
    $out = curl.exe -s -m 3 -o NUL -w 'HTTP %{http_code}  bytes=%{size_download}' $url 2>&1
    Write-Host ('  {0,-40} -> {1}' -f $url, $out)
  }
}

Write-Host ''
Write-Host '=== 6. probe from inside the container itself ==='
docker exec taskon-shlink wget -q -O - http://localhost:8080/rest/v3/health 2>&1
Write-Host ''
docker exec taskon-shlink wget -q -O - http://localhost/rest/v3/health 2>&1

Write-Host ''
Write-Host '=== 7. find the compose file that owns shlink ==='
$proj = (docker inspect taskon-shlink --format '{{ index .Config.Labels "com.docker.compose.project.config_files"}}' 2>&1)
Write-Host "compose project_config_files label: $proj"
$wd = (docker inspect taskon-shlink --format '{{ index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>&1)
Write-Host "compose project working_dir: $wd"
