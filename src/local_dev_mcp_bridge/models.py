"""Pydantic models for app + project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

PermissionMode = Literal["read_only", "workspace", "system"]
TunnelMode = Literal["named", "quick", "none"]
AuthMode = Literal["bearer", "anonymous"]

# Phase 5: Git 桌面参数（ProjectConfig 扩展，均可空；工具层暂只作配置存储）
GIT_FIELD_LABELS: dict[str, str] = {
    "git_user_name": "Git 用户名",
    "git_user_email": "Git 邮箱",
    "default_push_remote": "默认推送远程",
    "default_push_branch": "默认推送分支",
}
_GIT_FORBIDDEN_CHARS = set("\"'\\`$%&;|<>")


def git_field_error(kind: str, value: str) -> str | None:
    """Return a Chinese error message for an invalid git config value, else None.

    Rules: empty is allowed; no whitespace / control chars / quotes / shell
    metacharacters (avoids git config 注入与命令行拼接问题).
    """
    label = GIT_FIELD_LABELS.get(kind, kind)
    if not value:
        return None
    if any(ord(ch) < 32 for ch in value):
        return f"{label}不能包含控制字符。"
    if value != value.strip() or " " in value:
        return f"{label}不能包含空格。"
    if any(ch in _GIT_FORBIDDEN_CHARS for ch in value):
        return f"{label}不能包含引号或特殊符号（' \" \\ ` $ ; & | < >）。"
    if kind == "git_user_email" and ("@" not in value or "." not in value):
        return f"{label}格式不正确（示例: name@example.com）。"
    return None

PermissionMode = Literal["read_only", "workspace", "system"]
TunnelMode = Literal["named", "quick", "none"]
AuthMode = Literal["bearer", "anonymous"]


class ProjectConfig(BaseModel):
    """Per-project persisted settings."""

    display_name: str = ""
    root_path: str
    permission_mode: PermissionMode = "workspace"
    test_command: str = "uv run pytest"
    lint_command: str = "uv run ruff check ."
    typecheck_command: str = "uv run pyright"
    build_command: str = ""
    shell: str = "auto"
    tunnel_mode: TunnelMode = "named"
    connection: str = "local"  # ConnectionMethod.value, 桌面连接方式（持久化）
    public_hostname: str = ""  # Cloudflare/ngrok 固定域名
    local_port: int = 8765
    ignore_patterns: list[str] = Field(default_factory=list)
    git_user_name: str = ""  # Phase 5 Git 桌面参数（可空）
    git_user_email: str = ""
    default_push_remote: str = ""
    default_push_branch: str = ""
    last_used_at: str = ""


class AppConfig(BaseModel):
    """Global app configuration."""

    version: int = 1
    active_workspace: str | None = None
    auth_mode: AuthMode = "bearer"
    require_public_bearer: bool = True
    allow_local_anonymous: bool = True
    log_retention_days: int = 14
    tunnel_auto_reconnect: bool = True
    exit_stop_managed: bool = False
    first_system_risk_accepted: bool = False
    first_run_version: int = 0


class RuntimeConfig(BaseModel):
    """Sent to the backend subprocess describing the active session."""

    workspace: str
    permission_mode: PermissionMode = "workspace"
    local_port: int = 2865
    auth_mode: AuthMode = "bearer"
    allow_local_anonymous: bool = True
    require_public_bearer: bool = True
    log_dir: str | None = None
    process_log_dir: str | None = None
    max_file_bytes: int | None = None
    test_command: str = ""
    lint_command: str = ""
    typecheck_command: str = ""
    build_command: str = ""
    shell: str = "auto"
    ignore_patterns: list[str] = Field(default_factory=list)
    public_hostname: str = ""  # Named Tunnel 公网域名（Phase 4 transport_security 白名单）

    @field_validator("workspace")
    @classmethod
    def workspace_abs(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class TunnelState(BaseModel):
    tunnel_id: str = ""
    tunnel_name: str = ""
    hostname: str = ""
    origin_domain: str = ""
    local_url: str = ""
    credentials_source: Literal["token", "cert"] = "cert"
    created_at: str = ""
    last_status: str = ""