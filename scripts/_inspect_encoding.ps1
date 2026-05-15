$ErrorActionPreference = 'Stop'
$f = 'D:\Taskon\marketing\engine\landing_pages\free-diagnostic\index.html'
$bytes = [System.IO.File]::ReadAllBytes($f)

Write-Host "--- file size: $($bytes.Length) bytes ---"

# Find the title line bytes
$ascii = [System.Text.Encoding]::ASCII.GetString($bytes)
$titleIdx = $ascii.IndexOf('<title>')
$titleEnd = $ascii.IndexOf('</title>', $titleIdx)
Write-Host "--- title byte range: $titleIdx .. $titleEnd ---"

Write-Host "--- title raw bytes (hex) ---"
$titleBytes = $bytes[$titleIdx..$titleEnd]
($titleBytes | ForEach-Object { '{0:X2}' -f $_ }) -join ' '

Write-Host ""
Write-Host "--- title decoded as UTF-8 ---"
[System.Text.Encoding]::UTF8.GetString($titleBytes)

Write-Host ""
Write-Host "--- title decoded as GB18030 (Simplified Chinese) ---"
try {
  [System.Text.Encoding]::GetEncoding(54936).GetString($titleBytes)
} catch { "n/a: $_" }

Write-Host ""
Write-Host "--- title decoded as Windows-1252 (cp1252) ---"
[System.Text.Encoding]::GetEncoding(1252).GetString($titleBytes)

Write-Host ""
Write-Host "--- non-ASCII byte offsets in first 600 bytes ---"
for ($i=0; $i -lt [Math]::Min(600, $bytes.Length); $i++) {
  if ($bytes[$i] -gt 127) {
    $hex = '{0:X2}' -f $bytes[$i]
    $prev = if ($i -gt 0) { [char]$bytes[$i-1] } else { '?' }
    $next = if ($i+1 -lt $bytes.Length) { [char]$bytes[$i+1] } else { '?' }
    Write-Host "  offset $i : 0x$hex  (prev=$prev next=$next)"
  }
}
