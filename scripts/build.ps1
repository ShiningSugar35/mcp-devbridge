# Build script: Python tests -> ruff -> PyInstaller onedir -> smoke copy.
# Run from the project root in PowerShell:
#   .\scripts\build.ps1
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
Set-Location $root

Write-Host "== 1/4 鍗曞厓娴嬭瘯 =="
& .\.venv\Scripts\python.exe -m pytest tests/ -q
if ($LASTEXITCODE -ne 0) { throw "娴嬭瘯澶辫触" }

Write-Host "== 2/4 闈欐€佹鏌?=="
& .\.venv\Scripts\python.exe -m ruff check src tests
if ($LASTEXITCODE -ne 0) { throw "ruff 澶辫触" }

Write-Host "== 3/4 PyInstaller onedir =="
if (-not (Test-Path .\.venv\Scripts\pyinstaller.exe)) {
    & .\.venv\Scripts\python.exe -m pip install "pyinstaller>=6.11"
}
& .\.venv\Scripts\pyinstaller.exe packaging\local-dev-mcp-bridge.spec --noconfirm --clean 2>&1
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 澶辫触" }

Write-Host "== 4/4 浜х墿 =="
Get-ChildItem .\dist\MCPDevBridge | Select-Object Name, Length | Format-Table -AutoSize
Write-Host "鎵撳寘瀹屾垚锛歞ist\MCPDevBridge\MCPDevBridge.exe"
