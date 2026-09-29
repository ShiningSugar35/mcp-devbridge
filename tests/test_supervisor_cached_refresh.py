"""The non-UI supervisor, not property reads, owns status refreshes."""
from types import SimpleNamespace

from local_dev_mcp_bridge.engines import EngineState
from local_dev_mcp_bridge.project_manager import ProjectManager


def test_supervisor_drives_explicit_cached_status_refresh() -> None:
    calls: list[str] = []
    core = SimpleNamespace(refresh_status=lambda: calls.append("refresh"))

    def health(_value: str) -> tuple[bool, str]:
        calls.append("health")
        return True, "ok"

    unit = SimpleNamespace(codex=core, state=EngineState.READY, data_plane_health=health)
    manager = ProjectManager(supervisor_enabled=False)
    manager._units["fixture"] = unit  # type: ignore[assignment]
    manager._runtime_specs["fixture"] = SimpleNamespace(codex_token="fixture")  # type: ignore[assignment]
    manager._supervisor_tick()
    assert calls == ["refresh", "health"]
