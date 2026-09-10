"""Upgrade side-effect controls; extract pure PowerShell functions, never install."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/live_upgrade.ps1"


def test_project_artifacts_and_preserved_shortcuts_are_forwarded_to_worker() -> None:
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "[string]$ArtifactDirectory" in text
    assert "[switch]$PreserveShortcuts" in text
    assert "preserve_shortcuts = [bool]$PreserveShortcuts" in text
    assert 'Join-Path $RunDir "upgrade-worker-request.json"' in text
    assert 'Join-Path $RunDir "upgrade-worker.cmd"' in text
    assert '-ArtifactDirectory' in text[text.index('$workerCmd ='):]
    assert 'Join-Path $RunDir "upgrade-result.json"' in text
    assert 'Get-UpgradeInstallerArguments' in text
    assert 'if (-not [bool]$request.preserve_shortcuts)' in text
    assert '$env:TEMP = $previousTemp' in text and '$env:TMP = $previousTmp' in text
    assert 'Join-Path $ConfigDir "upgrade-resume.json"' in text


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parameter contract")
def test_artifact_scope_and_installer_arguments_are_safe() -> None:
    shell = shutil.which("powershell.exe")
    assert shell is not None
    command = r"""
$ErrorActionPreference='Stop'
$t=$null; $e=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$t,[ref]$e)
if($e.Count){throw 'parse failure'}
foreach($name in @('Resolve-UpgradeArtifactDirectory','Get-UpgradeInstallerArguments')) {
 $fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 if(-not $fn){throw "missing pure function $name"}
 . ([ScriptBlock]::Create($fn.Extent.Text))
}
$root='__ROOT__'
$inside=Join-Path $root '.ai-bridge\artifact-contract-no-write'
if((Resolve-UpgradeArtifactDirectory -ProjectRoot $root -Directory $inside) -ne $inside){throw 'inside rejected'}
foreach($bad in @('C:\outside','D:\outside',(Join-Path $root '..\escape'),($root+'-sibling\x'))) {
 $refused=$false
 try { Resolve-UpgradeArtifactDirectory -ProjectRoot $root -Directory $bad | Out-Null }catch{$refused=$true}
 if(-not $refused){throw 'outside path accepted'}
}
$keep=@(Get-UpgradeInstallerArguments -InstallRoot $root -PreserveShortcuts $true)
if($keep -notcontains '/NOICONS' -or $keep -contains '/TASKS=desktopicon'){throw 'preserve mode creates shortcuts'}
$normal=@(Get-UpgradeInstallerArguments -InstallRoot $root -PreserveShortcuts $false)
if($normal -notcontains '/TASKS=desktopicon'){throw 'legacy default changed'}
Write-Output 'pure artifact/installer contract passed; no files, task, or install created'
""".replace("__SOURCE__", str(SCRIPT).replace("'", "''")).replace("__ROOT__", str(ROOT).replace("'", "''"))
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
