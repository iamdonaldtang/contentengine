$b = [System.IO.File]::ReadAllBytes('D:\Taskon\marketing\engine\landing_pages\dist\index.html')
$ascii = [System.Text.Encoding]::ASCII.GetString($b)
$idx = $ascii.IndexOf('Projects')
Write-Host "'Projects' found at byte offset: $idx"
$slice = $b[($idx+9)..($idx+10)]
Write-Host "--- 2 bytes after 'Projects ' (expect C2 B7 = UTF-8 middle dot) ---"
($slice | ForEach-Object { '{0:X2}' -f $_ }) -join ' '

# also check BOM
Write-Host ""
Write-Host "--- first 3 bytes (expect NO BOM = not EF BB BF) ---"
($b[0..2] | ForEach-Object { '{0:X2}' -f $_ }) -join ' '

# file size
Write-Host ""
Write-Host "--- dist/index.html size ---"
Write-Host "$($b.Length) bytes"
