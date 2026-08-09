"""Multi-project lifecycle (no Qt): per-project engines + catalog state.

Architecture (多项目并行开发):

* One ``CodexProManager`` + one ``WindowsBridgeManager`` per project
  (``ProjectUnit``). Every engine process starts with ITS OWN root:
  ``CODEXPRO_ROOT`` / ``CODEXPRO_ALLOWED_ROOTS`` point at the project dir, and
  the process listens on the project's own internal port. Switching the
  desktop "entry project" therefore never re-binds a running engine, and two
  projects can run side by side without the old "Workspace root is outside
  allowed roots" failure.
* ``ProjectManager`` owns the catalog (``projects.json`` via ``config_store``),
  the running units, port allocation and startup auto-restore. ``enabled``
  projects are automatically restored after the desktop launches.
* Pure Python (no Qt) so the whole lifecycle is unit-testable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import constants
from .config_store import (
    assign_project_ports,
    delete_project,
    get_project_by_id,
    load_projects,
    migrate_project_id,
    save_projects,
    upsert_project,
)
from .engines import (
    CodexProManager,
    EngineState,
    SpawnError,
    WindowsBridgeManager,
)
from .models import ProjectConfig

WINDOWS_START_TIMEOUT_SECONDS = 240


@dataclass
class ProjectView:
    """Read-only snapshot of one project for the desktop table / list_projects."""

    id: str
    name: str
    root_path: str
    codexpro_port: int
    windows_bridge_port: int
    enabled: bool
    windows_enabled: bool
    state: str
    message: str
    engine_pid: int | None


class ProjectUnit:
    """Per-project engine pair (CodexPro + optional Windows bridge)."""

    def __init__(self, project: ProjectConfig, *, log_dir: Path | None = None) -> None:
        self.project = project
        base = Path(log_dir or constants.process_log_dir())
        self.log_dir = base / (project.id or "project")
        self.codex = CodexProManager(
            log_dir=self.log_dir,
            port=project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT,
        )
        self.windows = WindowsBridgeManager(
            log_dir=self.log_dir,
            port=project.windows_bridge_port or constants.DEFAULT_WINDOWS_MCP_PORT,
        )

    # ------------------------------------------------------------ state
    @property
    def state(self) -> EngineState:
        codex_state = self.codex.state
        windows_state = self.windows.state
        if codex_state in (EngineState.STARTING, EngineState.STOPPING, EngineState.ERROR):
            return codex_state
        if windows_state == EngineState.ERROR:
            return EngineState.ERROR
        if windows_state in (EngineState.STARTING, EngineState.STOPPING):
            return windows_state
        if codex_state == EngineState.READY:
            return EngineState.READY
        return EngineState.IDLE

    @property
    def message(self) -> str | None:
        return self.codex.error or self.windows.error

    @property
    def is_running(self) -> bool:
        return self.state in (EngineState.STARTING, EngineState.READY, EngineState.STOPPING)

    @property
    def engine_pid(self) -> int | None:
        return self.codex.pid

    # ---------------------------------------------------------- lifecycle
    def start(
        self,
        codex_token: str,
        *,
        permission_mode: str = "workspace",
        execution_profile: str = "developer",
        windows_token: str | None = None,
        windows_enabled: bool = False,
    ) -> None:
        """Start this project's engines. Reuses an already-running engine.

        Per-project ports must be set on ``self.project`` BEFORE the manager
        was constructed (ports are fixed per manager); callers use
        :meth:`ProjectManager.ensure_ports` to backfill first.
        """
        root = str(Path(self.project.root_path).expanduser().resolve())
        extra_env = None
        if windows_enabled and windows_token:
            extra_env = {
                "CODEXPRO_WINDOWS_BRIDGE_URL": (
                    f"http://127.0.0.1:{self.project.windows_bridge_port or self.windows.port}/mcp"
                )
            }
        self.codex.start(
            root,
            codex_token,
            permission_mode=permission_mode,
            windows_token=windows_token if windows_enabled else None,
            execution_profile=execution_profile,
            extra_env=extra_env,
        )
        if windows_enabled:
            self.windows.start(windows_token)
        else:
            if self.windows.is_running:
                self.windows.stop()

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        if not self.codex.wait_ready(timeout_seconds=timeout_seconds):
            return False
        return not (
            self.windows.is_running
            and not self.windows.wait_ready(
                timeout_seconds=timeout_seconds or WINDOWS_START_TIMEOUT_SECONDS
            )
        )

    def stop(self, timeout_seconds: float = 8.0) -> None:
        if self.codex.is_running:
            self.codex.stop(timeout_seconds=timeout_seconds)
        if self.windows.is_running:
            self.windows.stop(timeout_seconds=timeout_seconds)

    def log_tail(self, count: int = 200) -> str:
        return self.codex.log_tail(count)


class ProjectManager:
    """Catalog (projects.json) + running units for every project.

    One instance per desktop app. Pure Python; the Qt window consumes this.
    """

    def __init__(self, *, unit_factory: Any | None = None) -> None:
        self._lock = threading.Lock()
        self._units: dict[str, ProjectUnit] = {}
        # test hook: callable(project) -> ProjectUnit (usually None)
        self._unit_factory = unit_factory

    # ------------------------------------------------------------- catalog
    def list(self) -> list[ProjectConfig]:
        """Loaded projects with ids/ports backfilled and persisted."""
        projects = load_projects()
        for project in projects:
            migrate_project_id(project)
        if any(not p.codexpro_port or not p.windows_bridge_port for p in projects):
            assign_project_ports(projects, index=0)
            save_projects(projects)
        return projects

    def get(self, project_id: str) -> ProjectConfig | None:
        return get_project_by_id(project_id)

    def by_root(self, root: str) -> ProjectConfig | None:
        from .config_store import get_project

        return get_project(root)

    def add(self, root: str, *, display_name: str = "", permission_mode: str = "workspace") -> ProjectConfig:
        """Register a new project (assign id + ports, persist)."""
        from .config_store import suggest_commands

        target = Path(root).expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"目录不存在：{target}")
        existing = self.by_root(str(target))
        if existing is not None:
            return existing
        suggestions = suggest_commands(target)
        project = ProjectConfig(
            display_name=display_name or target.name,
            root_path=str(target),
            permission_mode=permission_mode,  # type: ignore[arg-type]
            test_command=suggestions.get("test_command", ""),
            lint_command=suggestions.get("lint_command", ""),
            typecheck_command=suggestions.get("typecheck_command", ""),
            build_command=suggestions.get("build_command", ""),
        )
        migrate_project_id(project)
        projects = self.list()
        projects.append(project)
        save_projects(projects)
        assign_project_ports(projects, index=len(projects) - 1)
        save_projects(projects)
        return project

    def remove(self, project_id: str) -> None:
        """Stop the project's engines (if any) and drop it from the catalog."""
        with self._lock:
            unit = self._units.pop(project_id, None)
        if unit is not None:
            unit.stop()
        project = self.get(project_id)
        if project is not None:
            delete_project(project.root_path)

    def update(self, project: ProjectConfig) -> None:
        upsert_project(project)

    def ensure_ports(self, project: ProjectConfig) -> None:
        """Backfill unassigned per-project ports on an existing config."""
        projects = load_projects()
        idx = next(
            (i for i, p in enumerate(projects) if p.id == project.id),
            None,
        )
        if idx is None:
            return
        if not project.codexpro_port or not project.windows_bridge_port:
            assign_project_ports(projects, index=idx)
            save_projects(projects)
            migrated = projects[idx]
            project.codexpro_port = migrated.codexpro_port
            project.windows_bridge_port = migrated.windows_bridge_port

    # --------------------------------------------------------------- units
    def unit(self, project_id: str) -> ProjectUnit | None:
        with self._lock:
            return self._units.get(project_id)

    def unit_for(self, project_id: str) -> ProjectUnit | None:
        """Unit of the project, created on first use (persisted ports backfilled)."""
        project = self.get(project_id)
        if project is None:
            return None
        self.ensure_ports(project)
        return self._ensure_unit(project)

    def _ensure_unit(self, project: ProjectConfig) -> ProjectUnit:
        with self._lock:
            unit = self._units.get(project.id)
            if unit is None:
                unit = (
                    self._unit_factory(project)
                    if self._unit_factory is not None
                    else ProjectUnit(project)
                )
                self._units[project.id] = unit
            return unit

    def start(
        self,
        project_id: str,
        *,
        codex_token: str,
        permission_mode: str | None = None,
        execution_profile: str = "developer",
        windows_token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ProjectView:
        """Start one project's engines (loopback only; tunnel/gateway not touched)."""
        project = self.get(project_id)
        if project is None:
            raise SpawnError(f"项目不存在：{project_id}")
        self.ensure_ports(project)
        root = Path(project.root_path).expanduser().resolve()
        if not root.is_dir():
            raise SpawnError(f"项目目录不存在：{root}")
        if not codex_token or len(codex_token) < 24:
            raise SpawnError("访问令牌未生成或长度不足（至少 24 字节）。")
        unit = self._ensure_unit(project)
        unit.start(
            codex_token,
            permission_mode=permission_mode or project.permission_mode,
            execution_profile=execution_profile or "developer",
            windows_token=windows_token,
            windows_enabled=project.windows_enabled and bool(windows_token),
        )
        if not unit.wait_ready(timeout_seconds=timeout_seconds):
            unit.stop()
            raise SpawnError(unit.message or "项目引擎启动失败。")
        return self.view(project_id)

    def stop(self, project_id: str) -> None:
        with self._lock:
            unit = self._units.get(project_id)
        if unit is not None:
            unit.stop()

    def stop_all(self) -> None:
        for project_id in list(self._units):
            self.stop(project_id)

    def start_enabled(self, *, codex_token: str, windows_token: str | None = None) -> list[ProjectView]:
        """Auto-restore: start engines of every enabled project (best effort)."""
        started: list[ProjectView] = []
        for project in self.list():
            if not project.enabled:
                continue
            try:
                started.append(
                    self.start(
                        project.id,
                        codex_token=codex_token,
                        windows_token=windows_token,
                    )
                )
            except (SpawnError, ValueError):
                continue
        return started

    # --------------------------------------------------------------- views
    def view(self, project_id: str) -> ProjectView:
        project = self.get(project_id)
        if project is None:
            raise SpawnError(f"项目不存在：{project_id}")
        return self._view_of(project)

    def views(self) -> list[ProjectView]:
        return [self._view_of(p) for p in self.list()]

    def _view_of(self, project: ProjectConfig) -> ProjectView:
        with self._lock:
            unit = self._units.get(project.id)
        state = EngineState.IDLE
        pid: int | None = None
        if unit is not None:
            state = unit.state
            pid = unit.engine_pid
        return ProjectView(
            id=project.id,
            name=project.display_name or Path(project.root_path).name,
            root_path=project.root_path,
            codexpro_port=project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT,
            windows_bridge_port=project.windows_bridge_port or constants.DEFAULT_WINDOWS_MCP_PORT,
            enabled=project.enabled,
            windows_enabled=project.windows_enabled,
            state=state.value,
            message="",
            engine_pid=pid,
        )


__all__ = ["ProjectManager", "ProjectUnit", "ProjectView", "WINDOWS_START_TIMEOUT_SECONDS"]