"""Multi-project lifecycle (no Qt): per-project engines + catalog state.

Architecture (多项目并行开发):

* One ``CodexProManager`` + one ``WindowsBridgeManager`` per project
  (``ProjectUnit``). Every engine process starts with ITS OWN root:
  ``CODEXPRO_ROOT`` / ``CODEXPRO_ALLOWED_ROOTS`` point at the project dir, and
  the process listens on the project's own internal port. The desktop bootstrap project used by the shared Hub therefore never re-binds
  another running engine, and two projects can run side by side without the old
  "Workspace root is outside allowed roots" failure.
* ``ProjectManager`` owns the catalog (``projects.json`` via ``config_store``),
  the running units and port allocation. ``enabled`` is retained for config/API
  compatibility; current desktop routing treats every running unit as active.
* Pure Python (no Qt) so the whole lifecycle is unit-testable.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

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
from .platform_support import IS_WINDOWS

WINDOWS_START_TIMEOUT_SECONDS = 240
PROJECT_HEALTH_INTERVAL_SECONDS = 10.0
PROJECT_HEALTH_TIMEOUT_SECONDS = 2.0
PROJECT_HEALTH_FAILURE_THRESHOLD = 2
PROJECT_RESTART_COOLDOWN_SECONDS = 30.0


@dataclass
class _RuntimeStartSpec:
    codex_token: str = field(repr=False)
    permission_mode: str = "workspace"
    execution_profile: str = "developer"
    windows_token: str | None = field(default=None, repr=False)
    timeout_seconds: float | None = None
    elevated: bool = False


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
        self.codex: Any = CodexProManager(
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
        elevated: bool = False,
    ) -> None:
        """Start this project's engines. Reuses an already-running engine.

        Per-project ports must be set on ``self.project`` BEFORE the manager
        was constructed (ports are fixed per manager); callers use
        :meth:`ProjectManager.ensure_ports` to backfill first.
        """
        windows_enabled = bool(windows_enabled and IS_WINDOWS)
        root = str(Path(self.project.root_path).expanduser().resolve())
        if IS_WINDOWS:
            from .elevation import ElevatedCodexProManager

            if elevated and not isinstance(self.codex, ElevatedCodexProManager):
                if self.codex.is_running:
                    raise SpawnError("必须先停止普通权限 CodexPro，才能切换到高权限 broker。")
                self.codex = ElevatedCodexProManager(
                    self.project.id,
                    log_dir=self.log_dir,
                    port=self.project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT,
                )
            elif not elevated and isinstance(self.codex, ElevatedCodexProManager):
                if self.codex.is_running:
                    raise SpawnError("必须先停止高权限 CodexPro，才能降级到普通工作区权限。")
                self.codex = CodexProManager(
                    log_dir=self.log_dir,
                    port=self.project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT,
                )
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
            assert windows_token is not None, "windows_enabled requires a windows token"
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
        errors: list[Exception] = []
        if self.codex.state != EngineState.IDLE or self.codex.is_running:
            try:
                self.codex.stop(timeout_seconds=timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - stop both engines before surfacing failure
                errors.append(exc)
        if self.windows.state != EngineState.IDLE or self.windows.is_running:
            try:
                self.windows.stop(timeout_seconds=timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - preserve first lifecycle failure
                errors.append(exc)
        if errors:
            raise errors[0]

    def log_tail(self, count: int = 200) -> str:
        return self.codex.log_tail(count)

    def data_plane_health(
        self,
        token: str,
        timeout_seconds: float = PROJECT_HEALTH_TIMEOUT_SECONDS,
    ) -> tuple[bool, str]:
        """Verify that the CodexPro process and its authenticated HTTP data plane are alive."""
        if self.codex.state != EngineState.READY or not self.codex.is_running:
            return False, self.codex.error or "CodexPro process is not ready"
        url = f"http://127.0.0.1:{self.project.codexpro_port or self.codex.port}/healthz"
        try:
            req = urllib_request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "MCPDevBridge-project-health",
                },
            )
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", 0) or response.getcode() or 0)
                payload = json.loads(response.read(64_000).decode("utf-8", errors="replace"))
        except (
            OSError,
            TimeoutError,
            urllib_error.URLError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if not 200 <= status < 300 or payload.get("ok") is not True:
            return False, f"healthz status={status} ok={payload.get('ok')!r}"
        expected_root = str(Path(self.project.root_path).expanduser().resolve())
        actual_root = str(Path(str(payload.get("defaultRoot") or "")).expanduser().resolve())
        if actual_root.casefold() != expected_root.casefold():
            return False, f"healthz root mismatch: {actual_root!r} != {expected_root!r}"
        return True, "ok"


class ProjectManager:
    """Catalog (projects.json) + running units for every project.

    One instance per desktop app. Pure Python; the Qt window consumes this.
    """

    def __init__(
        self,
        *,
        unit_factory: Any | None = None,
        supervisor_enabled: bool | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._units: dict[str, ProjectUnit] = {}
        self._operation_locks: dict[str, threading.RLock] = {}
        self._runtime_specs: dict[str, _RuntimeStartSpec] = {}
        self._health_failures: dict[str, int] = {}
        self._last_restart: dict[str, float] = {}
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        # Fake-unit tests are deterministic by default; production uses the supervisor.
        self._supervisor_enabled = (
            unit_factory is None if supervisor_enabled is None else supervisor_enabled
        )
        # test hook: callable(project) -> ProjectUnit (usually None)
        self._unit_factory = unit_factory

    # ------------------------------------------------------------- catalog
    def list(self) -> list[ProjectConfig]:
        """Loaded projects with ids/per-project engine ports backfilled and persisted."""
        projects = load_projects()
        changed = False
        for index, project in enumerate(projects):
            before_id = project.id
            migrate_project_id(project)
            if project.id != before_id:
                changed = True
            if not project.display_name:
                root = Path(project.root_path).expanduser()
                project.display_name = root.name or root.drive or str(root)
                changed = True
            if not project.codexpro_port or not project.windows_bridge_port:
                assign_project_ports(projects, index=index)
                changed = True
        if changed:
            save_projects(projects)
        return projects

    def get(self, project_id: str) -> ProjectConfig | None:
        return get_project_by_id(project_id)

    def by_root(self, root: str) -> ProjectConfig | None:
        from .config_store import get_project

        return get_project(root)

    def add(
        self, root: str, *, display_name: str = "", permission_mode: str = "system"
    ) -> ProjectConfig:
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
            display_name=display_name or target.name or target.drive or str(target),
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
        self.stop(project_id)
        with self._lock:
            self._units.pop(project_id, None)
            self._operation_locks.pop(project_id, None)
            self._health_failures.pop(project_id, None)
            self._last_restart.pop(project_id, None)
        project = self.get(project_id)
        if project is not None:
            delete_project(project.root_path)

    def update(self, project: ProjectConfig) -> None:
        upsert_project(project)

    def reconfigure(self, project: ProjectConfig) -> None:
        """Persist config and rebuild the cached unit when its fixed ports changed.

        ProjectUnit managers bind their ports at construction time.  An idle
        unit can therefore be dropped safely after an advanced-port edit so the
        next start uses the new persisted values.  Running units must be stopped
        first; silently rebinding a live process would make status misleading.
        """
        with self._lock:
            unit = self._units.get(project.id)
            if unit is not None and unit.is_running:
                raise SpawnError("项目服务正在运行，请先停止后再修改内部端口。")
            self._units.pop(project.id, None)
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

    # ---------------------------------------------------------- supervision
    def _operation_lock(self, project_id: str) -> threading.RLock:
        with self._lock:
            return self._operation_locks.setdefault(project_id, threading.RLock())

    def _write_supervisor_event(self, event: str, **fields: object) -> None:
        entry = {
            "event": event,
            **fields,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            constants.LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = constants.LOG_DIR / f"service-supervisor-{time.strftime('%Y-%m-%d')}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _start_supervisor(self) -> None:
        if not self._supervisor_enabled:
            return
        with self._lock:
            thread = self._supervisor_thread
            if thread is not None and thread.is_alive():
                return
            self._supervisor_stop = threading.Event()
            thread = threading.Thread(
                target=self._supervisor_loop,
                name="MCPDevBridge-project-supervisor",
                daemon=True,
            )
            self._supervisor_thread = thread
        thread.start()

    def _stop_supervisor_if_idle(self) -> None:
        with self._lock:
            idle = not self._runtime_specs
            thread = self._supervisor_thread
        if not idle or thread is None:
            return
        self._supervisor_stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if self._supervisor_thread is thread:
                self._supervisor_thread = None

    def _supervisor_loop(self) -> None:
        while not self._supervisor_stop.wait(PROJECT_HEALTH_INTERVAL_SECONDS):
            try:
                self._supervisor_tick()
            except Exception as exc:  # noqa: BLE001 - supervisor must stay alive
                self._write_supervisor_event("supervisor_tick_failed", error=type(exc).__name__)

    def _supervisor_tick(self) -> None:
        with self._lock:
            runtime_specs = list(self._runtime_specs.items())
        for project_id, spec in runtime_specs:
            unit = self.unit(project_id)
            if unit is None:
                ok, detail = False, "project unit missing"
            elif unit.state != EngineState.READY:
                ok, detail = False, unit.message or f"state={unit.state.value}"
            else:
                ok, detail = unit.data_plane_health(spec.codex_token)
            if ok:
                with self._lock:
                    self._health_failures[project_id] = 0
                continue
            with self._lock:
                failures = self._health_failures.get(project_id, 0) + 1
                self._health_failures[project_id] = failures
                last_restart = self._last_restart.get(project_id, 0.0)
            self._write_supervisor_event(
                "project_probe_failed",
                project_id=project_id,
                failures=failures,
                detail=detail[:500],
            )
            if failures < PROJECT_HEALTH_FAILURE_THRESHOLD:
                continue
            now = time.monotonic()
            if now - last_restart < PROJECT_RESTART_COOLDOWN_SECONDS:
                continue
            self._recover_project(project_id, detail)

    def _start_unit_from_spec(self, project: ProjectConfig, spec: _RuntimeStartSpec) -> ProjectView:
        root = Path(project.root_path).expanduser().resolve()
        if not root.is_dir():
            raise SpawnError(f"项目目录不存在：{root}")
        if not spec.codex_token or len(spec.codex_token) < 24:
            raise SpawnError("访问令牌未生成或长度不足（至少 24 字节）。")
        unit = self._ensure_unit(project)
        unit.start(
            spec.codex_token,
            permission_mode=spec.permission_mode or project.permission_mode,
            execution_profile=spec.execution_profile or "developer",
            windows_token=spec.windows_token,
            windows_enabled=project.windows_enabled and bool(spec.windows_token),
            elevated=spec.elevated,
        )
        if not unit.wait_ready(timeout_seconds=spec.timeout_seconds):
            unit.stop()
            raise SpawnError(unit.message or "项目引擎启动失败。")
        healthy, detail = unit.data_plane_health(spec.codex_token)
        if not healthy:
            unit.stop()
            raise SpawnError(f"项目数据面启动自检失败：{detail}")
        return self.view(project.id)

    def _recover_project(self, project_id: str, reason: str) -> None:
        lock = self._operation_lock(project_id)
        with lock:
            with self._lock:
                spec = self._runtime_specs.get(project_id)
                unit = self._units.get(project_id)
                if spec is None:
                    return
                self._last_restart[project_id] = time.monotonic()
            old_pid = unit.engine_pid if unit is not None else None
            self._write_supervisor_event(
                "project_restart",
                project_id=project_id,
                old_pid=old_pid,
                reason=reason[:500],
            )
            try:
                if unit is not None:
                    unit.stop()
                project = self.get(project_id)
                if project is None:
                    raise SpawnError(f"项目不存在：{project_id}")
                self.ensure_ports(project)
                view = self._start_unit_from_spec(project, spec)
                with self._lock:
                    self._health_failures[project_id] = 0
                self._write_supervisor_event(
                    "project_restart_ok",
                    project_id=project_id,
                    old_pid=old_pid,
                    new_pid=view.engine_pid,
                )
            except Exception as exc:  # noqa: BLE001 - durable recovery path
                self._write_supervisor_event(
                    "project_restart_failed",
                    project_id=project_id,
                    old_pid=old_pid,
                    error=type(exc).__name__,
                    detail=str(exc)[:500],
                )

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
        elevated: bool = False,
    ) -> ProjectView:
        """Start one project's engines and arm in-memory crash recovery."""
        lock = self._operation_lock(project_id)
        with lock:
            project = self.get(project_id)
            if project is None:
                raise SpawnError(f"项目不存在：{project_id}")
            self.ensure_ports(project)
            spec = _RuntimeStartSpec(
                codex_token=codex_token,
                permission_mode=permission_mode or project.permission_mode,
                execution_profile=execution_profile or "developer",
                windows_token=windows_token,
                timeout_seconds=timeout_seconds,
                elevated=elevated,
            )
            view = self._start_unit_from_spec(project, spec)
            with self._lock:
                self._runtime_specs[project_id] = spec
                self._health_failures[project_id] = 0
            self._start_supervisor()
            return view

    def stop(self, project_id: str) -> None:
        lock = self._operation_lock(project_id)
        with lock:
            with self._lock:
                self._runtime_specs.pop(project_id, None)
                self._health_failures.pop(project_id, None)
                unit = self._units.get(project_id)
            if unit is not None:
                unit.stop()
        self._stop_supervisor_if_idle()

    def stop_all(self) -> None:
        with self._lock:
            project_ids = list(self._units)
        errors: list[Exception] = []
        for project_id in project_ids:
            try:
                self.stop(project_id)
            except Exception as exc:  # noqa: BLE001 - stop every project before surfacing failure
                errors.append(exc)
        if errors:
            raise errors[0]

    def start_enabled(
        self, *, codex_token: str, windows_token: str | None = None
    ) -> list[ProjectView]:
        """Auto-restore: start engines of every enabled project in parallel."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        enabled_projects = [p for p in self.list() if p.enabled]
        if not enabled_projects:
            return []

        started: list[ProjectView] = []
        max_workers = min(len(enabled_projects), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.start,
                    p.id,
                    codex_token=codex_token,
                    permission_mode=p.permission_mode,
                    execution_profile="full_system"
                    if p.permission_mode == "system"
                    else "developer",
                    windows_token=windows_token,
                    elevated=bool(IS_WINDOWS and p.permission_mode == "system"),
                ): p
                for p in enabled_projects
            }
            for future in as_completed(futures):
                try:
                    started.append(future.result())
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
