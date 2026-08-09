"""MCP tool implementations for the MCP DevBridge backend.

All tools raise ValueError / PermissionDeniedError for user-facing failures; the
MCPServer decorator converts these into MCP error results.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import platform
import re
import shutil
import stat
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from . import constants
from .engines import port_listening
from .execution_profile import (
    DEFAULT_EXECUTION_PROFILE,
    ExecutionProfileError,
    check_execution,
    enforce_full_system_confirmation,
)
from .models import ProjectConfig
from .permissions import PermissionError as PermissionDeniedError  # noqa: A004 - domain error type
from .permissions import PermissionMode, PermissionPolicy
from .processes import ProcessRegistry
from .shell import (
    detect_binaries,
    get_shell_info,
    powershell_version,
)
from .shell import (
    run_command as _run_command,
)
from .shell import (
    run_program as _run_program,
)

# ---------------------------------------------------------------------------
# Tool annotations (must be accurate; never label destructive as read-only)
# ---------------------------------------------------------------------------


def read_tool() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )


def read_non_idempotent_tool() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )


def write_tool() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )


def destructive_tool() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    )


def execute_tool() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )


SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "credentials.json",
        "service-account.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "cookie",
        "cookies.json",
    }
)

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "coverage",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".next",
        ".nuxt",
    }
)


class WorkspaceCatalog:
    """Per-session project bindings for the multi-project tools layer.

    One instance per backend process. A session is identified by the
    ``mcp-session-id`` request header (streamable HTTP transport); requests
    without one share the ``"<default>"`` bucket. ``switch_workspace`` changes
    ONLY the calling session's binding — other sessions keep their project.
    """

    DEFAULT_BUCKET = "<default>"

    def __init__(self, projects: Sequence[ProjectConfig], default_id: str | None = None) -> None:
        self._projects: dict[str, ProjectConfig] = {p.id: p for p in projects if p.id}
        self._order: list[str] = [p.id for p in projects if p.id]
        defaults = [p for p in projects if p.id == default_id]
        self._default = defaults[0] if defaults else (projects[0] if projects else None)
        self._bindings: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def session_key(ctx: Any) -> str:
        """Extract the MCP session id from a request context.

        The MCP SDK Context exposes a ``headers`` property that returns a
        Starlette ``Headers`` (MutableMapping, not dict). We iterate via
        ``.items()`` to cover dict, Mapping and list-of-tuple representations.
        """
        if ctx is None:
            return WorkspaceCatalog.DEFAULT_BUCKET
        headers = getattr(ctx, "headers", None)
        if headers is not None:
            try:
                for name, value in headers.items():
                    if str(name).lower() == "mcp-session-id" and value:
                        return str(value)
            except (TypeError, AttributeError):
                pass
        return WorkspaceCatalog.DEFAULT_BUCKET

    @property
    def default(self) -> ProjectConfig | None:
        return self._default

    def project(self, key: str) -> ProjectConfig | None:
        if self._default is None:
            return None
        with self._lock:
            project_id = self._bindings.get(key)
        if project_id is None or project_id not in self._projects:
            return self._default
        return self._projects[project_id]

    def bind(self, key: str, project_id: str) -> ProjectConfig:
        if project_id not in self._projects:
            raise ValueError(f"未找到项目：{project_id}。可用 list_projects 查看。")
        with self._lock:
            self._bindings[key] = project_id
        return self._projects[project_id]

    def projects(self) -> list[ProjectConfig]:
        order = self._default_order()
        return [self._projects[p] for p in order]

    def _default_order(self) -> list[str]:
        default_id = self._default.id if self._default is not None else ""
        ids = list(self._projects)
        if default_id in ids:
            ids.remove(default_id)
            ids.insert(0, default_id)
        return ids


class _SessionState:
    """Resolved per-session project state (policy + workspace + exclude set)."""

    __slots__ = ("project", "workspace", "policy", "excluded")

    def __init__(self, project: ProjectConfig) -> None:
        self.project = project
        self.workspace: Path = Path(project.root_path).expanduser().resolve()
        self.policy = PermissionPolicy(project.permission_mode, self.workspace)  # type: ignore[arg-type]
        self.excluded: frozenset[str] = frozenset(DEFAULT_EXCLUDED_DIRS)


class LocalDevTools:
    """Stateful registry of workspace tools; one instance per backend process."""

    def __init__(
        self,
        workspace: Path,
        permission_mode: PermissionMode = "workspace",
        *,
        test_command: str = "",
        lint_command: str = "",
        typecheck_command: str = "",
        build_command: str = "",
        shell: str = "auto",
        execution_profile: str = "developer",
        full_system_confirmed: bool = False,
        ignore_patterns: list[str] | None = None,
        max_file_bytes: int | None = None,
        registry: ProcessRegistry | None = None,
        projects: Sequence[ProjectConfig] | None = None,
        default_project_id: str | None = None,
    ) -> None:
        # 多项目：编目优先；未提供时按单项目语义构造（兼容既有调用/测试）。
        if projects and default_project_id is None:
            match = next((p for p in projects if _norm_eq(Path(p.root_path), workspace)), None)
            default_project_id = match.id if match is not None else projects[0].id
        if not projects or not all(p.id for p in projects):
            projects = [
                ProjectConfig(
                    id=default_project_id or "default",
                    display_name=workspace.name,
                    root_path=str(workspace.resolve()),
                    permission_mode=permission_mode,
                    test_command=test_command,
                    lint_command=lint_command,
                    typecheck_command=typecheck_command,
                    build_command=build_command,
                    shell=shell,
                )
            ]
        self._catalog = WorkspaceCatalog(projects, default_id=default_project_id)
        default_project = self._catalog.default
        assert default_project is not None, "LocalDevTools 总是至少有一个项目"  # noqa: S101
        self.workspace = Path(default_project.root_path).resolve()
        self.policy = PermissionPolicy(permission_mode, self.workspace)
        self.test_command = test_command
        self.lint_command = lint_command
        self.typecheck_command = typecheck_command
        self.build_command = build_command
        self.shell = shell
        self.execution_profile = execution_profile or DEFAULT_EXECUTION_PROFILE
        self.full_system_confirmed = full_system_confirmed
        self.ignore_patterns = list(ignore_patterns or [])
        self.max_file_bytes = max_file_bytes or constants.MAX_FILE_BYTES
        self.registry = registry or ProcessRegistry()
        self._excluded = set(DEFAULT_EXCLUDED_DIRS)
        self._excluded.update(p.strip().lower() for p in self.ignore_patterns if p.strip())
        self._session_cache: dict[tuple[str, str], _SessionState] = {}
        self._session_lock = threading.Lock()

    # --------------------------- helpers -----------------------------------
    def _require(self, category: str, ctx: Context | None = None) -> None:
        self._policy(ctx).require(category)

    # ------------------------------------------------- multi-project engine
    def _session_state(self, ctx: Any) -> _SessionState:
        """Resolve (and cache) the calling session's project state.

        The cache key is (session key, project id) so switching workspaces in
        one session re-resolves that session only.
        """
        key = WorkspaceCatalog.session_key(ctx)
        project = self._catalog.project(key)
        if project is None:
            raise PermissionDeniedError("未配置任何项目。")
        cache_key = (key, project.id)
        with self._session_lock:
            cached = self._session_cache.get(cache_key)
        if cached is not None:
            return cached
        state = _SessionState(project)
        state.excluded = frozenset(
            {*DEFAULT_EXCLUDED_DIRS, *(p.strip().lower() for p in project.ignore_patterns if p.strip())}
        )
        with self._session_lock:
            if len(self._session_cache) > 256:  # 防御性上限
                self._session_cache.clear()
            self._session_cache[cache_key] = state
        return state

    def _workspace(self, ctx: Context | None = None) -> Path:
        return self._session_state(ctx).workspace

    def _policy(self, ctx: Context | None = None) -> PermissionPolicy:
        return self._session_state(ctx).policy

    def _excluded_set(self, ctx: Context | None = None) -> frozenset[str]:
        return self._session_state(ctx).excluded

    def _guard_command(self, command: str) -> None:
        """Approve a command against the active execution profile.

        Destructive/system-modifying commands are rejected in every profile;
        ``full_system`` additionally requires the one-time confirmation done
        in the desktop (or a ``--confirm-full-system`` CLI flag).
        """
        try:
            enforce_full_system_confirmation(
                self.execution_profile, confirmed=self.full_system_confirmed
            )
        except ExecutionProfileError as exc:
            raise PermissionDeniedError(str(exc)) from None
        allowed, reason = check_execution(command, self.execution_profile)
        if not allowed:
            raise PermissionDeniedError(
                f"命令被执行档位拒绝：{reason}\n命令：{command[:200]}"
            )

    def _resolve(self, path: str | None, ctx: Context | None = None) -> Path:
        """Resolve user path; bounded to the session's workspace unless the
        session's project runs in system permission mode."""
        workspace = self._workspace(ctx)
        policy = self._policy(ctx)
        excluded = self._excluded_set(ctx)
        if not path or not str(path).strip():
            return workspace
        raw = Path(str(path)).expanduser()
        target = raw if raw.is_absolute() else workspace / raw
        resolved = target.resolve()
        if policy.mode != "system":
            try:
                resolved.relative_to(workspace)
            except ValueError:
                raise PermissionDeniedError(
                    f"路径在项目根目录之外：{resolved}。当前模式仅允许访问 {workspace}。"
                ) from None
            rel_parts = {part.lower() for part in resolved.relative_to(workspace).parts}
            if rel_parts.intersection(excluded):
                raise PermissionDeniedError("该路径位于默认排除目录中，请显式指定具体子路径。")
            if resolved.name.lower() in SENSITIVE_NAMES:
                raise PermissionDeniedError("该文件被识别为敏感文件，请通过明确的命令工具访问。")
        return resolved

    def _relative(self, target: Path, ctx: Context | None = None) -> str:
        try:
            return target.relative_to(self._workspace(ctx)).as_posix()
        except ValueError:
            return str(target)

    def _read_text(self, path: Path) -> str:
        data = path.read_bytes()
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return data.decode("utf-8", errors="replace")

    def _clip(self, text: str, max_chars: int | None = None) -> tuple[str, bool]:
        limit = max_chars or constants.MAX_TEXT_OUTPUT_CHARS
        if len(text) <= limit:
            return text, False
        half = limit // 2
        return (
            text[:half] + f"\n…[输出截断, 原始 {len(text)} 字符, 保留首尾各 {half}]…\n" + text[-half:],
            True,
        )

    def _fmt_result(self, res: Any) -> str:
        status = "超时" if res.timed_out else f"退出码 {res.exit_code}"
        out = [
            f"Shell: {res.shell}",
            f"状态: {status}",
            "",
            "STDOUT:",
            res.stdout,
            "",
            "STDERR:",
            res.stderr,
        ]
        if res.truncated:
            out.append(f"(stdout 原始 {res.original_stdout_len} 字节, stderr 原始 {res.original_stderr_len} 字节)")
        return "\n".join(out)

    # ----------------------------- info ------------------------------------
    def list_projects(self, ctx: Context | None = None) -> str:
        """列出所有项目（名称、路径、状态、CodexPro 端口）与当前会话绑定。"""
        self._require("info", ctx)
        workspace = self._workspace(ctx)
        key = WorkspaceCatalog.session_key(ctx)
        current = self._catalog.project(key)
        bound_id = current.id if current is not None else ""
        default_project = self._catalog.default
        default_id = default_project.id if default_project is not None else ""
        rows = [f"项目列表（会话 {key}）:", f"{'名称':<24} {'路径':<48} {'状态':<6} {'CodexPro端口':<12} 备注"]
        for project in self._catalog.projects():
            running = "运行中" if port_listening(project.codexpro_port or 0) else "未运行"
            mark = "← 当前绑定" if project.id == bound_id else ("默认" if project.id == default_id else "")
            rows.append(
                f"{project.display_name or Path(project.root_path).name:<24} "
                f"{project.root_path:<48} {running:<6} {project.codexpro_port or 0:<12} {mark}"
            )
        rows.append(f"当前项目: {workspace}")
        return "\n".join(rows)

    def switch_workspace(self, project_id: str, ctx: Context | None = None) -> str:
        """切换当前 MCP session 的操作项目；不影响其他 session 的绑定。"""
        self._require("info", ctx)
        key = WorkspaceCatalog.session_key(ctx)
        project = self._catalog.bind(key, project_id)
        # 该会话的缓存状态全部失效，保证后续工具立即解析到新项目
        with self._session_lock:
            self._session_cache = {k: v for k, v in self._session_cache.items() if k[0] != key}
        return (
            f"当前 MCP session（{key}）已切换到项目:\n"
            f"名称: {project.display_name or project.root_path}\n"
            f"路径: {project.root_path}\n"
            f"权限模式: {project.permission_mode}\n"
            f"CodexPro 端口: {project.codexpro_port or 0}（其他 session 不受影响）"
        )

    def shell_info(self) -> dict:
        """返回默认 Shell 与已检测 Shell 的信息（含 is_wsl 标记与版本）。"""
        return get_shell_info()

    def get_workspace_info(self, ctx: Context | None = None) -> str:
        """返回当前项目根目录、权限模式、Git 状态与工具版本。"""
        self._require("info", ctx)
        workspace = self._workspace(ctx)
        project = self._catalog.project(WorkspaceCatalog.session_key(ctx))
        project_name = project.display_name if project is not None else workspace.name
        branch = ""
        is_git = (workspace / ".git").exists()
        if is_git:
            res = _run_program(
                "git",
                ["-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workspace,
                timeout_seconds=30,
            )
            branch = res.stdout.strip() if res.exit_code == 0 else "(未知)"
        versions = detect_binaries()
        return "\n".join(
            [
                f"项目: {project_name}",
                f"项目根目录: {workspace}",
                f"权限模式: {self._policy(ctx).describe()}",
                f"是否 Git 仓库: {'是' if is_git else '否'}",
                f"当前分支: {branch or '(非 Git 仓库)'}",
                f"Python: {versions.get('python') or '未安装'}",
                f"Git: {versions.get('git') or '未安装'}",
                f"uv: {versions.get('uv') or '未安装'}",
                f"Node: {versions.get('node') or '未安装'}",
                f"npm: {versions.get('npm') or '未安装'}",
                f"默认测试命令: {self.test_command or '(未配置)'}",
                f"操作系统: {platform.platform()}",
            ]
        )

    def get_capabilities(self, ctx: Context | None = None) -> str:
        """返回当前允许的工具类型、写入/命令/项目外访问开关与输出限制。"""
        self._require("info", ctx)
        policy = self._policy(ctx)
        return "\n".join(
            [
                f"当前权限模式: {policy.describe()}",
                f"允许写入: {'是' if policy.allows_write else '否'}",
                f"允许命令执行: {'是' if policy.allows_commands else '否'}",
                f"允许项目外访问: {'是' if policy.allows_project_external else '否'}",
                f"单文件读取上限: {self.max_file_bytes} 字节",
                f"单次文本输出上限: {constants.MAX_TEXT_OUTPUT_CHARS} 字符",
                f"命令默认超时: {constants.DEFAULT_COMMAND_TIMEOUT_SECONDS} 秒",
                f"命令最大超时: {constants.MAX_COMMAND_TIMEOUT_SECONDS} 秒",
            ]
        )

    def get_system_info(self, ctx: Context | None = None) -> str:
        """返回 Windows 版本、CPU、内存、磁盘与开发工具版本。"""
        self._require("info", ctx)
        workspace = self._workspace(ctx)
        lines = [f"系统: {platform.platform()}", f"架构: {platform.machine()}"]
        try:
            du = shutil.disk_usage(workspace)
            lines.append(f"磁盘(项目分区): {du.free / 1024**3:.1f} GiB 可用 / {du.total / 1024**3:.1f} GiB")
        except OSError:
            pass
        versions = detect_binaries()
        for name in ("python", "git", "uv", "node", "npm"):
            label = {"python": "Python", "git": "Git", "uv": "uv", "node": "Node", "npm": "npm"}[name]
            lines.append(f"{label}: {versions.get(name) or '未安装'}")
        lines.append(f"PowerShell: {powershell_version()}")
        return "\n".join(lines)

    def shell_self_test(self, ctx: Context | None = None) -> str:
        """快速检测 shell 与关键开发命令是否可运行（供 AI 与桌面自查）。

        依次探测：默认 shell（可执行性）、python、git、pytest、pyright、ruff。
        返回逐项 ✓ / ✗ 与整体结论。
        """
        self._require("info", ctx)
        checks: list[tuple[str, str]] = []

        shell_info = get_shell_info()
        default = shell_info["default"]
        assert isinstance(default, dict)
        if default.get("executable"):
            checks.append(
                ("shell", f"默认 Shell: {default.get('name')} ({default.get('path')})")
            )
        else:
            checks.append(("shell", f"警告：默认 Shell 不可执行 ({default.get('path')})"))

        workspace = self._workspace(ctx)
        for name, arg, label in (
            ("python", "--version", "python"),
            ("git", "--version", "git"),
            ("pytest", "--version", "pytest"),
            ("pyright", "--version", "pyright"),
        ):
            if name in ("pytest", "pyright"):
                # uv trampoline 直接调用可能失败；改经当前解释器 -m 探测
                try:
                    res = _run_program(
                        sys.executable, ["-m", name, "--version"],
                        cwd=workspace, timeout_seconds=30,
                    )
                except Exception as exc:  # noqa: BLE001
                    checks.append((label, f"失败：{exc}"))
                    continue
                text = (res.stdout or res.stderr or "").strip().splitlines()
                checks.append(
                    (label, text[0] if text and res.exit_code == 0 else f"退出码 {res.exit_code}: {(text or ['(无输出)'])[0]}")
                )
                continue
            exe = shutil.which(name)
            if not exe:
                checks.append((label, "未安装（当前 PATH 中找不到）"))
                continue
            try:
                res = _run_program(exe, [arg], cwd=workspace, timeout_seconds=30)
                text = (res.stdout or res.stderr or "").strip().splitlines()
                detail = text[0] if text else "(无输出)"
                checks.append((label, detail if res.exit_code == 0 else f"退出码 {res.exit_code}: {detail}"))
            except Exception as exc:  # noqa: BLE001
                checks.append((label, f"失败：{exc}"))

        lines = [f"{label}: {detail}" for label, detail in checks]
        ok = all(
            not detail.startswith(("警告", "失败", "退出码"))
            and not detail.startswith("未安装")
            for _, detail in checks
        )
        lines.insert(
            0,
            "shell 自测：开发工具链 OK（Shell/Python/Git/pytest/pyright 可用）"
            if ok
            else "shell 自测：部分工具缺失或异常，AI 的 测试/检查 步骤可能失败。",
        )
        return "\n".join(lines)

    def get_environment_variable(self, name: str) -> dict:
        """读取单个环境变量（名称含 KEY/TOKEN/SECRET/PASSWORD/COOKIE/AUTH 时值被遮罩）。"""
        self._require("info")
        if not name or not name.strip():
            raise ValueError("环境变量名不能为空。")
        value = os.environ.get(name)
        if value is None:
            raise ValueError(f"环境变量不存在：{name}")
        masked = _looks_sensitive(name)
        shown = "[已遮罩]" if masked else value
        return {"name": name, "value": shown, "masked": masked}

    # ------------------------- file reading --------------------------------
    def list_directory(
        self,
        path: str = "",
        recursive: bool = False,
        max_depth: int = 3,
        max_entries: int = 500,
        include_hidden: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """列出目录内容，可递归；返回项目相对路径列表。"""
        self._require("file_read", ctx)
        excluded = self._excluded_set(ctx)
        base = self._resolve(path, ctx)
        if not base.exists():
            raise ValueError(f"路径不存在: {base}")
        if not base.is_dir():
            raise ValueError(f"不是目录: {base}")
        max_entries = max(1, min(int(max_entries), constants.MAX_DIRECTORY_ENTRIES))
        max_depth = max(0, min(int(max_depth), 8))
        results: list[str] = []

        def walk(dir_path: Path, depth: int) -> None:
            if depth > max_depth or len(results) >= max_entries:
                return
            try:
                entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError:
                return
            for entry in entries:
                if len(results) >= max_entries:
                    return
                if entry.name.startswith(".") and not include_hidden:
                    continue
                if entry.name.lower() in excluded and entry.is_dir():
                    continue
                results.append(self._relative(entry, ctx) + ("/" if entry.is_dir() else ""))
                if entry.is_dir():
                    walk(entry, depth + 1)

        if recursive:
            walk(base, 0)
        else:
            try:
                entries = sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError as exc:
                raise ValueError(f"读取目录失败: {exc}") from exc
            for entry in entries:
                if entry.name.startswith(".") and not include_hidden:
                    continue
                results.append(self._relative(entry, ctx) + ("/" if entry.is_dir() else ""))

        text = "\n".join(results) if results else "(空目录)"
        if len(results) >= max_entries:
            text += f"\n…[达到条目上限 {max_entries}]"
        return text

    def stat_path(self, path: str = "", ctx: Context | None = None) -> str:
        """返回文件/目录类型、大小、修改时间、只读与符号链接状态。"""
        self._require("file_read", ctx)
        target = self._resolve(path, ctx)
        if not target.exists():
            raise ValueError(f"路径不存在: {target}")
        st = target.stat()
        return "\n".join(
            [
                f"路径: {self._relative(target, ctx)}",
                f"类型: {'目录' if target.is_dir() else '文件'}",
                f"大小(字节): {st.st_size}",
                f"修改时间: {_fmt_time(st.st_mtime)}",
                f"符号链接: {'是' if target.is_symlink() else '否'}",
                f"只读: {'是' if is_readonly(target) else '否'}",
            ]
        )

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 400, ctx: Context | None = None) -> str:
        """读取文本文件（自动尝试 UTF-8/UTF-8 BOM/GB18030），带行号。"""
        self._require("file_read", ctx)
        target = self._resolve(path, ctx)
        if not target.exists():
            raise ValueError(f"文件不存在: {target}")
        if not target.is_file():
            raise ValueError(f"不是文件: {target}")
        if target.stat().st_size > self.max_file_bytes:
            raise ValueError(
                f"文件超过读取上限（{self.max_file_bytes} 字节），请使用命令工具按需读取。"
            )
        text = self._read_text(target)
        lines = text.splitlines()
        start = max(1, int(start_line))
        limit = max(1, min(int(max_lines), 2000))
        begin = min(start - 1, len(lines))
        end = min(len(lines), begin + limit)
        selected = [f"{i + 1}: {lines[i]}" for i in range(begin, end)]
        body, truncated = self._clip("\n".join(selected))
        header = f"文件: {self._relative(target, ctx)}\n行 {begin + 1}-{end} / 共 {len(lines)} 行\n"
        tail = f"\n…[其余 {len(lines) - end} 行未读，可用 start_line={end + 1} 继续]…" if end < len(lines) else ""
        return header + body + tail

    def read_files(self, paths: list[str], start_line: int = 1, max_lines: int = 200, ctx: Context | None = None) -> str:
        """批量读取多个小文件（最多 20 个，单文件 ≤64KB，总量 ≤60000 字符）。"""
        self._require("file_read", ctx)
        if not paths:
            raise ValueError("paths 不能为空。")
        if len(paths) > constants.MAX_READ_FILES_COUNT:
            raise ValueError(f"一次最多读取 {constants.MAX_READ_FILES_COUNT} 个文件。")
        blocks: list[str] = []
        total = 0
        for ptext in paths:
            target = self._resolve(ptext, ctx)
            if not target.is_file():
                raise ValueError(f"文件不存在: {target}")
            if target.stat().st_size > constants.MAX_READ_FILES_PER_FILE_BYTES:
                raise ValueError(
                    f"文件 {target.name} 超过单文件上限 {constants.MAX_READ_FILES_PER_FILE_BYTES} 字节。"
                )
            text = self._read_text(target)
            lines = text.splitlines()
            begin = min(max(1, int(start_line)) - 1, len(lines))
            end = min(len(lines), begin + max(int(max_lines), 1))
            block = f"### {self._relative(target, ctx)}\n" + "\n".join(
                f"{i + 1}: {lines[i]}" for i in range(begin, end)
            )
            total += len(block)
            if total > constants.MAX_READ_FILES_TOTAL_CHARS:
                raise ValueError("批量读取内容超出总输出上限，请减少文件数或行数。")
            blocks.append(block)
        text, truncated = self._clip("\n\n".join(blocks))
        return text

    def find_files(
        self,
        path: str = "",
        pattern: str = "*",
        name_contains: str = "",
        extension: str = "",
        max_results: int = 100,
        use_default_ignore: bool = True,
        ctx: Context | None = None,
    ) -> str:
        """按 glob/关键词/扩展名查找文件；默认排除 .git/.venv/node_modules 等。"""
        self._require("file_read", ctx)
        base = self._resolve(path, ctx)
        if not base.exists():
            raise ValueError(f"路径不存在: {base}")
        max_results = max(1, min(int(max_results), constants.MAX_SEARCH_RESULTS))
        excluded_set = self._excluded_set(ctx) if use_default_ignore else frozenset()
        ext = extension[1:].lower() if extension.startswith(".") else extension.lower()
        results: list[str] = []

        def walk(dir_path: Path) -> None:
            if len(results) >= max_results:
                return
            try:
                entries = sorted(dir_path.iterdir(), key=lambda e: e.name.lower())
            except OSError:
                return
            for entry in entries:
                if len(results) >= max_results:
                    return
                if entry.is_dir():
                    if entry.name.lower() in excluded_set:
                        continue
                    walk(entry)
                elif entry.is_file():
                    name = entry.name
                    if name_contains and name_contains.lower() not in name.lower():
                        continue
                    if ext and not name.lower().endswith("." + ext):
                        continue
                    if pattern != "*" and not _glob_match(name, pattern):
                        continue
                    results.append(self._relative(entry, ctx))

        walk(base)
        text = "\n".join(results) if results else "未找到匹配文件。"
        if len(results) >= max_results:
            text += f"\n…[达到结果上限 {max_results}]"
        return text

    def search_text(
        self,
        query: str,
        path: str = "",
        regex: bool = False,
        case_sensitive: bool = False,
        file_glob: str = "",
        max_results: int = 100,
        ctx: Context | None = None,
    ) -> str:
        """在文本文件中搜索字符串或正则，返回 文件:行号: 内容。"""
        self._require("file_read", ctx)
        if not query:
            raise ValueError("query 不能为空。")
        base = self._resolve(path, ctx)
        if not base.exists():
            raise ValueError(f"路径不存在: {base}")
        max_results = max(1, min(int(max_results), constants.MAX_SEARCH_RESULTS))
        compiled: re.Pattern[str] | None = None
        if regex:
            try:
                compiled = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"正则表达式无效: {exc}") from exc
        needle = query if case_sensitive else query.lower()
        workspace = self._workspace(ctx)
        excluded_set = self._excluded_set(ctx)
        hits: list[str] = []

        iterator = [base] if base.is_file() else (e for e in base.rglob("*") if e.is_file())
        for file_path in iterator:
            if len(hits) >= max_results:
                break
            if file_glob and not _glob_match(file_path.name, file_glob):
                continue
            if self._policy(ctx).mode != "system":
                try:
                    rel_parts = {p.lower() for p in file_path.relative_to(workspace).parts}
                except ValueError:
                    continue
                if rel_parts.intersection(excluded_set):
                    continue
                if file_path.name.lower() in SENSITIVE_NAMES:
                    continue
            try:
                if file_path.stat().st_size > self.max_file_bytes:
                    continue
                content = self._read_text(file_path)
            except OSError:
                continue
            for idx, line in enumerate(content.splitlines(), 1):
                if regex:
                    if compiled and not compiled.search(line):  # compiled 仅在 regex 时非 None
                        continue
                elif case_sensitive:
                    if needle not in line:
                        continue
                else:
                    if needle not in line.lower():
                        continue
                hits.append(f"{self._relative(file_path, ctx)}:{idx}: {line.strip()}")
                if len(hits) >= max_results:
                    break
        text = "\n".join(hits) if hits else "(无匹配)"
        if len(hits) >= max_results:
            text += f"\n…[达到结果上限 {max_results}]"
        return text

    # ------------------------- file writing --------------------------------
    def write_file(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: str = "",
        ctx: Context | None = None,
    ) -> str:
        """写入文本文件（原子写入）；overwrite=true 才覆盖已存在文件。"""
        self._require("file_write", ctx)
        target = self._resolve(path, ctx)
        if target.exists():
            if not overwrite:
                raise ValueError("文件已存在。确认覆盖时请设置 overwrite=true。")
            if expected_sha256:
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != expected_sha256:
                    raise ValueError(
                        f"现状文件 SHA256 与 expected_sha256 不一致（{actual[:12]}…），未写入。"
                    )
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        atomically_write(target, data)
        return f"已写入 {self._relative(target, ctx)}（{len(data)} 字节）"

    def replace_text(self, path: str, old_text: str, new_text: str, expected_count: int = 1, ctx: Context | None = None) -> str:
        """精确文本替换；匹配数必须等于 expected_count 才会修改。"""
        self._require("file_write", ctx)
        target = self._resolve(path, ctx)
        if not target.is_file():
            raise ValueError(f"文件不存在: {target}")
        content = self._read_text(target)
        actual = content.count(old_text)
        if actual != int(expected_count):
            raise ValueError(f"预期 {expected_count} 处匹配，实际 {actual} 处。未做任何修改。")
        updated = content.replace(old_text, new_text, int(expected_count))
        atomically_write(target, updated.encode("utf-8"))
        return f"已替换 {self._relative(target, ctx)}，共 {int(expected_count)} 处。"

    def apply_patch(self, diff: str, ctx: Context | None = None) -> str:
        """应用 unified diff（校验上下文；任一 hunk 不匹配则全部不修改）。"""
        self._require("file_write", ctx)
        if not diff or not diff.strip():
            raise ValueError("diff 内容为空。")
        patches = parse_unified_diff(diff)
        if not patches:
            raise ValueError("无法解析 diff（需要至少一个 @@ hunk）。")
        applied: list[str] = []
        for patch in patches:
            file_b = patch["file_b"]
            clean = file_b[2:] if file_b.startswith(("a/", "b/")) else file_b
            target = self._resolve(clean, ctx)
            content = self._read_text(target) if target.is_file() else ""
            new_content, hunk_count = apply_unified_patch(content, patch["hunks"])
            target.parent.mkdir(parents=True, exist_ok=True)
            atomically_write(target, new_content.encode("utf-8"))
            applied.append(f"{self._relative(target, ctx)}（{hunk_count} 个 hunk）")
        return "已应用补丁: " + "; ".join(applied)

    def make_directory(self, path: str, ctx: Context | None = None) -> str:
        """创建目录（自动创建父目录）。"""
        self._require("file_write", ctx)
        target = self._resolve(path, ctx)
        target.mkdir(parents=True, exist_ok=True)
        return f"已确保目录存在: {self._relative(target, ctx)}"

    def copy_path(self, source: str, destination: str, ctx: Context | None = None) -> str:
        """复制文件或目录。"""
        self._require("file_write", ctx)
        src = self._resolve(source, ctx)
        if not src.exists():
            raise ValueError(f"路径不存在: {src}")
        dst = self._resolve(destination, ctx)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return f"已复制 {self._relative(src, ctx)} → {self._relative(dst, ctx)}"

    def move_path(self, source: str, destination: str, overwrite: bool = False, ctx: Context | None = None) -> str:
        """移动文件或目录。"""
        self._require("file_write", ctx)
        src = self._resolve(source, ctx)
        if not src.exists():
            raise ValueError(f"路径不存在: {src}")
        dst = self._resolve(destination, ctx)
        if dst.exists() and not overwrite:
            raise ValueError("目标已存在，请设置 overwrite=true 或先删除目标。")
        shutil.move(str(src), str(dst))
        return f"已移动 {self._relative(src, ctx)} → {self._relative(dst, ctx)}"

    def delete_path(self, path: str, recursive: bool = False, ctx: Context | None = None) -> str:
        """删除文件或目录（目录需 recursive=true；项目全权限下不弹本地确认）。"""
        self._require("file_delete", ctx)
        target = self._resolve(path, ctx)
        if not target.exists():
            raise ValueError(f"路径不存在: {target}")
        workspace = self._workspace(ctx)
        if target.is_dir():
            if not recursive:
                raise ValueError("删除目录需要 recursive=true。")
            if _is_within(target, workspace) and target == workspace:
                raise ValueError("不允许删除项目根目录本身。")
            safe_delete_dir(target)
        else:
            target.unlink()
        return f"已删除 {self._relative(target, ctx)}"

    # ----------------------------- git -------------------------------------
    def _git(self, args: list[str], category: str, ctx: Any, timeout: int = 120) -> str:
        self._require(category, ctx)
        workspace = self._workspace(ctx)
        result = _run_program(
            "git",
            ["-C", str(workspace), *args],
            cwd=workspace,
            timeout_seconds=timeout,
        )
        lines = [f"命令: git {' '.join(args)}", f"退出码: {result.exit_code}"]
        if result.timed_out:
            lines.append("(超时)")
        if result.stdout:
            lines += ["STDOUT:", result.stdout]
        if result.stderr:
            lines += ["STDERR:", result.stderr]
        return "\n".join(lines)

    def git_status(self, ctx: Context | None = None) -> str:
        """显示 Git 工作区状态（只读）。"""
        return self._git(["status", "--short", "--branch"], "git_read", ctx, 60)

    def git_diff(self, path: str = "", staged: bool = False, context_lines: int = 3, ctx: Context | None = None) -> str:
        """显示 Git 差异（可选 staged 或指定文件）。"""
        args = ["diff"]
        if staged:
            args.append("--cached")
        args.append(f"-U{max(0, int(context_lines))}")
        if path.strip():
            args += ["--", self._relative(self._resolve(path, ctx), ctx)]
        return self._git(args, "git_read", ctx, 120)

    def git_log(self, max_count: int = 20, path: str = "", ctx: Context | None = None) -> str:
        """显示 Git 提交历史（只读）。"""
        args = [
            "log",
            "--pretty=format:%h %ad %s",
            "--date=short",
            "-n",
            str(max(1, min(int(max_count), 100))),
        ]
        if path.strip():
            args += ["--", self._relative(self._resolve(path, ctx), ctx)]
        return self._git(args, "git_read", ctx, 60)

    def git_branch(self, action: str = "list", name: str = "", ctx: Context | None = None) -> str:
        """查看(list)或创建(create) Git 分支。"""
        if action == "list":
            return self._git(["branch", "-a"], "git_read", ctx, 30)
        if action == "create":
            if not name.strip():
                raise ValueError("创建分支需要 name 参数。")
            return self._git(["branch", name], "git_write", ctx, 30)
        raise ValueError("action 仅支持 list/create。")

    def git_add(self, paths: list[str] | None = None, add_all: bool = False, ctx: Context | None = None) -> str:
        """将文件加入 Git 暂存区。"""
        if add_all:
            return self._git(["add", "-A"], "git_write", ctx, 60)
        if not paths:
            raise ValueError("需提供 paths 或设置 all=true。")
        args = ["add", *[self._relative(self._resolve(p, ctx), ctx) for p in paths]]
        return self._git(args, "git_write", ctx, 60)

    def git_commit(self, message: str, paths: list[str] | None = None, allow_empty: bool = False, ctx: Context | None = None) -> str:
        """创建 Git 提交。"""
        if not message.strip():
            raise ValueError("提交信息不能为空。")
        args = ["commit", "-m", message]
        if paths:
            args += ["--", *[self._relative(self._resolve(p, ctx), ctx) for p in paths]]
        if allow_empty:
            args.append("--allow-empty")
        return self._git(args, "git_write", ctx, 60)

    def git_checkout(self, ref: str, ctx: Context | None = None) -> str:
        """切换 Git 分支或提交（会改变工作区状态）。"""
        if not ref.strip():
            raise ValueError("ref 不能为空。")
        return self._git(["checkout", ref], "git_write", ctx, 60)

    def git_restore(self, paths: list[str], staged: bool = False, ctx: Context | None = None) -> str:
        """恢复文件：staged=false 丢弃未提交更改；staged=true 从暂存区恢复。"""
        if not paths:
            raise ValueError("paths 不能为空。")
        args = ["restore"]
        if staged:
            args.append("--staged")
        args.extend(self._relative(self._resolve(p, ctx), ctx) for p in paths)
        return self._git(args, "git_write", ctx, 60)

    def git_push(self, remote: str = "origin", branch: str = "", ctx: Context | None = None) -> str:
        """推送提交到远程仓库（高风险写操作）。"""
        args = ["push", remote]
        if branch:
            args.append(branch)
        return self._git(args, "git_write", ctx, 600)

    # -------------------------- commands -----------------------------------
    def run_command(
        self,
        command: str,
        cwd: str = "",
        timeout_seconds: int = 0,
        max_output_chars: int = 0,
        environment: dict[str, str] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """执行 Shell 命令（默认 PowerShell，绝不默认 WSL）；返回 shell、退出码与 stdout/stderr。"""
        self._require("command", ctx)
        if not command.strip():
            raise ValueError("command 不能为空。")
        self._guard_command(command)
        timeout = _clamp_int(timeout_seconds, constants.DEFAULT_COMMAND_TIMEOUT_SECONDS)
        max_output = _clamp_int(max_output_chars, constants.DEFAULT_COMMAND_OUTPUT_CHARS, 200_000)
        workspace = self._workspace(ctx)
        workdir = self._resolve(cwd, ctx) if cwd.strip() else workspace
        if not workdir.is_dir():
            raise ValueError(f"工作目录不存在: {workdir}")
        res = _run_command(
            command,
            cwd=workdir,
            timeout_seconds=timeout,
            max_output_chars=max_output,
            env=environment,
            shell=self.shell if self.shell != "auto" else None,
        )
        return self._fmt_result(res)

    def run_program(
        self,
        executable: str,
        args: list[str] | None = None,
        cwd: str = "",
        timeout_seconds: int = 0,
        environment: dict[str, str] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """直接运行程序（参数数组，不经 shell 解析）。"""
        self._require("command", ctx)
        if not executable.strip():
            raise ValueError("executable 不能为空。")
        full = executable + (" " + " ".join(args or []) if args else "")
        self._guard_command(full)
        timeout = _clamp_int(timeout_seconds, constants.DEFAULT_COMMAND_TIMEOUT_SECONDS)
        workspace = self._workspace(ctx)
        workdir = self._resolve(cwd, ctx) if cwd.strip() else workspace
        if not workdir.is_dir():
            raise ValueError(f"工作目录不存在: {workdir}")
        res = _run_program(
            executable,
            list(args or []),
            cwd=workdir,
            timeout_seconds=timeout,
            env=environment,
        )
        return self._fmt_result(res)

    # ------------------------- processes -----------------------------------
    def start_process(
        self,
        executable: str,
        args: list[str] | None = None,
        cwd: str = "",
        label: str = "",
        environment: dict[str, str] | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """启动长期运行进程（前端 dev server、后端服务等），跟踪输出日志。"""
        self._require("process", ctx)
        if not executable.strip():
            raise ValueError("executable 不能为空。")
        self._guard_command(executable + (" " + " ".join(args or []) if args else ""))
        workspace = self._workspace(ctx)
        workdir = self._resolve(cwd, ctx) if cwd.strip() else workspace
        if not workdir.is_dir():
            raise ValueError(f"工作目录不存在: {workdir}")
        record = self.registry.start(
            executable, list(args or []), workdir, env=environment, label=label
        )
        return {
            "process_id": record.process_id,
            "pid": record.pid,
            "status": record.status,
            "label": record.label,
            "started_at": record.started_at,
            "log_file": record.log_file,
        }

    def poll_process(self, process_id: str, max_chars: int = 4000) -> dict:
        """返回管理进程的增量输出与状态。"""
        self._require("process")
        result = self.registry.poll(process_id, max_chars=max(int(max_chars), 100))
        if result is None:
            raise ValueError(f"进程不存在: {process_id}")
        return result

    def stop_process(self, process_id: str, force: bool = False) -> dict:
        """停止管理进程及其子进程树。"""
        self._require("process")
        return self.registry.stop(process_id, force=bool(force))

    def list_managed_processes(self) -> dict:
        """列出所有受管进程。"""
        self._require("process")
        items = self.registry.list()
        text = "\n".join(
            f"{p['process_id']} pid={p['pid']} {p['status']} {p['label']} cwd={p['cwd']}"
            for p in items
        ) or "(无受管进程)"
        return {"text": text, "processes": items}

    def stop_all_managed_processes(self, force: bool = False) -> dict:
        """停止全部受管进程（含子进程树）。"""
        self._require("process")
        results = self.registry.stop_all(force=bool(force))
        return {"text": f"已对 {len(results)} 个受管进程发出停止指令", "stopped": results}


# ---------------------------------------------------------------------------
# module-level helpers (unit-testable)
# ---------------------------------------------------------------------------


def _norm_eq(a: Path, b: Path) -> bool:
    """Normalized path equality (for matching workspaces across projects)."""
    try:
        return str(a.expanduser().resolve()).lower() == str(b.expanduser().resolve()).lower()
    except Exception:
        return str(a).lower() == str(b).lower()


def _looks_sensitive(name: str) -> bool:
    upper = name.upper()
    return any(
        marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "AUTH")
    )


def _fmt_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def is_readonly(path: Path) -> bool:
    try:
        return not os.access(path, os.W_OK)
    except OSError:
        return True


def _is_within(target: Path, base: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _clamp_int(value: Any, default: int, maximum: int | None = None) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    if num <= 0:
        return default
    if maximum is not None:
        return min(num, maximum)
    return num


def _glob_match(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.lower(), pattern.lower())


def atomically_write(path: Path, data: bytes) -> None:
    """Write bytes atomically: temp file in same dir, then os.replace."""
    tmp = path.with_name(path.name + ".ldmb-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _clear_readonly(entry: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(entry, stat.S_IWRITE | stat.S_IREAD)


def safe_delete_dir(target: Path) -> None:
    """Recursively delete a directory, handling read-only files on Windows."""
    if target.is_symlink():
        target.unlink()
        return
    for child in sorted(target.iterdir(), key=lambda c: len(c.parts), reverse=True):
        if child.is_dir():
            safe_delete_dir(child)
        else:
            _clear_readonly(child)
            child.unlink()
    _clear_readonly(target)
    target.rmdir()


def parse_unified_diff(diff_text: str) -> list[dict[str, Any]]:
    """Parse a multi-file unified diff into patches: [{file_a, file_b, hunks}]."""
    patches: list[dict[str, Any]] = []
    current_patch: dict[str, Any] | None = None
    file_a = ""
    file_b = ""
    hunk: dict[str, Any] | None = None

    def close_hunk() -> None:
        nonlocal hunk
        if hunk is not None and current_patch is not None and (hunk["old_lines"] or hunk["new_lines"]):
            current_patch["hunks"].append(hunk)
        hunk = None

    for line in diff_text.splitlines():
        if line.startswith("--- "):
            file_a = line[4:].strip()
        elif line.startswith("+++ "):
            file_b = line[4:].strip()
            current_patch = {"file_a": file_a, "file_b": file_b, "hunks": []}
            patches.append(current_patch)
        elif line.startswith("@@"):
            close_hunk()
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            if current_patch is None:
                # @@ without a preceding +++ header (e.g. synthetic diffs)
                current_patch = {"file_a": file_a, "file_b": file_b, "hunks": []}
                patches.append(current_patch)
            hunk = {
                "old_start": int(m.group(1)),
                "new_start": int(m.group(3)),
                "old_lines": [],
                "new_lines": [],
            }
        elif hunk is not None and current_patch is not None:
            if line.startswith("-"):
                hunk["old_lines"].append(line[1:])
            elif line.startswith("+"):
                hunk["new_lines"].append(line[1:])
            elif line.startswith(" "):
                hunk["old_lines"].append(line[1:])
                hunk["new_lines"].append(line[1:])
            elif line.startswith("\\"):
                continue
    close_hunk()
    return patches


def apply_unified_patch(content: str, hunks: list[dict[str, Any]]) -> tuple[str, int]:
    """Apply hunks in order; raise ValueError if any hunk context does not match.

    Returns (new_content, applied_hunk_count). The whole operation is
    all-or-nothing per file: hunks are validated in sequence; on first
    mismatch nothing has been committed (caller only writes after success).
    """
    lines = content.splitlines()
    trailing_newline = content.endswith("\n")
    applied = 0
    for hunk in hunks:
        old_seq = hunk["old_lines"]
        position = _find_sequence(lines, old_seq, hunk["old_start"])
        if position is None:
            raise ValueError(
                f"补丁上下文不匹配（需要 {len(old_seq)} 行，位于第 {hunk['old_start']} 行附近），"
                "未应用任何修改。"
            )
        before = lines[:position]
        after = lines[position + len(old_seq):]
        lines = [*before, *hunk["new_lines"], *after]
        applied += 1
    text = "\n".join(lines)
    if trailing_newline and text and not text.endswith("\n"):
        text += "\n"
    return text, applied


def _find_sequence(lines: list[str], seq: list[str], around: int, window: int = 80) -> int | None:
    """Find the first occurrence of seq in lines near position `around` (1-based)."""
    if not seq:
        return max(0, min(around - 1, len(lines)))
    start = max(0, around - 1 - window)
    end = min(len(lines) - len(seq) + 1, around - 1 + window)
    if start < 0:
        start = 0
    for i in range(start, end):
        if lines[i : i + len(seq)] == seq:
            return i
    return None