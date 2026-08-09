"""Load / save app config, project profiles and runtime state."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from . import constants
from .constants import ensure_dirs
from .models import AppConfig, ProjectConfig, RuntimeConfig, TunnelState


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
    base_gateway: int = constants.DEFAULT_GATEWAY_PORT,
) -> list[ProjectConfig]:
    """Assign per-project internal ports (0 = unset) without collisions.

    ``index`` selects the project whose ports are being allocated (defaults to
    the first project with any unset port). Uses the lowest free integer >= the
    default base for each kind, skipping ports already claimed by other
    projects (or by a coexisting project of the other kind when the bases
    differ). Pure function; caller persists the result.
    """
    used_codex = {p.codexpro_port for p in projects if p.codexpro_port}
    used_windows = {p.windows_bridge_port for p in projects if p.windows_bridge_port}
    used_gateway = {p.gateway_port for p in projects if p.gateway_port}

    if index is None:
        for idx, project in enumerate(projects):
            if not project.codexpro_port or not project.windows_bridge_port or not project.gateway_port:
                index = idx
                break
        if index is None:
            return projects
    target = projects[index]
    all_used = used_codex | used_windows | used_gateway
    if not target.codexpro_port:
        port = find_free(base_codex, all_used)
        target.codexpro_port = port
        all_used.add(port)
    if not target.windows_bridge_port:
        port = find_free(base_windows, all_used)
        target.windows_bridge_port = port
        all_used.add(port)
    if not target.gateway_port:
        port = find_free(base_gateway, all_used)
        target.gateway_port = port
        all_used.add(port)
    return projects


def find_free(base: int, used: set[int]) -> int:
    port = base
    while port in used:
        port += 1
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