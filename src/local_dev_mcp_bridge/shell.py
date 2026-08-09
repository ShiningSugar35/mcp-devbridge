"""Shell / subprocess execution helpers (Windows-first)."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class CommandResult:
    command: str
    shell: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    truncated: bool
    original_stdout_len: int
    original_stderr_len: int


def find_powershell() -> str:
    """Prefer PowerShell 7 (pwsh.exe), fall back to Windows PowerShell."""
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found
    return "powershell.exe"


def powershell_version(shell_path: str | None = None) -> str:
    shell = shell_path or find_powershell()
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        version = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        return version[0] if version else "unknown"
    except Exception:
        return "unknown"


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    half = max_chars // 2
    return text[:half] + f"\n…[输出截断, 原始 {len(text)} 字符]…\n" + text[-half:], True


def build_powershell_command(command: str, shell: str | None = None) -> list[str]:
    shell_path = shell or find_powershell()
    # Force UTF-8 on the console stream so Chinese output / paths survive
    # cp1252/OEM runners (GitHub Actions windows-latest etc.).
    prefix = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8;"
    )
    return [
        shell_path,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        prefix + command,
    ]


def _run_with_tree_kill(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, bytes, bytes, bool]:
    """Popen + communicate with timeout; kills the whole process tree on timeout."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError:
        raise
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout_seconds)
        return proc.returncode, raw_out, raw_err, False
    except subprocess.TimeoutExpired:
        try:
            kill_process_tree(proc.pid)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
        raw_out, raw_err = proc.communicate(timeout=10)
        return proc.returncode, raw_out, raw_err, True


def run_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int = 600,
    env: dict[str, str] | None = None,
    max_output_chars: int = 60_000,
    shell: str | None = None,
) -> CommandResult:
    """Run a shell command in the given working directory."""
    start = time.monotonic()
    shell_path = shell or find_powershell()
    args = build_powershell_command(command, shell_path)
    environment = dict(os.environ)
    if env:
        environment.update(env)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUTF8", "1")
    try:
        exit_code, raw_out, raw_err, timed_out = _run_with_tree_kill(
            args,
            cwd=str(cwd),
            env=environment,
            timeout_seconds=timeout_seconds,
        )
    except OSError as exc:
        return CommandResult(
            command=command,
            shell=shell_path,
            exit_code=-1,
            stdout="",
            stderr=f"启动命令失败: {exc}",
            timed_out=False,
            duration_seconds=time.monotonic() - start,
            truncated=False,
            original_stdout_len=0,
            original_stderr_len=0,
        )

    def decode(data: bytes) -> str:
        for encoding in ("utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    stdout_text = decode(raw_out)
    stderr_text = decode(raw_err)
    stdout_text, truncated_out = _truncate(stdout_text, max_output_chars)
    stderr_text, truncated_err = _truncate(stderr_text, max_output_chars)
    return CommandResult(
        command=command,
        shell=shell_path,
        exit_code=exit_code,
        stdout=stdout_text,
        stderr=stderr_text,
        timed_out=timed_out,
        duration_seconds=round(time.monotonic() - start, 3),
        truncated=truncated_out or truncated_err,
        original_stdout_len=len(raw_out),
        original_stderr_len=len(raw_err),
    )


def kill_process_tree(pid: int) -> bool:
    """Kill a process and its descendants on Windows via taskkill /T /F."""
    if pid <= 0:
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except Exception:
        return False


def run_program(
    executable: str,
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int = 600,
    env: dict[str, str] | None = None,
    max_output_chars: int = 60_000,
) -> CommandResult:
    """Run an executable with an argument array (no shell parsing)."""
    start = time.monotonic()
    argv = [executable, *args]
    environment = dict(os.environ)
    if env:
        environment.update(env)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUTF8", "1")
    timed_out = False
    try:
        exit_code, raw_out, raw_err, timed_out = _run_with_tree_kill(
            argv,
            cwd=str(cwd),
            env=environment,
            timeout_seconds=timeout_seconds,
        )
    except OSError as exc:
        return CommandResult(
            command=" ".join(argv),
            shell="(direct)",
            exit_code=-1,
            stdout="",
            stderr=f"启动程序失败: {exc}",
            timed_out=False,
            duration_seconds=time.monotonic() - start,
            truncated=False,
            original_stdout_len=0,
            original_stderr_len=0,
        )

    def decode(data: bytes) -> str:
        for encoding in ("utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    stdout_text, truncated_out = _truncate(decode(raw_out), max_output_chars)
    stderr_text, truncated_err = _truncate(decode(raw_err), max_output_chars)
    return CommandResult(
        command=" ".join(argv),
        shell="(direct)",
        exit_code=exit_code,
        stdout=stdout_text,
        stderr=stderr_text,
        timed_out=timed_out,
        duration_seconds=round(time.monotonic() - start, 3),
        truncated=truncated_out or truncated_err,
        original_stdout_len=len(raw_out),
        original_stderr_len=len(raw_err),
    )


def detect_binaries() -> dict[str, str]:
    """Version strings for common development tools (empty string when absent)."""
    versions: dict[str, str] = {}
    for name in ("git", "python", "uv", "node", "npm"):
        try:
            result = subprocess.run(
                [name, "--version"],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
            versions[name] = result.stdout.decode("utf-8", errors="replace").strip().splitlines()[0]
        except Exception:
            versions[name] = ""
    return versions


__all__ = [
    "CommandResult",
    "find_powershell",
    "powershell_version",
    "build_powershell_command",
    "run_command",
    "run_program",
    "kill_process_tree",
    "detect_binaries",
    "CREATE_NO_WINDOW",
    "DETACHED_PROCESS",
    "CREATE_NEW_PROCESS_GROUP",
]