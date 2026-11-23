# Build the portable Windows GUI and console clients with one shared runtime.
[CmdletBinding()]
param([string]$StagingRoot)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create the project .venv and install the package dependencies first.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$name = 'MCP-Live-Agent'
if (-not $StagingRoot) { $StagingRoot = Join-Path $project ".ai-bridge" }
$stage = Join-Path $StagingRoot ("live-agent-package-$stamp")
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
$oldBuildSource = $env:MCP_LIVE_BUILD_SOURCE
$env:MCP_LIVE_BUILD_SOURCE = $project
$env:TEMP = $temp
$env:TMP = $temp
$env:PYINSTALLER_CONFIG_DIR = Join-Path $stage 'cache'

try {
    $launcher = Join-Path $stage 'agent.py'
    @'
import sys

if sys.argv[1:] == ["--live-bridge-worker"]:
    from local_dev_mcp_bridge.live_bridge_worker import main
elif sys.argv[1:2] == ["--live-elevated-broker"]:
    from local_dev_mcp_bridge.live_elevation import broker_main

    raise SystemExit(broker_main(sys.argv[2:]))
elif sys.argv[1:] == ["--live-desktop"]:
    from local_dev_mcp_bridge.live_desktop import main
else:
    from local_dev_mcp_bridge.live_agent import main

raise SystemExit(main())
'@ | Set-Content -LiteralPath $launcher -Encoding ASCII

    $desktopLauncher = Join-Path $stage 'desktop.py'
    @'
from pathlib import Path
import ctypes
import os
import subprocess
import sys

agent = Path(sys.executable).parent / "_internal" / "MCP-Live-Agent.exe"
try:
    subprocess.Popen(
        [str(agent), "--live-desktop"],
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
except OSError:
    ctypes.windll.user32.MessageBoxW(
        None, "Extract the entire ZIP, then start MCP-Live.exe from that folder.",
        "MCP Live", 0x10,
    )
    raise SystemExit(1)
'@ | Set-Content -LiteralPath $desktopLauncher -Encoding ASCII

    $spec = Join-Path $stage 'MCP-Live-Agent.spec'
    @'
import os
from pathlib import Path

from local_dev_mcp_bridge.engines import find_node

stage = Path(SPECPATH)
project = Path(os.environ["MCP_LIVE_BUILD_SOURCE"])
codexpro = project / "build" / "codexpro-runtime"
if not (codexpro / "dist" / "http.js").is_file() or not (codexpro / "node_modules").is_dir():
    raise RuntimeError("Build the original CodexPro production runtime first.")
node = Path(find_node())
if not node.is_file():
    raise RuntimeError("The pinned Windows Node runtime is required.")
data = [
    (str(node), "runtime"),
    (str(codexpro / "package.json"), "third_party/codexpro"),
]
for directory in ("dist", "node_modules"):
    for member in (codexpro / directory).rglob("*"):
        if member.is_file() and member.suffix != ".map":
            destination = Path("third_party/codexpro") / member.parent.relative_to(codexpro)
            data.append((str(member), destination.as_posix()))
analysis = Analysis(
    [str(stage / "agent.py")], pathex=[str(project / "src")],
    datas=data, hiddenimports=["win32cred"],
)
pyz = PYZ(analysis.pure)

launcher = Analysis([str(stage / "desktop.py")], pathex=[str(stage)])
desktop = EXE(
    PYZ(launcher.pure), launcher.scripts, [], exclude_binaries=True,
    name="MCP-Live", console=False, upx=False,
)
console = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="MCP-Live-Agent", console=True, upx=False, contents_directory=".",
)
bundle = COLLECT(
    desktop, console, analysis.binaries, analysis.datas, launcher.binaries, launcher.datas,
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
    $exe = Join-Path $bundle "_internal\$name.exe"
    Move-Item -LiteralPath (Join-Path $bundle "$name.exe") -Destination $exe
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'MCP-Live.exe') -PathType Leaf)) {
        throw "The packaged graphical client is missing. Inspect $stage."
    }
    $helpOutput = & $exe --help
    if ($LASTEXITCODE -ne 0 -or ($helpOutput -join "`n") -notmatch 'endpoint workspace') {
        throw "The packaged client did not expose its expected CLI. Inspect $stage."
    }

    @'
MCP Live（Windows x64 便携端）

解压整个压缩包，保留 MCP-Live.exe 和 _internal 目录在一起。
双击 MCP-Live.exe 打开图形界面，添加要共享的文件夹，选择文件夹权限，输入设备密钥并启动。
出现「已连接」后，可在已关联的 ChatGPT 账号中选 @MCP Live。
工作区能力按所选项目权限启动。点击「断开」或关闭窗口会停止 Live 接入与本次启动的引擎。
当前窗口可以复用已验证的固定密钥。关闭窗口后，下次重新输入。
固定密钥不会写入便携设置。服务地址和设备名称可在「高级设置」修改。

选择「完全访问」并启动共享时，请在本机确认 Windows 管理员授权。断开共享会关闭本次管理员进程。
首次启用 Windows 控制且本机没有 uvx 时，程序会下载并校验其运行组件。
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
    $env:MCP_LIVE_BUILD_SOURCE = $oldBuildSource
}
