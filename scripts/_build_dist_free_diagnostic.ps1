$ErrorActionPreference = 'Stop'
Set-Location 'D:\Taskon\marketing\engine'

# 1. build dist tree
$Dist = "landing_pages\dist\free-diagnostic"
New-Item -ItemType Directory -Force -Path "$Dist\css" | Out-Null
New-Item -ItemType Directory -Force -Path "$Dist\js"  | Out-Null

# 2. copy 6 assets
Copy-Item landing_pages\free-diagnostic\index.html "$Dist\"
Copy-Item landing_pages\free-diagnostic\styles.css "$Dist\"
Copy-Item landing_pages\shared\taskon-base.css "$Dist\css\"
Copy-Item frontend_snippets\taskon_uid.js          "$Dist\js\"
Copy-Item frontend_snippets\landing_impression.js  "$Dist\js\"
Copy-Item frontend_snippets\landing_form_submit.js "$Dist\js\"

# 3. rewrite paths in dist index.html
$f = "$Dist\index.html"
$html = Get-Content $f -Raw
$html = $html.Replace('href="/css/taskon-base.css"',       'href="/free-diagnostic/css/taskon-base.css"')
$html = $html.Replace('src="/js/taskon_uid.js"',           'src="/free-diagnostic/js/taskon_uid.js"')
$html = $html.Replace('src="/js/landing_impression.js"',   'src="/free-diagnostic/js/landing_impression.js"')
$html = $html.Replace('src="/js/landing_form_submit.js"',  'src="/free-diagnostic/js/landing_form_submit.js"')
Set-Content $f $html -Encoding UTF8

# 4. verify (expect: no output)
Write-Host '--- step 4: leftover root-absolute paths (expect none) ---'
Select-String -Path $f -Pattern 'href="/css/|src="/js/'

# 5. dist tree
Write-Host '--- step 5: dist tree ---'
Get-ChildItem -Recurse landing_pages\dist | Select-Object FullName
