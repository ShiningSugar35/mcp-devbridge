"""Shell execution profiles: what commands may run and where they run.

Three profiles:

- ``safe``        — keep the original project-workspace behaviour (项目内命令
                    允许). Local tools stay permissive; the CodexPro engine
                    still applies its own safe-bash allowlist.
- ``developer``   — default. Only known developer executables may be the
                    first command (pytest/pyright/ruff/git/npm/uv/python ...),
                    plus a dangerous-pattern guard that blocks
                    system-destructive lines even when the first word is one
                    of the allowed tools.
- ``full_system`` — allow any command, but only after a one-time risk
                    confirmation (桌面首次启用确认；调用层执行强制)。

The dangerous-pattern guard is a hard stop in every profile. It deliberately
matches explicit destructive verbs (format / diskpart / shutdown / reboot /
reg delete / bcdedit ...) and recursive deletes aimed at drive roots or
system directories. It is conservative: a false positive only forces the
user to widen the profile once, a false negative could destroy the machine.
"""

from __future__ import annotations

import re
from typing import Literal

ExecutionProfile = Literal["safe", "developer", "full_system"]

PROFILE_NAMES: frozenset[str] = frozenset(ExecutionProfile.__args__)

DEFAULT_EXECUTION_PROFILE: str = "developer"

# First-word allowlist for the developer profile. The first token of a
# PowerShell line is the executable name; everything a normal coding
# workflow needs lives here. `python -m pytest`, `git -C`, `npm run`,
# `uv run ...` all pass because the guard only matches the first word.
DEVELOPER_EXECUTABLES: frozenset[str] = frozenset(
    {
        "pwsh", "powershell",
        "python", "python.exe", "py",
        "uv", "uvx",
        "pytest", "pyright", "ruff",
        "pip", "pip3",
        "node", "node.exe", "npm", "npx", "yarn", "pnpm", "bun",
        "dotnet", "cargo", "java", "mvn", "gradle", "go",
        "git", "git.exe",
        "set", "echo", "type", "dir", "ls", "cat",
        "Get-Content", "Get-ChildItem", "Test-Path",
        "Select-Object", "Measure-Object", "Where-Object",
        "date", "chcp", "ver", "whoami", "where", "where.exe",
    }
)

# Read-only CLI builtins allowed regardless of profile.
_ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {"echo", "type", "dir", "ls", "cat", "Get-Content", "Test-Path",
     "where", "chcp", "ver", "whoami", "date"}
)

_DANGEROUS_WORDS: tuple[str, ...] = (
    "format",      # format C: → 整盘格式化
    "diskpart",    # 分区/磁盘操作
    "shutdown",    # 关机
    "reboot",      # 重启
    "bcdedit",     # 引导配置
    "chkdsk",      # 磁盘修复
    "delpart",     # 删除分区
    "cleanmgr",    # 磁盘清理（可能误删）
    "msiexec",     # 软件安装/卸载
    "cipher",      # 磁盘加密/擦除
    "takeown",     # 夺权
    "icacls",      # ACL 修改
)

_DANGEROUS_WORD_RE = re.compile(
    r"(?<![\w.-])(" + "|".join(re.escape(w) for w in _DANGEROUS_WORDS) + r")\b",
    re.IGNORECASE,
)

_DRIVE_LETTER_RE = re.compile(r"\b[A-Za-z]:[\\/]", re.IGNORECASE)
_SYSTEM_DIR_RE = re.compile(
    r"(?i)\b[A-Za-z]:[\\/]"
    r"(?:windows|program\ files(?: \(x86\))?|programdata|system32|\$recycle\.bin|users)\b",
)
_WINDOWS_SYSTEM_DIR_RE = re.compile(
    r"(?i)[\\/](?:windows|program\ files(?: \(x86\))?|system32)\b"
)
_DRIVE_ROOT_RE = re.compile(r"\b[A-Za-z]:[\\/]*(?:\\s|$)", re.IGNORECASE)

_REG_DELETE_RE = re.compile(
    r"""(?ix)
    \breg\s+delete\b
    | \bRemove-Item\b.{0,80}\bHK(?:EY)?[A-Z]{2}\b
    """,
)

_FORMAT_DRIVE_RE = re.compile(
    r"""(?ix)
    \bformat\s+[A-Za-z]:[\\/0-9]*(?:\s|$)
    """,
)

# Recursive deletes aimed at drive roots or system directories. Python token
# logic, not a single regex: flag words + recursive flags + system targets.
_RECURSIVE_VERBS = frozenset({"del", "erase", "rm", "remove-item", "rd", "rmdir"})
_RECURSIVE_FLAGS = ("/s", "-s", "-r", "-rf", "-recurse", "-force", "-f", "/f", "/q", "-q")
_SYSTEM_PATH_MARKERS = (r"\windows", r"\system32", r"\program files", "$recycle", r"\users")


class ExecutionProfileError(PermissionError):
    """Raised when a command violates its execution profile."""


def normalize_first_word(command: str) -> str:
    """Lower-case base name of the first executable of ``command``."""
    command = command.strip()
    if not command:
        return ""
    word = command.split(None, 1)[0].strip("\"'")
    base = word.split("\\")[-1].split("/")[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _recursive_delete_risk(command: str) -> bool:
    """True when ``command`` recursively deletes a drive root / system dir.

    Conservative by design: `del /s C:`, `rd /s C:\\Windows`, `rm -rf /`,
    `Remove-Item -Recurse C:\\Windows` are all caught regardless of profile.
    Deleting a whole user profile (`C:\\Users`) is treated as system scope.
    """
    first = normalize_first_word(command)
    if first not in _RECURSIVE_VERBS:
        return False
    rest = command.strip()
    first_token = rest.split(None, 1)[0] if rest else ""
    after = rest[len(first_token):]
    recursive = any(flag in after.lower() for flag in _RECURSIVE_FLAGS)
    if not recursive:
        return False
    low = after.lower()
    if _DRIVE_ROOT_RE.search(rest):
        return True
    if _SYSTEM_DIR_RE.search(rest):
        return True
    if "/" in low and any(marker in low for marker in _SYSTEM_PATH_MARKERS):
        return True
    return low.lstrip().startswith(("/", "\\")) or any(
        tok.startswith("/") or tok.startswith("\\\\") for tok in low.lstrip().split()
    )


def check_execution(command: str, profile: str = DEFAULT_EXECUTION_PROFILE) -> tuple[bool, str]:
    """(allowed, reason) for ``command`` under ``profile``. Pure policy check."""
    if not command or not command.strip():
        return False, "命令为空。"
    profile = (profile or DEFAULT_EXECUTION_PROFILE).lower()
    if profile not in PROFILE_NAMES:
        profile = DEFAULT_EXECUTION_PROFILE

    if _FORMAT_DRIVE_RE.search(command):
        return False, "检测到磁盘格式化命令（format <盘符>），已拒绝。"
    if _recursive_delete_risk(command):
        return False, "检测到针对磁盘根/系统目录的递归删除命令，已拒绝。"
    if _REG_DELETE_RE.search(command):
        return False, "检测到注册表删除（reg delete / Remove-Item HK*），已拒绝。"
    bad = _DANGEROUS_WORD_RE.search(command)
    if bad:
        return False, f"检测到危险命令 `{bad.group(0)}`，已拒绝。"

    first = normalize_first_word(command)
    if first in _ALWAYS_ALLOWED:
        return True, "允许（基础安全命令）"
    if profile == "full_system":
        return True, "允许（full_system）"
    if profile == "safe":
        # safe 档保留当前项目工作区行为（CodexPro 引擎另行执行其 safe-bash
        # allowlist）；此处仅保留硬性危险命令拦截。
        return True, "允许（safe）"
    if first not in DEVELOPER_EXECUTABLES:
        return False, f"developer 档仅允许开发工具，`{first}` 不在名单中。"
    return True, "允许"


def enforce_full_system_confirmation(profile: str, *, confirmed: bool) -> None:
    """Raise unless ``full_system`` got its one-time confirmation.

    Call at the tool boundary before any command runs. Policy queries (like
    the CodexPro env mapping) stay pure and use ``check_execution`` only.
    """
    profile = (profile or DEFAULT_EXECUTION_PROFILE).lower()
    if profile == "full_system" and not confirmed:
        raise ExecutionProfileError(
            "full_system 档位需要一次性风险确认后才能执行命令；"
            "请先在桌面「启动风险确认」中确认，或切换到 developer 档。"
        )