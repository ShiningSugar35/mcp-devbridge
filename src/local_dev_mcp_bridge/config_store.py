"""Load / save app config, project profiles and runtime state."""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any

from . import constants
from .constants import ensure_dirs
from .models import AppConfig, DeviceConfig, ProjectConfig, RuntimeConfig, TunnelState


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: Any) -> None:
    ensure_dirs()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_app_config() -> AppConfig:
    data = _read_json(constants.config_file())
    if not isinstance(data, dict):
        return AppConfig()
    try:
        return AppConfig.model_validate(data)
    except Exception:
        return AppConfig()


def save_app_config(cfg: AppConfig) -> None:
    _write_json(constants.config_file(), cfg.model_dump())


def load_projects() -> list[ProjectConfig]:
    data = _read_json(constants.projects_file())
    if not isinstance(data, dict):
        return []
    raw_list = data.get("projects", [])
    projects: list[ProjectConfig] = []
    for item in raw_list:
        try:
            projects.append(ProjectConfig.model_validate(item))
        except Exception:
            continue
    return projects


def save_projects(projects: list[ProjectConfig]) -> None:
    _write_json(
        constants.projects_file(),
        {"projects": [p.model_dump() for p in projects]},
    )


def load_devices() -> list[DeviceConfig]:
    data = _read_json(constants.devices_file())
    if not isinstance(data, dict):
        return []
    devices: list[DeviceConfig] = []
    for item in data.get("devices", []):
        try:
            devices.append(DeviceConfig.model_validate(item))
        except Exception:
            continue
    return devices


def save_devices(devices: list[DeviceConfig]) -> None:
    _write_json(
        constants.devices_file(),
        {"devices": [d.model_dump() for d in devices]},
    )


def upsert_device(device: DeviceConfig) -> list[DeviceConfig]:
    devices = load_devices()
    for index, existing in enumerate(devices):
        if existing.id == device.id:
            devices[index] = device
            break
    else:
        devices.append(device)
    save_devices(devices)
    return devices


def delete_device(device_id: str) -> list[DeviceConfig]:
    devices = [d for d in load_devices() if d.id != device_id]
    save_devices(devices)
    return devices


def upsert_project(project: ProjectConfig) -> list[ProjectConfig]:
    projects = load_projects()
    existed = False
    for i, existing in enumerate(projects):
        if _norm_path(existing.root_path) == _norm_path(project.root_path):
            projects[i] = project
            existed = True
            break
    if not existed:
        projects.insert(0, project)
    projects.sort(key=lambda p: p.last_used_at, reverse=True)
    save_projects(projects)
    return projects


def get_project(root: str) -> ProjectConfig | None:
    target = _norm_path(root)
    for project in load_projects():
        if _norm_path(project.root_path) == target:
            return project
    return None


def get_project_by_id(project_id: str) -> ProjectConfig | None:
    for project in load_projects():
        if project.id and project.id == project_id:
            return project
    return None


def delete_project(root: str) -> list[ProjectConfig]:
    """Remove a project by normalized root path; returns the surviving list."""
    target = _norm_path(root)
    projects = [p for p in load_projects() if _norm_path(p.root_path) != target]
    save_projects(projects)
    return projects


def migrate_project_id(project: ProjectConfig) -> ProjectConfig:
    """Backfill a stable short id for projects saved before multi-project support."""
    if project.id:
        return project
    project.id = uuid.uuid4().hex[:8]
    return project


def assign_project_ports(
    projects: list[ProjectConfig],
    *,
    index: int | None = None,
    base_codex: int = constants.DEFAULT_CODEXPRO_PORT,
    base_windows: int = constants.DEFAULT_WINDOWS_MCP_PORT,
) -> list[ProjectConfig]:
    """Assign only per-project engine ports without collisions.

    Gateway is a shared Hub service and therefore uses AppConfig.gateway_port;
    it is deliberately not allocated per project.
    """
    used_codex = {p.codexpro_port for p in projects if p.codexpro_port}
    used_windows = {p.windows_bridge_port for p in projects if p.windows_bridge_port}

    if index is None:
        for idx, project in enumerate(projects):
            if not project.codexpro_port or not project.windows_bridge_port:
                index = idx
                break
        if index is None:
            return projects
    target = projects[index]
    all_used = used_codex | used_windows
    if not target.codexpro_port:
        port = find_free(base_codex, all_used)
        target.codexpro_port = port
        all_used.add(port)
    if not target.windows_bridge_port:
        port = find_free(base_windows, all_used)
        target.windows_bridge_port = port
        all_used.add(port)
    return projects

def _loopback_port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def find_free(base: int, used: set[int]) -> int:
    port = base
    while port <= 65535 and (port in used or _loopback_port_in_use(port)):
        port += 1
    if port > 65535:
        raise ValueError("没有可用的本机项目端口。")
    return port


def _norm_path(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve()).lower()
    except Exception:
        return value.lower()


def detect_project_features(root: Path) -> dict[str, bool]:
    """Best-effort feature detection used for suggestions only."""
    markers: dict[str, bool] = {}
    required = {
        "git": ".git",
        "pyproject": "pyproject.toml",
        "uv_lock": "uv.lock",
        "requirements": "requirements.txt",
        "package_json": "package.json",
        "pnpm_lock": "pnpm-lock.yaml",
        "yarn_lock": "yarn.lock",
        "sln": "*.sln",
        "csproj": "*.csproj",
    }
    for key, value in required.items():
        if key in ("sln", "csproj"):
            markers[key] = any(root.glob(value))
        else:
            markers[key] = (root / value).exists()
    return markers


def suggest_commands(root: Path) -> dict[str, str]:
    """Suggested commands for a project (never modify the project itself)."""
    features = detect_project_features(root)
    suggestions: dict[str, str] = {}
    if features["uv_lock"]:
        suggestions["test_command"] = "uv run pytest"
        suggestions["lint_command"] = "uv run ruff check ."
        suggestions["typecheck_command"] = "uv run pyright"
        suggestions["build_command"] = "uv build"
    elif features["pyproject"]:
        suggestions["test_command"] = "python -m pytest"
        suggestions["lint_command"] = "python -m ruff check ."
        suggestions["typecheck_command"] = "python -m pyright"
        suggestions["build_command"] = "python -m build"
    elif features["package_json"]:
        suggestions["test_command"] = "npm test"
        suggestions["lint_command"] = "npm run lint"
        suggestions["typecheck_command"] = "npm run typecheck"
        suggestions["build_command"] = "npm run build"
    elif features["sln"] or features["csproj"]:
        suggestions["test_command"] = "dotnet test"
        suggestions["lint_command"] = ""
        suggestions["typecheck_command"] = ""
        suggestions["build_command"] = "dotnet build"
    else:
        suggestions["test_command"] = ""
        suggestions["lint_command"] = ""
        suggestions["typecheck_command"] = ""
        suggestions["build_command"] = ""
    return suggestions


def load_runtime_config(path: Path | str | None = None) -> RuntimeConfig | None:
    target = Path(path or constants.rc_file())
    data = _read_json(target)
    if not isinstance(data, dict):
        return None
    try:
        return RuntimeConfig.model_validate(data)
    except Exception:
        return None


def save_runtime_config(rc: RuntimeConfig, path: Path | None = None) -> None:
    _write_json(path or constants.rc_file(), rc.model_dump())


def load_tunnel_state() -> TunnelState:
    data = _read_json(constants.state_file())
    if not isinstance(data, dict):
        return TunnelState()
    try:
        return TunnelState.model_validate(data)
    except Exception:
        return TunnelState()


def save_tunnel_state(state: TunnelState) -> None:
    _write_json(constants.state_file(), state.model_dump())