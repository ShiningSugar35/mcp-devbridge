# Build the portable Windows GUI and console clients with one shared runtime.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create the project .venv and install the package dependencies first.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$name = 'MCP-Live-Agent'
$stage = Join-Path $project (".ai-bridge\live-agent-package-$stamp")
$dist = Join-Path $project 'dist'
$archiveBase = Join-Path $dist "$name-Windows-$stamp"
$archive = "$archiveBase.zip"
New-Item -ItemType Directory -Path $stage | Out-Null
New-Item -ItemType Directory -Path $dist -Force | Out-Null

$temp = Join-Path $stage 'temp'
New-Item -ItemType Directory -Path $temp | Out-Null
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$oldCache = $env:PYINSTALLER_CONFIG_DIR
$env:TEMP = $temp
$env:TMP = $temp
$env:PYINSTALLER_CONFIG_DIR = Join-Path $stage 'cache'

try {
    $launcher = Join-Path $stage 'agent.py'
    @'
from pathlib import Path
import sys

if sys.argv[1:] == ["--live-bridge-worker"]:
    from local_dev_mcp_bridge.live_bridge_worker import main
elif Path(sys.executable).stem.casefold() == "mcp-live-agent":
    from local_dev_mcp_bridge.live_agent import main
else:
    from local_dev_mcp_bridge.live_desktop import main

raise SystemExit(main())
'@ | Set-Content -LiteralPath $launcher -Encoding ASCII

    $spec = Join-Path $stage 'MCP-Live-Agent.spec'
    @'
from pathlib import Path

from local_dev_mcp_bridge.engines import find_node, find_uvx

stage = Path(SPECPATH)
project = stage.parents[1]
codexpro = project / "build" / "codexpro-runtime"
if not (codexpro / "dist" / "http.js").is_file() or not (codexpro / "node_modules").is_dir():
    raise RuntimeError("Build the original CodexPro production runtime first.")
node, uvx = Path(find_node()), Path(find_uvx())
if not node.is_file() or not uvx.is_file():
    raise RuntimeError("The Windows Node and uvx runtimes are required.")
data = [
    (str(node), "runtime"),
    (str(uvx), "runtime"),
    (str(codexpro / "dist"), "third_party/codexpro/dist"),
    (str(codexpro / "node_modules"), "third_party/codexpro/node_modules"),
    (str(codexpro / "package.json"), "third_party/codexpro"),
]
uv = uvx.with_name("uv.exe")
if uv.is_file():
    data.append((str(uv), "runtime"))
analysis = Analysis(
    [str(stage / "agent.py")], pathex=[str(project / "src")],
    datas=data, hiddenimports=["win32cred"],
)
pyz = PYZ(analysis.pure)

desktop = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="MCP-Live", console=False, upx=False,
)
console = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="MCP-Live-Agent", console=True, upx=False,
)
bundle = COLLECT(
    desktop, console, analysis.binaries, analysis.datas,
    name="MCP-Live-Agent", upx=False,
)
'@ | Set-Content -LiteralPath $spec -Encoding ASCII
    $payload = Join-Path $stage 'payload'
    & $python -B -m PyInstaller --workpath (Join-Path $stage 'build') `
        --distpath $payload $spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed. Inspect $stage."
    }

    $bundle = Join-Path $payload $name
    $exe = Join-Path $bundle "$name.exe"
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'MCP-Live.exe') -PathType Leaf)) {
        throw "The packaged graphical client is missing. Inspect $stage."
    }
    $helpOutput = & $exe --help
    if ($LASTEXITCODE -ne 0 -or ($helpOutput -join "`n") -notmatch 'endpoint workspace') {
        throw "The packaged client did not expose its expected CLI. Inspect $stage."
    }

    @'
MCP Live（Windows x64 便携端）

解压整个压缩包，保留两个 EXE 和 _internal 目录在一起。
打开 MCP-Live.exe，添加要共享的文件夹，选择文件夹权限，输入设备密钥并启动。
出现「已连接」后，可在已关联的 ChatGPT 账号中选 @MCP Live。
默认只读，其他能力按所选项目权限启动。点击「断开」或关闭窗口会停止 Live 接入与本次启动的引擎。
当前窗口可以复用已验证的固定密钥。关闭窗口后，下次重新输入。
固定密钥不会写入便携设置。服务地址和设备名称可在「高级设置」修改。

命令行入口
  .\MCP-Live-Agent.exe "https://live.shiningsugar.shop" "C:\path\to\shared-folder"

在终端输入固定设备密钥或一次性配对码。按 Ctrl+C 断开。
'@ | Set-Content -LiteralPath (Join-Path $bundle 'README.txt') -Encoding UTF8

    & $python -B -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2], sys.argv[3])" `
        $archiveBase $payload $name
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        throw "Could not create the portable ZIP. Inspect $stage."
    }

    $hash = Get-FileHash -LiteralPath $archive -Algorithm SHA256
    Write-Output "Archive: $archive"
    Write-Output "SHA256: $($hash.Hash)"
}
finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $env:PYINSTALLER_CONFIG_DIR = $oldCache
}
