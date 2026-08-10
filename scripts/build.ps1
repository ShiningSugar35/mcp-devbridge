#Requires -Version 5.1
<#
.SYNOPSIS
    MCP DevBridge full build chain: unit tests -> lint/typecheck ->
    PyInstaller onedir -> Inno Setup installer -> artifact check.
.DESCRIPTION
    Run from the project root:
        .\scripts\build.ps1 [-Version X.Y.Z] [-SkipTests] [-SkipLint]
                            [-SkipInstaller] [-SkipVersionCheck]
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$SkipTests,
    [switch]$SkipLint,
    [switch]$SkipInstaller,
    [switch]$SkipVersionCheck
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

$script:root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$script:py = Join-Path $script:root ".venv\Scripts\python.exe"
$script:iscc = $env:ISCC

function Step([string]$msg) { Write-Host "== $msg ==" -ForegroundColor Cyan }

function Get-PyProjectVersion {
    $line = Get-Content (Join-Path $script:root "pyproject.toml") |
        Where-Object { $_ -match '^version\s*=\s*"([^"]+)"' } | Select-Object -First 1
    if ($line -and $line -match '^version\s*=\s*"([^"]+)"') { return $Matches[1] }
    throw "Could not read version from pyproject.toml"
}

# --- 1. Prerequisites -------------------------------------------------------
Step "1/6 prerequisites"
if (-not (Test-Path $script:py)) {
    throw "Missing $script:py - create the venv first:  python -m venv .venv"
}
$script:version = if ($Version) { $Version } else { Get-PyProjectVersion }
$script:distRoot = Join-Path $script:root ("dist\staging-" + $script:version)
$script:distDir = Join-Path $script:distRoot "MCPDevBridge"
$script:workDir = Join-Path $script:root ("build\staging-" + $script:version)
$script:appExe = Join-Path $script:distDir "MCPDevBridge.exe"
Write-Host "Building version: $script:version"
Write-Host "Staging output: $script:distDir"

if (-not $SkipVersionCheck) {
    $spec = Get-Content (Join-Path $script:root "packaging\local-dev-mcp-bridge.spec") -Raw
    if ($spec -notmatch ('PROJECT_VERSION\s*=\s*"' + [regex]::Escape($script:version) + '"')) {
        Write-Warning "packaging\local-dev-mcp-bridge.spec PROJECT_VERSION does not match $script:version"
    }
    $iss = Get-Content (Join-Path $script:root "scripts\installer.iss") -Raw
    if ($iss -notmatch ('#define\s+MyAppVersion\s+"' + [regex]::Escape($script:version) + '"')) {
        Write-Warning "scripts\installer.iss MyAppVersion does not match $script:version"
    }
}

# --- 2. Unit tests ----------------------------------------------------------
if (-not $SkipTests) {
    Step "2/6 unit tests (pytest)"
    & $script:py -m pytest tests/ -q
    if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
}

# --- 3. Lint / typecheck ----------------------------------------------------
if (-not $SkipLint) {
    Step "3/6 lint (ruff)"
    & $script:py -m ruff check src tests
    if ($LASTEXITCODE -ne 0) { throw "ruff failed" }

    Step "3b/6 typecheck (pyright)"
    & $script:py -m pyright --pythonpath $script:py src tests
    if ($LASTEXITCODE -ne 0) { throw "pyright failed" }
}

# --- 4. PyInstaller onedir --------------------------------------------------
Step "4/6 PyInstaller onedir"
& $script:py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller (venv has no pip module) ..."
    $uv = Join-Path $script:root ".venv\Scripts\uv.exe"
    if (Test-Path $uv) { & $uv pip install "pyinstaller>=6.11" }
    else { & uv pip install --python $script:py "pyinstaller>=6.11" }
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller install failed" }
}
& $script:py -m PyInstaller packaging\local-dev-mcp-bridge.spec `
    --noconfirm `
    --clean `
    --distpath $script:distRoot `
    --workpath $script:workDir
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path $script:appExe)) { throw "Build artifact missing: $script:appExe" }

# --- 5. Installer (Inno Setup) ----------------------------------------------
if (-not $SkipInstaller) {
    Step "5/6 Inno Setup installer"
    if (-not $script:iscc) {
        $candidates = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            "C:\Program Files\Inno Setup 6\ISCC.exe"
        )
        $script:iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $script:iscc -or -not (Test-Path $script:iscc)) {
        throw "ISCC.exe not found. Install Inno Setup 6 (winget install JRSoftware.InnoSetup) or set env ISCC"
    }
    $script:setupExe = Join-Path $script:root "release\MCPDevBridge-Setup-$script:version.exe"
    if (Test-Path $script:setupExe) { Remove-Item -LiteralPath $script:setupExe -Force }
    $sourceDefine = "/DMySourceDir=$script:distDir"
    & $script:iscc (Join-Path $script:root "scripts\installer.iss") $sourceDefine /Qp
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
    if (-not (Test-Path $script:setupExe)) { throw "Installer artifact missing: $script:setupExe" }
}

# --- 6. Artifacts ------------------------------------------------------------
Step "6/6 artifacts"
Get-ChildItem $script:distDir | Select-Object Name, Length | Format-Table -AutoSize
Write-Host "App: $script:appExe"
if (-not $SkipInstaller) { Write-Host "Installer: $script:setupExe" }
Write-Host "Build OK (version $script:version)"