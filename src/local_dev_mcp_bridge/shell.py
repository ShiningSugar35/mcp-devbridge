"""Shell / subprocess execution helpers (Windows-first)."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from .platform_support import IS_WINDOWS, popen_platform_kwargs, run_platform_kwargs

CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if IS_WINDOWS else 0
DETACHED_PROCESS = int(getattr(subprocess, "DETACHED_PROCESS", 0)) if IS_WINDOWS else 0
CREATE_NEW_PROCESS_GROUP = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if IS_WINDOWS else 0


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


# ---------------------------------------------------------------------------
# Shell detection
# ---------------------------------------------------------------------------

_WINDOWS_POWERSHELL = Path(
    os.environ.get("WINDIR", r"C:\Windows")
) / r"System32\WindowsPowerShell\v1.0\powershell.exe"
_WINDOWS_CMD = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32\\cmd.exe"
_WINDOWS_BASH = Path(os.environ.get("WINDIR", r"C:\Windows")) / r"System32\bash.exe"


@dataclass(frozen=True)
class ShellInfo:
    name: str          # 展示名：pwsh / Windows PowerShell / cmd / Git Bash / WSL Bash
    path: str          # 可执行文件路径（或空表示未安装）
    kind: str          # pwsh | windows_powershell | cmd | bash | wsl_bash
    required_for: bool = True

    @property
    def executable(self) -> bool:
        return bool(self.path) and os.path.isfile(self.path)

    @property
    def is_wsl(self) -> bool:
        """True when this shell runs inside the Windows Subsystem for Linux."""
        is_windows_bash = (
            bool(self.path)
            and self.kind == "bash"
            and Path(self.path).resolve() == _WINDOWS_BASH.resolve()
        )
        return self.kind == "wsl" or is_windows_bash

    @property
    def version(self) -> str:
        """Human-readable shell version (PowerShell family only; cheap)."""
        if self.kind in ("pwsh", "windows_powershell"):
            return powershell_version(self.path)
        return ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "type": self.kind,
            "executable": self.executable,
            "is_wsl": self.is_wsl,
            "version": self.version,
        }


def detect_shells() -> list[ShellInfo]:
    """Detect shells available on this machine, in preference order."""
    if not IS_WINDOWS:
        shells: list[ShellInfo] = []
        seen: set[str] = set()
        preferred = os.environ.get("SHELL", "").strip()
        candidates = [preferred, shutil.which("bash") or "", shutil.which("zsh") or "", shutil.which("fish") or "", shutil.which("sh") or ""]
        for candidate in candidates:
            if not candidate:
                continue
            path = shutil.which(candidate) or candidate
            if not os.path.isfile(path):
                continue
            resolved = str(Path(path).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            base = Path(resolved).name.lower()
            kind = "bash" if base == "bash" else base
            shells.append(ShellInfo(name=base, path=resolved, kind=kind))
        return shells

    shells: list[ShellInfo] = []
    for name, kind, hint in (
        ("pwsh", "pwsh", "pwsh"),
        ("Windows PowerShell", "windows_powershell", str(_WINDOWS_POWERSHELL)),
        ("cmd", "cmd", str(_WINDOWS_CMD)),
        ("Git Bash", "bash", None),
        ("WSL Bash", "wsl", str(_WINDOWS_BASH)),
    ):
        if kind == "pwsh":
            path = shutil.which("pwsh") or ""
        elif kind == "bash":
            path = shutil.which("bash") or ""
            if path and Path(path).resolve() == _WINDOWS_BASH.resolve():
                path = ""
        elif kind == "wsl":
            path = hint if hint and os.path.isfile(hint) else (shutil.which("wsl") or "")
        elif kind == "windows_powershell":
            path = hint
        elif kind == "cmd":
            path = hint
            if path is None or not os.path.isfile(path):
                continue
        else:
            path = ""
        if not path:
            continue
        shells.append(ShellInfo(name=name, path=path, kind=kind))
    return shells


def default_shell() -> ShellInfo:
    """The shell a new / auto-configured session will use."""
    shells = detect_shells()
    if not IS_WINDOWS:
        if shells:
            bash = next((item for item in shells if item.kind == "bash"), None)
            return bash or shells[0]
        fallback = "/bin/sh" if Path("/bin/sh").is_file() else "sh"
        return ShellInfo(name="sh", path=fallback, kind="sh")

    order = {"pwsh": 0, "windows_powershell": 1, "cmd": 2, "bash": 3}
    best: ShellInfo | None = None
    for shell in shells:
        if shell.kind == "wsl":
            continue
        if best is None or order.get(shell.kind, 99) < order.get(best.kind, 99):
            best = shell
    if best is not None:
        return best
    return ShellInfo(name="cmd", path=str(_WINDOWS_CMD), kind="cmd")


def get_shell_info() -> dict[str, object]:
    """JSON-friendly report used by MCP tools, the GUI and the self-test."""
    default = default_shell()
    detected = [shell.to_dict() for shell in detect_shells()]
    return {
        "default": default.to_dict(),
        "detected": detected,
    }


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
            **run_platform_kwargs(),
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


def build_shell_command(command: str, shell: str | None = None) -> tuple[list[str], str]:
    """Build an argv for the platform default shell and return (argv, display_shell)."""
    if IS_WINDOWS:
        shell_path = shell or find_powershell()
        return build_powershell_command(command, shell_path), shell_path
    info = default_shell() if not shell else ShellInfo(Path(shell).name, shell, Path(shell).name.lower())
    shell_path = info.path or "/bin/bash"
    kind = info.kind.lower()
    if kind in {"bash", "zsh", "fish"}:
        return [shell_path, "-lc", command], shell_path
    return [shell_path, "-c", command], shell_path


def _run_with_tree_kill(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, bytes, bytes, bool]:
    """Popen + communicate with timeout; kills the whole process tree on timeout."""
    try:
        proc: subprocess.Popen[Any] = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=False,
            **popen_platform_kwargs(new_session=True),
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
    args, shell_path = build_shell_command(command, shell)
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
    """Terminate a process and descendants on Windows or POSIX."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=30,
                **run_platform_kwargs(),
            )
            return True
        except Exception:
            return False
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return True
    children = parent.children(recursive=True)
    for proc in children:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    with contextlib.suppress(psutil.Error):
        parent.terminate()
    _gone, alive = psutil.wait_procs([*children, parent], timeout=3)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(alive, timeout=3)
    return True


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
                **run_platform_kwargs(),
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
    "build_shell_command",
    "run_command",
    "run_program",
    "kill_process_tree",
    "detect_binaries",
    "CREATE_NO_WINDOW",
    "DETACHED_PROCESS",
    "CREATE_NEW_PROCESS_GROUP",
]