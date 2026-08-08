"""Load / save app config, project profiles and runtime state."""

from __future__ import annotations

import json
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