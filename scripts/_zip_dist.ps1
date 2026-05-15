$ErrorActionPreference = 'Stop'
Set-Location 'D:\Taskon\marketing\engine'

# 1. zip dist contents (note trailing \* — zip root = free-diagnostic/, not dist/free-diagnostic/)
Compress-Archive -Path "landing_pages\dist\*" -DestinationPath "landing_pages\dist.zip" -Force

# 2. verify zip structure
Expand-Archive -Path "landing_pages\dist.zip" -DestinationPath "$env:TEMP\dist_check" -Force
Write-Host '--- zip contents ---'
Get-ChildItem -Recurse "$env:TEMP\dist_check" | Select-Object FullName | Format-Table -AutoSize
Remove-Item -Recurse "$env:TEMP\dist_check"

# 3. absolute path of the zip
Write-Host '--- zip path ---'
(Resolve-Path landing_pages\dist.zip).Path
