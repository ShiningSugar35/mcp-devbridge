"""Bounded control of the separately owner-provisioned MCP relay, never provision it."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

from . import constants

SUPERVISOR_SHA256 = "cfa214603c2d98980ee364f42a60f80d7237838859d26132f89d5443e5e7a51a"
UNIT_SHA256 = "04cc42d93cfafaba11cca0780d94d16957bc7961bff4f10110f04662a539f17f"
TASK = "MCP DevBridge - SSH relay"


class RelayController:
    """Current Windows relay deployment only; other platforms fail explicitly."""

    def __init__(self) -> None:
        self.directory = constants.config_dir() / "relay"
        self.host = "opencode-server"

    @property
    def configured(self) -> bool:
        # Missing or changed files in an existing deployment are not "no relay".
        return self.directory.exists()

    @property
    def provisioned(self) -> bool:
        script = self.directory / "relay-supervisor.ps1"
        try:
            return sys.platform == "win32" and hashlib.sha256(script.read_bytes()).hexdigest() == SUPERVISOR_SHA256
        except OSError:
            return False

    @property
    def is_running(self) -> bool:
        try:
            state = json.loads((self.directory / "state.json").read_text(encoding="utf-8-sig"))
            process = psutil.Process(int(state["ssh_pid"]))
            expected = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
            return (
                state.get("state") == "forward_running"
                and Path(process.exe()) == expected
                and abs(process.create_time() - datetime.fromisoformat(state["ssh_started_at"]).timestamp()) < 0.01
            )
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            return False

    def _run(self, args: list[str], *, timeout: float = 30) -> str:
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("专用中转控制未完成，请检查服务器连接后重试。") from exc
        if result.returncode:
            # Never echo arbitrary SSH/scheduled-task output or credentials into the UI.
            raise RuntimeError(f"专用中转控制失败（退出码 {result.returncode}），原有项目保持运行。")
        return result.stdout

    def _task(self, action: str) -> None:
        if not self.provisioned:
            raise RuntimeError("这台电脑尚未配置受信任的服务器中转。")
        script = str(self.directory / "relay-supervisor.ps1").replace("'", "''")
        prelude = rf"""$ErrorActionPreference='Stop';
$t=Get-ScheduledTask -TaskName '{TASK}';
$me=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
$expectedExe=Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe';
$u=$t.Principal.UserId;
if(-not $u.StartsWith('S-1-')){{$u=([Security.Principal.NTAccount]$u).Translate([Security.Principal.SecurityIdentifier]).Value}};
if($u -ne $me -or $t.Principal.RunLevel -ne 'Limited' -or @($t.Actions).Count -ne 1 -or $t.Actions[0].Execute -ne $expectedExe -or
 $t.Actions[0].Arguments -ne '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script}"'){{throw 'Unrecognized MCP relay task'}};
"""
        if action == "check":
            command = ""
        elif action == "start":
            command = f"Enable-ScheduledTask -TaskName '{TASK}' | Out-Null; if($t.State -ne 'Running'){{Start-ScheduledTask -TaskName '{TASK}'}}"
        elif action == "stop":
            command = f"""Disable-ScheduledTask -TaskName '{TASK}' | Out-Null;
$end=(Get-Date).AddSeconds(20);
while((Get-ScheduledTask -TaskName '{TASK}').State -eq 'Running'){{
 if((Get-Date) -ge $end){{throw 'Relay did not stop normally'}}; Start-Sleep -Milliseconds 200
}}"""
        else:
            raise ValueError("Unknown relay action")
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self._run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", prelude + command])

    def _remote(self, start: bool | None) -> None:
        # The hash pins the exact dedicated unit authorized and installed in this deployment.
        check = f'test "$(sha256sum /etc/systemd/system/mcp-devbridge-relay.service | cut -d " " -f 1)" = {UNIT_SHA256}'
        if start is None:
            action = "true"
        elif start:
            action = "systemctl enable mcp-devbridge-relay.service && systemctl start mcp-devbridge-relay.service && systemctl is-active --quiet mcp-devbridge-relay.service"
        else:
            action = "systemctl disable mcp-devbridge-relay.service && systemctl stop mcp-devbridge-relay.service && ! systemctl is-active --quiet mcp-devbridge-relay.service"
        ssh = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
        self._run([str(ssh), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.host, f"{check} && {action}"])

    def validate(self) -> None:
        """Read-only preflight before disconnecting an already working path."""
        self._task("check")
        self._remote(None)

    def _mark(self, disabled: bool) -> None:
        path = self.directory / "mode.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"host": self.host, "unit_sha256": UNIT_SHA256, "remote_disabled": disabled}), encoding="utf-8")
        temp.replace(path)

    def start(self) -> None:
        if not self.provisioned:
            raise RuntimeError("这台电脑尚未配置受信任的服务器中转。")
        self._task("check")
        request = self.directory / "stop.request"
        request.unlink(missing_ok=True)
        self._task("start")
        deadline = time.monotonic() + 20
        while not self.is_running:
            if time.monotonic() >= deadline:
                raise RuntimeError("中转通道尚未建立，请检查服务器连接。")
            time.sleep(0.2)
        self._mark(False)  # Clear disabled proof before any remote activation.
        self._remote(True)

    def stop(self) -> None:
        if not self.configured:
            return
        self._task("check")
        try:
            marker = json.loads((self.directory / "mode.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        if marker != {"host": self.host, "unit_sha256": UNIT_SHA256, "remote_disabled": True}:
            self._remote(False)  # Keep working SSH if remote deactivation cannot be confirmed.
            self._mark(True)
        (self.directory / "stop.request").write_text("MCP connection mode change", encoding="ascii")
        self._task("stop")
