<#
.SYNOPSIS
TaskOn Landing Pages · 部署助手（打包 + 校验 + 可选自动上传）

.DESCRIPTION
**设计**: 路径自适应，**在主笔记本 / 引擎机 / 任何同步了源码的机器上都能跑**。
默认是服务器端部署 —— 推荐放引擎机上跑（引擎机 24/7 在线，定时验证可以挂上 Scheduled Task）。

3 个模式:

  -Mode Package  (默认)  打 zip 包到 D:\TaskOn\infra\landing_<date>.zip，给运维上传
  -Mode Verify           部署完跑 MIME 校验（curl JS/CSS 看 Content-Type 是否正确）
  -Mode Deploy           wrangler 一键上传 + 自动验证（需要 wrangler login + CF API token）

.EXAMPLE
.\deploy_landing.ps1
    打 zip 给运维 · 默认输出 D:\TaskOn\infra\landing_free-diagnostic_<date>.zip

.EXAMPLE
.\deploy_landing.ps1 -Mode Verify
    部署完手动跑校验 · 看 MIME 是否对

.EXAMPLE
.\deploy_landing.ps1 -Mode Deploy
    wrangler 自动上传 + 自动校验

.EXAMPLE
.\deploy_landing.ps1 -DistDir D:\engine-host\taskon\marketing\engine\landing_pages\dist
    显式指定 dist 目录（如果脚本自动找不到）

.NOTES
为什么这个脚本必要:
  18 天前那次手工打 zip 漏了 dist/js/ 和 dist/css/ 子目录,
  导致 production 落地页 form submit 静默挂掉 18 天没人发现.
  这个脚本的核心价值是 Test-DistStructure 校验 + Test-MimeTypes 校验,
  让漏文件 / MIME 错的 zip 永远不会传上去 / 不会被忽略.
#>

[CmdletBinding()]
param(
    [ValidateSet('Package', 'Verify', 'Deploy')]
    [string]$Mode = 'Package',

    [string]$DistDir = '',
    [string]$OutZip = '',
    [string]$ProjectName = 'contentengine-landing',
    [string]$VerifyBase = 'https://taskon.xyz/free-diagnostic'
)

$ErrorActionPreference = 'Stop'
$VerbosePreference = 'SilentlyContinue'

# ============================================================================
# 路径自适应 · 在 主笔记本 / 引擎机 / 任意拷贝过 engine 仓的机器 都能找到 dist
# ============================================================================
function Find-DistDir {
    if ($DistDir -and (Test-Path $DistDir)) {
        return (Resolve-Path $DistDir).Path
    }
    if ($env:LANDING_DIST_DIR -and (Test-Path $env:LANDING_DIST_DIR)) {
        return (Resolve-Path $env:LANDING_DIST_DIR).Path
    }

    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    }

    # 候选路径 · 按优先级排：相对 → 主笔记本 → 引擎机
    $candidates = @(
        (Join-Path $scriptDir '..\landing_pages\dist'),
        (Join-Path $scriptDir '..\..\landing_pages\dist'),
        'D:\TaskOn\marketing\engine\landing_pages\dist',
        'D:\engine-host\taskon\marketing\engine\landing_pages\dist',
        'D:\engine-host\taskon\landing_pages\dist'
    )

    foreach ($c in $candidates) {
        if (Test-Path $c) {
            return (Resolve-Path $c).Path
        }
    }

    throw "Cannot locate dist directory. Use -DistDir <path> or set `$env:LANDING_DIST_DIR. Tried:`n$($candidates -join "`n")"
}

# ============================================================================
# 校验 · 本地 dist 结构必须完整（防 18-day-MIME-bug 再发生）
# ============================================================================
function Test-DistStructure {
    param([string]$Dist)

    $required = @(
        'index.html',
        'styles.css',
        'css\taskon-base.css',
        'js\taskon_uid.js',
        'js\landing_impression.js',
        'js\landing_form_submit.js'
    )

    $missing = @()
    foreach ($rel in $required) {
        $full = Join-Path $Dist $rel
        if (-not (Test-Path $full)) {
            $missing += $rel
        }
    }

    if ($missing.Count -gt 0) {
        Write-Host "`n[X] dist 缺以下文件:" -ForegroundColor Red
        $missing | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
        Write-Host ""
        throw "dist structure INCOMPLETE. Abort to avoid uploading a broken bundle (would cause 18-day-MIME-bug again)."
    }

    Write-Host "[OK] dist 结构完整 · 6 个核心文件齐" -ForegroundColor Green
}

# ============================================================================
# 打 zip · 构造正确目录结构: zip 根/free-diagnostic/{index.html, css/, js/, styles.css}
# ============================================================================
function Invoke-Package {
    param([string]$Dist, [string]$OutPath)

    $stage   = Join-Path $env:TEMP "landing_pkg_$(Get-Random)"
    $stageFD = Join-Path $stage 'free-diagnostic'

    try {
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $stageFD -Force | Out-Null

        # 拷 dist/* → stage/free-diagnostic/
        Copy-Item -Path "$Dist\*" -Destination $stageFD -Recurse -Force

        Write-Host "`n=== Package contents ===" -ForegroundColor Cyan
        Get-ChildItem $stage -Recurse -File | Sort-Object FullName | ForEach-Object {
            $rel = $_.FullName.Substring($stage.Length + 1)
            Write-Host ("  {0,-50} {1,8} bytes" -f $rel, $_.Length) -ForegroundColor Gray
        }

        # 打 zip
        $outDir = Split-Path -Parent $OutPath
        if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
        if (Test-Path $OutPath) { Remove-Item $OutPath -Force }
        Compress-Archive -Path "$stage\*" -DestinationPath $OutPath -Force

    } finally {
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    }

    $info = Get-Item $OutPath
    Write-Host "`n[OK] Zip ready:" -ForegroundColor Green
    Write-Host "     Path: $($info.FullName)" -ForegroundColor White
    Write-Host "     Size: $([math]::Round($info.Length/1KB, 1)) KB" -ForegroundColor Gray
}

# ============================================================================
# MIME 校验 · curl 5 个关键 endpoint 看 Content-Type
# ============================================================================
function Test-MimeTypes {
    param([string]$BaseUrl)

    $cases = @(
        @{ Path = '/js/landing_form_submit.js';  Expect = 'javascript' }
        @{ Path = '/js/landing_impression.js';   Expect = 'javascript' }
        @{ Path = '/js/taskon_uid.js';            Expect = 'javascript' }
        @{ Path = '/css/taskon-base.css';         Expect = 'css' }
        @{ Path = '/styles.css';                  Expect = 'css' }
    )

    Write-Host "`n=== MIME Verification on $BaseUrl ===" -ForegroundColor Cyan
    $failed = @()

    foreach ($c in $cases) {
        $url = "$BaseUrl$($c.Path)"
        try {
            $r  = Invoke-WebRequest -Uri $url -Method Head -TimeoutSec 10 -UseBasicParsing
            $ct = $r.Headers['Content-Type']
            if ($ct -match $c.Expect) {
                Write-Host ("  [OK] {0,-45} {1}" -f $c.Path, $ct) -ForegroundColor Green
            } else {
                Write-Host ("  [X]  {0,-45} {1} (expected: {2})" -f $c.Path, $ct, $c.Expect) -ForegroundColor Red
                $failed += $c.Path
            }
        } catch {
            $msg = $_.Exception.Message
            Write-Host ("  [X]  {0,-45} {1}" -f $c.Path, $msg) -ForegroundColor Red
            $failed += $c.Path
        }
    }

    if ($failed.Count -gt 0) {
        throw "MIME verification FAILED · these files served as wrong MIME type:`n  $($failed -join "`n  ")`nLikely cause: CF Pages upload missed js/ or css/ subdir. Re-package and re-upload."
    }
    Write-Host "`n[OK] All MIME types correct · upload was successful." -ForegroundColor Green
}

# ============================================================================
# wrangler 自动部署
# ============================================================================
function Invoke-WranglerDeploy {
    param([string]$Dist, [string]$Project)

    $cmd = Get-Command wrangler -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "wrangler not installed. Run: npm install -g wrangler"
    }

    $stage   = Join-Path $env:TEMP "landing_deploy_$(Get-Random)"
    $stageFD = Join-Path $stage 'free-diagnostic'

    try {
        New-Item -ItemType Directory -Path $stageFD -Force | Out-Null
        Copy-Item -Path "$Dist\*" -Destination $stageFD -Recurse -Force

        Push-Location $stage
        $msg = "auto-deploy $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        wrangler pages deploy . --project-name=$Project --commit-message=$msg
        if ($LASTEXITCODE -ne 0) {
            throw "wrangler exited with code $LASTEXITCODE"
        }
    } finally {
        Pop-Location -ErrorAction SilentlyContinue
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ============================================================================
# 部署日志 · 写到 D:\TaskOn\logs\YYYY-MM-DD.md（Donald CLAUDE.md 约定的日志位置）
# ============================================================================
function Write-DeployLog {
    param([string]$Action, [string]$Detail)

    $logDate = Get-Date -Format 'yyyy-MM-dd'
    $logTime = Get-Date -Format 'HH:mm:ss'

    $logCandidates = @(
        "D:\TaskOn\logs\$logDate.md",
        "D:\engine-host\infra\logs\$logDate.md"
    )

    foreach ($p in $logCandidates) {
        $dir = Split-Path -Parent $p
        if (Test-Path $dir) {
            $line = "`n- [$logTime] **deploy_landing.ps1** · $Action · $Detail"
            Add-Content -Path $p -Value $line -Encoding UTF8
            Write-Host "[log] Appended to $p" -ForegroundColor DarkGray
            return
        }
    }
}

# ============================================================================
# 主入口
# ============================================================================
$banner = "=" * 60
Write-Host "`n$banner" -ForegroundColor Cyan
Write-Host " TaskOn Landing Pages Deploy · Mode: $Mode" -ForegroundColor Cyan
Write-Host " Host: $env:COMPUTERNAME · User: $env:USERNAME · $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray
Write-Host "$banner`n" -ForegroundColor Cyan

$dist = Find-DistDir
Write-Host "[info] dist directory: $dist" -ForegroundColor DarkGray

Test-DistStructure -Dist $dist

switch ($Mode) {
    'Package' {
        if (-not $OutZip) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

            $outDirCandidates = @(
                'D:\TaskOn\infra',
                'D:\engine-host\infra',
                "$env:USERPROFILE\Desktop"
            )
            $outDir = $outDirCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
            if (-not $outDir) { $outDir = (Get-Location).Path }

            $OutZip = Join-Path $outDir "landing_free-diagnostic_$stamp.zip"
        }

        Invoke-Package -Dist $dist -OutPath $OutZip
        Write-DeployLog -Action 'Package' -Detail "Built $OutZip"

        Write-Host "`n----------------------------------------------------------------" -ForegroundColor Yellow
        Write-Host " 下一步:" -ForegroundColor Yellow
        Write-Host "   1. 把这个 zip 发给运维上传到 CF Pages 项目 '$ProjectName'" -ForegroundColor Yellow
        Write-Host "      → https://dash.cloudflare.com → Workers & Pages → $ProjectName → Create deployment → Upload assets" -ForegroundColor DarkGray
        Write-Host "   2. 运维上传完后等 30 秒，跑校验:" -ForegroundColor Yellow
        Write-Host "      .\deploy_landing.ps1 -Mode Verify" -ForegroundColor White
        Write-Host "----------------------------------------------------------------`n" -ForegroundColor Yellow
    }

    'Verify' {
        Test-MimeTypes -BaseUrl $VerifyBase
        Write-DeployLog -Action 'Verify' -Detail "All MIME OK on $VerifyBase"
    }

    'Deploy' {
        Invoke-WranglerDeploy -Dist $dist -Project $ProjectName
        Write-Host "`n[info] Waiting 30s for CDN cache..." -ForegroundColor DarkGray
        Start-Sleep -Seconds 30
        Test-MimeTypes -BaseUrl $VerifyBase
        Write-DeployLog -Action 'Deploy' -Detail "wrangler deploy + MIME OK"
    }
}

Write-Host "`n[done] $Mode completed.`n" -ForegroundColor Green
