"""Command execution tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from local_dev_mcp_bridge.shell import (
    find_powershell,
    kill_process_tree,
    run_command,
    run_program,
)


def _mk_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "工作 空间"
    ws.mkdir()
    return ws


class TestRunCommand:
    def test_success(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        command = "Write-Output 'hi'" if os.name == "nt" else "printf '%s\\n' hi"
        res = run_command(command, cwd=ws, timeout_seconds=30)
        assert res.exit_code == 0
        assert "hi" in res.stdout

    def test_failure(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        res = run_command("exit 3", cwd=ws, timeout_seconds=30)
        assert res.exit_code == 3

    def test_chinese_output(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        command = "Write-Output '中文输出测试'" if os.name == "nt" else "printf '%s\\n' '中文输出测试'"
        res = run_command(command, cwd=ws, timeout_seconds=30)
        assert res.exit_code == 0
        assert "中文输出测试" in res.stdout

    def test_env(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        command = (
            "Write-Output $env:MY_TEST_VAR"
            if os.name == "nt"
            else "printf '%s\\n' \"$MY_TEST_VAR\""
        )
        res = run_command(command, cwd=ws, env={"MY_TEST_VAR": "abc123"}, timeout_seconds=30)
        assert "abc123" in res.stdout

    def test_cwd(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        command = "(Get-Location).Path" if os.name == "nt" else "pwd"
        res = run_command(command, cwd=ws, timeout_seconds=30)
        assert str(ws).lower() in res.stdout.lower()

    def test_timeout_kills_tree(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        command = (
            "Start-Process ping -ArgumentList '-t','127.0.0.1'; Start-Sleep -Seconds 60"
            if os.name == "nt"
            else "sleep 60"
        )
        res = run_command(command, cwd=ws, timeout_seconds=3)
        assert res.timed_out
        if os.name == "nt":
            time.sleep(1)
            remaining = [
                p for p in psutil.process_iter(["name", "cmdline"])
                if p.info["name"] and "ping" in p.info["name"].lower()
                and p.info["cmdline"] and "-t" in p.info["cmdline"]
            ]
            assert len(remaining) == 0

    def test_output_truncation(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        command = (
            "1..5000 | ForEach-Object { Write-Output ('line-' + $_) }"
            if os.name == "nt"
            else "for i in $(seq 1 5000); do echo line-$i; done"
        )
        res = run_command(
            command,
            cwd=ws,
            timeout_seconds=60,
            max_output_chars=2000,
        )
        assert res.truncated
        assert res.original_stdout_len > 2000
        assert "line-1" in res.stdout
        assert "line-5000" in res.stdout


class TestRunProgram:
    def test_direct_program(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        code = "print('direct-ok')"
        res = run_program(sys.executable, ["-c", code], cwd=ws, timeout_seconds=30)
        assert res.exit_code == 0
        assert "direct-ok" in res.stdout

    def test_missing_executable(self, tmp_path: Path) -> None:
        ws = _mk_ws(tmp_path)
        res = run_program("definitely-not-a-real-tool-xyz", [], cwd=ws, timeout_seconds=10)
        assert res.exit_code == -1
        assert "失败" in res.stderr


class TestShellHelpers:
    def test_find_powershell(self) -> None:
        if os.name == "nt":
            assert find_powershell().lower().endswith(("powershell.exe", "pwsh.exe"))
        else:
            assert find_powershell()

    def test_kill_process_tree(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        time.sleep(1)
        assert proc.poll() is None
        assert kill_process_tree(proc.pid)
        proc.wait(timeout=10)
