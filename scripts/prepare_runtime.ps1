#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$ForceDownload
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Tools = Join-Path $Root ".tools"
$NodeVersion = "22.19.0"
$UvVersion = "0.11.25"
$CloudflaredVersion = "2026.7.3"
$NodeSha256 = "995a3fb3cefad590cd3f4b321532a4b9582fb9c6575320ed2e3e894caac3e362"

New-Item -ItemType Directory -Force $Tools | Out-Null

function Get-VersionLine {
    param([string]$Exe, [string[]]$Arguments)
    if (-not (Test-Path -LiteralPath $Exe)) { return "" }
    try {
        $text = & $Exe @Arguments 2>&1 | Select-Object -First 1
        return [string]$text
    }
    catch { return "" }
}

function Test-NodeRuntime {
    param([string]$Path)
    return (Get-VersionLine -Exe $Path -Arguments @("--version")) -eq "v$NodeVersion"
}

function Test-UvRuntime {
    param([string]$Path)
    return (Get-VersionLine -Exe $Path -Arguments @("--version")) -match ("^uvx? " + [regex]::Escape($UvVersion) + "(?:\s|$)")
}

function Test-CloudflaredRuntime {
    param([string]$Path)
    return (Get-VersionLine -Exe $Path -Arguments @("--version")) -match ("cloudflared version " + [regex]::Escape($CloudflaredVersion))
}

function Get-CommandPath {
    param([string]$Name)
    try {
        $command = Get-Command $Name -ErrorAction Stop | Select-Object -First 1
        return [string]$command.Source
    }
    catch { return "" }
}

$NodeTarget = Join-Path $Tools "node.exe"
if ($ForceDownload -or -not (Test-NodeRuntime $NodeTarget)) {
    $systemNode = Get-CommandPath "node.exe"
    if (-not $ForceDownload -and $systemNode -and (Test-NodeRuntime $systemNode)) {
        Copy-Item -LiteralPath $systemNode -Destination $NodeTarget -Force
    }
    else {
        $url = "https://nodejs.org/dist/v$NodeVersion/win-x64/node.exe"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $NodeTarget
    }
}
$nodeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $NodeTarget).Hash.ToLowerInvariant()
if ($nodeHash -ne $NodeSha256 -or -not (Test-NodeRuntime $NodeTarget)) {
    throw "Pinned Node.js runtime verification failed."
}
Write-Host "Node.js runtime ready: $NodeTarget (v$NodeVersion)"

$UvTarget = Join-Path $Tools "uv.exe"
$UvxTarget = Join-Path $Tools "uvx.exe"
if ($ForceDownload -or -not (Test-UvRuntime $UvTarget) -or -not (Test-UvRuntime $UvxTarget)) {
    $systemUv = Get-CommandPath "uv.exe"
    $systemUvx = Get-CommandPath "uvx.exe"
    if (
        -not $ForceDownload -and
        $systemUv -and $systemUvx -and
        (Test-UvRuntime $systemUv) -and
        (Test-UvRuntime $systemUvx)
    ) {
        Copy-Item -LiteralPath $systemUv -Destination $UvTarget -Force
        Copy-Item -LiteralPath $systemUvx -Destination $UvxTarget -Force
    }
    else {
        $zipUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
        $hashUrl = "$zipUrl.sha256"
        $tempDir = Join-Path $env:TEMP ("MCPDevBridge-uv-" + [Guid]::NewGuid().ToString("N"))
        $zipPath = Join-Path $tempDir "uv.zip"
        $hashPath = Join-Path $tempDir "uv.zip.sha256"
        New-Item -ItemType Directory -Force $tempDir | Out-Null
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zipPath
            Invoke-WebRequest -UseBasicParsing -Uri $hashUrl -OutFile $hashPath
            $expected = ((Get-Content -LiteralPath $hashPath -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
            if (-not $expected -or $actual -ne $expected) { throw "uv archive SHA-256 verification failed." }
            Expand-Archive -LiteralPath $zipPath -DestinationPath $tempDir -Force
            $downloadedUv = Get-ChildItem -LiteralPath $tempDir -Filter "uv.exe" -Recurse | Select-Object -First 1
            $downloadedUvx = Get-ChildItem -LiteralPath $tempDir -Filter "uvx.exe" -Recurse | Select-Object -First 1
            if (-not $downloadedUv -or -not $downloadedUvx) { throw "uv archive is missing uv.exe/uvx.exe." }
            Copy-Item -LiteralPath $downloadedUv.FullName -Destination $UvTarget -Force
            Copy-Item -LiteralPath $downloadedUvx.FullName -Destination $UvxTarget -Force
        }
        finally {
            Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
if (-not (Test-UvRuntime $UvTarget) -or -not (Test-UvRuntime $UvxTarget)) {
    throw "Pinned uv/uvx runtime verification failed."
}
Write-Host "uv runtime ready: $UvTarget / $UvxTarget ($UvVersion)"

$CloudflaredTarget = Join-Path $Tools "cloudflared.exe"
if ($ForceDownload -or -not (Test-CloudflaredRuntime $CloudflaredTarget)) {
    $systemCloudflared = Get-CommandPath "cloudflared.exe"
    if (-not $ForceDownload -and $systemCloudflared -and (Test-CloudflaredRuntime $systemCloudflared)) {
        Copy-Item -LiteralPath $systemCloudflared -Destination $CloudflaredTarget -Force
    }
    else {
        $url = "https://github.com/cloudflare/cloudflared/releases/download/$CloudflaredVersion/cloudflared-windows-amd64.exe"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $CloudflaredTarget
    }
}
if (-not (Test-CloudflaredRuntime $CloudflaredTarget)) {
    throw "Pinned cloudflared runtime verification failed."
}
Write-Host "cloudflared runtime ready: $CloudflaredTarget ($CloudflaredVersion)"

Write-Host "Portable runtime preparation OK."
