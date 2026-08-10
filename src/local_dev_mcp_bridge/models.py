"""Pydantic models for app + project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from . import constants

type PermissionMode = Literal["read_only", "workspace", "system"]
type ClientTarget = Literal["chatgpt", "gemini"]
type TunnelMode = Literal["named", "quick", "none"]
type AuthMode = Literal["bearer", "anonymous"]

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


def validate_port(value: int) -> int:
    """Reject out-of-range ports (1-65535); used by all port fields."""
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError(f"端口必须是 1-65535 之间的整数（当前值：{value!r}）。")
    return value


def gateway_service_url(port: int) -> str:
    """The Service URL Cloudflare must point at for the given gateway port."""
    validate_port(port)
    return f"http://localhost:{port}"


class ProjectConfig(BaseModel):
    """Per-project persisted settings."""

    id: str = ""  # 多项目唯一标识（add 时自动分配）；空串视为未初始化
    display_name: str = ""
    root_path: str
    permission_mode: PermissionMode = "system"
    client_target: ClientTarget = "chatgpt"
    gemini_redirect_uri: str = ""
    test_command: str = "uv run pytest"
    lint_command: str = "uv run ruff check ."
    typecheck_command: str = "uv run pyright"
    build_command: str = ""
    shell: str = "auto"
    tunnel_mode: TunnelMode = "named"
    connection: str = "local"  # ConnectionMethod.value, 桌面连接方式（持久化）
    public_hostname: str = ""  # Cloudflare/ngrok 固定域名
    ignore_patterns: list[str] = Field(default_factory=list)
    git_user_name: str = ""  # Phase 5 Git 桌面参数（可空）
    git_user_email: str = ""
    default_push_remote: str = ""
    default_push_branch: str = ""
    last_used_at: str = ""
    # 多项目并行（Phase 9）：每个项目独立 CodexPro / Windows 桥端口；
    # 0 = 未分配，启动时回退到全局默认端口（constants.DEFAULT_*_PORT）。
    codexpro_port: int = 0
    windows_bridge_port: int = 0
    gateway_port: int = 0
    windows_enabled: bool = False
    # v0.4 及更早用于“桌面启动自动恢复”。v0.5 桌面不再自动恢复；保留字段仅兼容旧 projects.json。
    enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def _migrate_port(cls, data: Any) -> Any:
        # v0.1: ProjectConfig.local_port 语义含糊且从未生效，v0.2 移除；
        # 端口改为全局（AppConfig），旧值不再使用，直接丢弃以保持兼容。
        if isinstance(data, dict):
            data.pop("local_port", None)
        return data


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
    execution_profile: str = "developer"
    full_system_risk_accepted: bool = False
    first_run_version: int = 0
    # 端口配置（集中维护默认值，见 constants.DEFAULT_*_PORT）：
    gateway_port: int = constants.DEFAULT_GATEWAY_PORT
    codexpro_port: int = constants.DEFAULT_CODEXPRO_PORT
    windows_mcp_port: int = constants.DEFAULT_WINDOWS_MCP_PORT
    legacy_backend_port: int = constants.DEFAULT_LEGACY_BACKEND_PORT

    @field_validator("gateway_port", "codexpro_port", "windows_mcp_port", "legacy_backend_port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        return validate_port(v)


class RuntimeConfig(BaseModel):
    """Sent to the backend subprocess describing the active session."""

    workspace: str
    permission_mode: PermissionMode = "workspace"
    legacy_backend_port: int = constants.DEFAULT_LEGACY_BACKEND_PORT
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
    execution_profile: str = "developer"
    full_system_confirmed: bool = False
    ignore_patterns: list[str] = Field(default_factory=list)
    public_hostname: str = ""  # Named Tunnel 公网域名（Phase 4 transport_security 白名单）
    project_catalog_enabled: bool = True  # 后端是否加载 projects.json（多项目目录）

    @field_validator("legacy_backend_port")
    @classmethod
    def _check_backend_port(cls, v: int) -> int:
        return validate_port(v)

    @model_validator(mode="before")
    @classmethod
    def _migrate_local_port(cls, data: Any) -> Any:
        # v0.1 曾用 local_port（默认 2865）表达后端端口；v0.2 统一为
        # legacy_backend_port（默认 8765）。旧配置迁移，用户自定义值保留。
        if isinstance(data, dict) and "local_port" in data and "legacy_backend_port" not in data:
            data["legacy_backend_port"] = data.pop("local_port")
        return data

    @property
    def local_port(self) -> int:
        """Deprecated alias kept for v0.1 callers; prefer legacy_backend_port."""
        return self.legacy_backend_port

    @local_port.setter
    def local_port(self, value: int) -> None:
        self.legacy_backend_port = value

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