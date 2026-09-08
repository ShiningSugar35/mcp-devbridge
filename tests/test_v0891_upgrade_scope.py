"""Upgrade process ownership: inspect/execute only a pure filter, never stop a process."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_upgrade.ps1"


def test_worker_uses_install_identity_before_any_taskkill() -> None:
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "function Get-UpgradeOwnedProcesses" in text
    worker = text[text.index("$oldPidValue = [int]$request.old_pid"):]
    assert "Get-UpgradeOwnedProcesses -InstallRoot $installDir" in worker
    assert "Upgrade parent is not an owned installation process." in worker
    assert worker.index("Get-UpgradeOwnedProcesses -InstallRoot $installDir") < worker.index("taskkill.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell ownership filter")
@pytest.mark.parametrize("empty_root", [False, True])
def test_filter_excludes_other_installs_and_unknown_paths(empty_root: bool) -> None:
    shell = shutil.which("powershell.exe")
    assert shell is not None
    source = str(SCRIPT).replace("'", "''")
    script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('__SOURCE__', [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Script parse failed' }
$fn = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-UpgradeOwnedProcesses'}, $true)
if (-not $fn) { throw 'Missing ownership filter' }
. ([ScriptBlock]::Create($fn.Extent.Text))
$items = @(
 [pscustomobject]@{Name='MCPDevBridge.exe';ProcessId=1;ExecutablePath='C:\fixture\bridge\MCPDevBridge.exe'},
 [pscustomobject]@{Name='mcpdevbridge.exe';ProcessId=2;ExecutablePath='c:\FIXTURE\bridge\MCPDevBridge.exe'},
 [pscustomobject]@{Name='MCPDevBridge.exe';ProcessId=3;ExecutablePath='C:\other\MCPDevBridge.exe'},
 [pscustomobject]@{Name='MCPDevBridge.exe';ProcessId=4;ExecutablePath=$null},
 [pscustomobject]@{Name='node.exe';ProcessId=5;ExecutablePath='C:\fixture\bridge\node.exe'}
)
if (__EMPTY__) {
 $failed = $false
 try { Get-UpgradeOwnedProcesses -InstallRoot '' -Processes $items | Out-Null } catch { $failed = $true }
 if (-not $failed) { throw 'Ambiguous root was accepted' }
} else {
 $result = @(Get-UpgradeOwnedProcesses -InstallRoot 'C:\fixture\bridge' -Processes $items)
 if (($result.ProcessId -join ',') -ne '1,2') { throw 'Unowned process was selected' }
}
Write-Output 'ownership filter passed; no stop/install/scheduled task executed'
""".replace("__SOURCE__", source).replace("__EMPTY__", "$true" if empty_root else "$false")
    completed = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
