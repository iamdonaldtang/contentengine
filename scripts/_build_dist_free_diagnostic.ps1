$ErrorActionPreference = 'Stop'
Set-Location 'D:\Taskon\marketing\engine'

# 1. clean old dist (had encoding garble + wrong outer wrapper)
Remove-Item -Recurse -Force landing_pages\dist -ErrorAction SilentlyContinue
Remove-Item -Force landing_pages\dist.zip -ErrorAction SilentlyContinue

# 2. build new dist · FLAT inside (no free-diagnostic outer wrapper)
$Dist = "landing_pages\dist"
New-Item -ItemType Directory -Force -Path "$Dist\css" | Out-Null
New-Item -ItemType Directory -Force -Path "$Dist\js"  | Out-Null

# 3. binary copy · preserves UTF-8
Copy-Item landing_pages\free-diagnostic\index.html "$Dist\"
Copy-Item landing_pages\free-diagnostic\styles.css "$Dist\"
Copy-Item landing_pages\shared\taskon-base.css "$Dist\css\"
Copy-Item frontend_snippets\taskon_uid.js          "$Dist\js\"
Copy-Item frontend_snippets\landing_impression.js  "$Dist\js\"
Copy-Item frontend_snippets\landing_form_submit.js "$Dist\js\"

# 4. .NET safe UTF-8 read/write · rewrite paths with /free-diagnostic/ prefix
$f = (Resolve-Path "$Dist\index.html").Path
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$html = [System.IO.File]::ReadAllText($f, [System.Text.Encoding]::UTF8)

$html = $html.Replace('href="/css/taskon-base.css"',      'href="/free-diagnostic/css/taskon-base.css"')
$html = $html.Replace('src="/js/taskon_uid.js"',          'src="/free-diagnostic/js/taskon_uid.js"')
$html = $html.Replace('src="/js/landing_impression.js"',  'src="/free-diagnostic/js/landing_impression.js"')
$html = $html.Replace('src="/js/landing_form_submit.js"', 'src="/free-diagnostic/js/landing_form_submit.js"')

[System.IO.File]::WriteAllText($f, $html, $utf8NoBom)

# 5. encoding sanity check (expect: middle-dot · normal · not garbled CJK)
Write-Host '--- step 5: TaskOn lines (expect clean UTF-8) ---'
Select-String -Path $f -Pattern 'TaskOn' | Select-Object -First 3

# 6. path rewrite check (expect: no output)
Write-Host '--- step 6: leftover /css|/js root paths (expect none) ---'
Select-String -Path $f -Pattern 'href="/css/|src="/js/'

# 7. dist structure (expect: index.html directly under dist root)
Write-Host '--- step 7: dist tree (relative) ---'
Get-ChildItem -Recurse $Dist | Select-Object @{N='Path';E={$_.FullName.Replace((Resolve-Path $Dist).Path + '\','')}}

# 8. zip · flat (★ $Dist\* puts files at zip root, no wrapper)
Compress-Archive -Path "$Dist\*" -DestinationPath "landing_pages\dist.zip" -Force

# 9. verify zip structure
Write-Host '--- step 9: zip contents ---'
Expand-Archive -Path "landing_pages\dist.zip" -DestinationPath "$env:TEMP\zip_verify" -Force
Get-ChildItem -Recurse "$env:TEMP\zip_verify" | Select-Object FullName
Remove-Item -Recurse "$env:TEMP\zip_verify"

# 10. zip size
Write-Host '--- step 10: zip size ---'
Get-Item landing_pages\dist.zip | Select-Object Name, Length
