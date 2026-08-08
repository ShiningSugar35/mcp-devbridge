"""Permission policy for the MCP backend."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

PermissionMode = Literal["read_only", "workspace", "system"]

# Tool categories
CATEGORY_FILE_READ = "file_read"
CATEGORY_FILE_WRITE = "file_write"
CATEGORY_GIT_READ = "git_read"
CATEGORY_GIT_WRITE = "git_write"
CATEGORY_COMMAND = "command"
CATEGORY_PROCESS = "process"
CATEGORY_INFO = "info"

WRITE_CATEGORIES = {
    CATEGORY_FILE_WRITE,
    CATEGORY_GIT_WRITE,
    CATEGORY_COMMAND,
    CATEGORY_PROCESS,
    "file_delete",
}


class PermissionError(Exception):  # noqa: A001 - user-facing error type by design
    """Raised when a tool is not allowed in the current permission mode."""


class PermissionPolicy:
    def __init__(self, mode: PermissionMode = "workspace", workspace: Path | None = None) -> None:
        self.mode = mode
        self.workspace = (workspace or Path.cwd()).resolve()

    def require(self, category: str) -> None:
        if self.mode == "read_only":
            if category in WRITE_CATEGORIES:
                raise PermissionError(
                    f"当前为只读模式，不允许执行 {category} 类型操作。请切换到“项目全权限”或“系统全权限”。"
                )
            return
        if self.mode == "workspace":
            if category == CATEGORY_COMMAND:
                # 项目全权限下允许项目内命令（命令本身可能访问项目外部，界面已明确提示风险）
                return
            return
        if self.mode == "system":
            return
        raise PermissionError(f"未知权限模式: {self.mode}")

    @property
    def allows_project_external(self) -> bool:
        return self.mode == "system"

    @property
    def allows_write(self) -> bool:
        return self.mode != "read_only"

    @property
    def allows_commands(self) -> bool:
        return self.mode != "read_only"

    def describe(self) -> str:
        return {
            "read_only": "只读",
            "workspace": "项目全权限",
            "system": "系统全权限",
        }.get(self.mode, self.mode)


__all__ = ["PermissionPolicy", "PermissionError", "PermissionMode"]