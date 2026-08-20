"""Gateway-side argument normalization for Agent Pool / Orchestrator tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent_orchestrator import AgentOrchestrator
from .agent_pool import AgentPool

AGENT_POOL_TOOL_NAMES = frozenset(
    {
        "agent_pool_capabilities",
        "agent_pool_spawn",
        "agent_pool_spawn_batch",
        "agent_pool_list",
        "agent_pool_get",
        "agent_pool_wait",
        "agent_pool_cancel",
        "agent_pool_collect",
        "agent_pool_cleanup",
    }
)
AGENT_ORCHESTRATOR_TOOL_NAMES = frozenset(
    {
        "spawn_agent",
        "spawn_agent_team",
        "list_agents",
        "get_agent",
        "get_agent_team",
        "message_agent",
        "cancel_agent",
        "wait_agents",
        "cleanup_agent",
        "cleanup_agent_team",
    }
)
AGENT_TOOL_NAMES = AGENT_POOL_TOOL_NAMES | AGENT_ORCHESTRATOR_TOOL_NAMES


def resolve_agent_target(workspace: Path, raw_target: object) -> Path:
    """Resolve an optional absolute/relative target without escaping the routed root."""
    text = str(raw_target or "").strip()
    if not text:
        return workspace.resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    root = workspace.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"target_path 超出当前工作区范围：{candidate}") from exc
    if not candidate.is_dir():
        raise ValueError(f"target_path 必须是已存在目录：{candidate}")
    return candidate


def _require_list(arguments: dict[str, Any], name: str) -> list[Any]:
    value = arguments.get(name) or []
    if not isinstance(value, list):
        raise ValueError(f"{name} 必须是数组。")
    return value


def _execute_pool_tool(
    name: str,
    arguments: dict[str, Any],
    workspace: Path,
    workspace_id: str,
    pool: AgentPool,
) -> dict[str, Any]:
    if name == "agent_pool_capabilities":
        return pool.capabilities()
    if name == "agent_pool_spawn":
        target = resolve_agent_target(workspace, arguments.get("target_path"))
        return pool.spawn(
            workspace=target,
            route_root=workspace,
            route_workspace_id=workspace_id,
            prompt=str(arguments.get("prompt") or ""),
            title=str(arguments.get("title") or ""),
            executor=str(arguments.get("executor") or "auto"),
            model=str(arguments.get("model") or ""),
            write=bool(arguments.get("write", True)),
            isolation_mode=str(arguments.get("isolation_mode") or "auto"),
        )
    if name == "agent_pool_spawn_batch":
        target = resolve_agent_target(workspace, arguments.get("target_path"))
        return pool.spawn_batch(
            workspace=target,
            route_root=workspace,
            route_workspace_id=workspace_id,
            tasks=_require_list(arguments, "tasks"),
        )
    if name == "agent_pool_list":
        return pool.list()
    task_id = str(arguments.get("task_id") or "")
    if name == "agent_pool_get":
        return pool.get(task_id)
    if name == "agent_pool_wait":
        return pool.wait(task_id, int(arguments.get("wait_seconds") or 15))
    if name == "agent_pool_cancel":
        return pool.cancel(task_id)
    if name == "agent_pool_collect":
        return pool.collect(task_id, include_diff=bool(arguments.get("include_diff", True)))
    if name == "agent_pool_cleanup":
        return pool.cleanup(task_id, remove_branch=bool(arguments.get("remove_branch", False)))
    raise ValueError(f"未知 Agent Pool 工具：{name}")


def _execute_orchestrator_tool(
    name: str,
    arguments: dict[str, Any],
    workspace: Path,
    workspace_id: str,
    orchestrator: AgentOrchestrator,
) -> dict[str, Any]:
    if name == "spawn_agent":
        target = resolve_agent_target(workspace, arguments.get("target_path"))
        return orchestrator.spawn_agent(
            workspace=target,
            route_root=workspace,
            route_workspace_id=workspace_id,
            prompt=str(arguments.get("prompt") or ""),
            title=str(arguments.get("title") or ""),
            role=str(arguments.get("role") or "worker"),
            executor=str(arguments.get("executor") or "auto"),
            model=str(arguments.get("model") or ""),
            write=bool(arguments.get("write", True)),
            isolation_mode=str(arguments.get("isolation_mode") or "auto"),
        )
    if name == "spawn_agent_team":
        target = resolve_agent_target(workspace, arguments.get("target_path"))
        return orchestrator.spawn_agent_team(
            workspace=target,
            route_root=workspace,
            route_workspace_id=workspace_id,
            objective=str(arguments.get("objective") or ""),
            tasks=_require_list(arguments, "tasks"),
            title=str(arguments.get("title") or ""),
            executor=str(arguments.get("executor") or "auto"),
            model=str(arguments.get("model") or ""),
            reviewer=bool(arguments.get("reviewer", True)),
            merger=bool(arguments.get("merger", True)),
            reviewer_prompt=str(arguments.get("reviewer_prompt") or ""),
            merger_prompt=str(arguments.get("merger_prompt") or ""),
            reviewer_executor=str(arguments.get("reviewer_executor") or "auto"),
            reviewer_model=str(arguments.get("reviewer_model") or ""),
            merger_executor=str(arguments.get("merger_executor") or "auto"),
            merger_model=str(arguments.get("merger_model") or ""),
            success_policy=str(arguments.get("success_policy") or "all_required"),
            isolation_mode=str(arguments.get("isolation_mode") or "auto"),
        )
    if name == "list_agents":
        return orchestrator.list_agents(team_id=str(arguments.get("team_id") or ""))
    if name == "get_agent":
        return orchestrator.get_agent(str(arguments.get("agent_id") or ""))
    if name == "get_agent_team":
        return orchestrator.get_team(str(arguments.get("team_id") or ""))
    if name == "message_agent":
        return orchestrator.message_agent(
            str(arguments.get("agent_id") or ""),
            str(arguments.get("message") or ""),
        )
    if name == "cancel_agent":
        return orchestrator.cancel_agent(str(arguments.get("agent_id") or ""))
    if name == "wait_agents":
        raw_ids = _require_list(arguments, "agent_ids")
        return orchestrator.wait_agents(
            agent_ids=[str(item) for item in raw_ids],
            team_id=str(arguments.get("team_id") or ""),
            wait_seconds=int(arguments.get("wait_seconds") or 15),
        )
    if name == "cleanup_agent":
        return orchestrator.cleanup_agent(
            str(arguments.get("agent_id") or ""),
            remove_branch=bool(arguments.get("remove_branch", True)),
        )
    if name == "cleanup_agent_team":
        return orchestrator.cleanup_team(
            str(arguments.get("team_id") or ""),
            remove_branches=bool(arguments.get("remove_branches", True)),
        )
    raise ValueError(f"未知 Agent Orchestrator 工具：{name}")


def execute_agent_tool(
    *,
    name: str,
    arguments: dict[str, Any],
    workspace: Path,
    workspace_id: str,
    pool: AgentPool,
    orchestrator: AgentOrchestrator,
) -> dict[str, Any]:
    """Execute one Agent tool and return a serializable business payload."""
    if name in AGENT_POOL_TOOL_NAMES:
        result = _execute_pool_tool(name, arguments, workspace, workspace_id, pool)
        if name == "agent_pool_capabilities":
            # Executor processes are disposable, while the logical runtime task
            # that owns each turn is checkpointed and resumed after restart.
            # Expose both guarantees instead of leaking the legacy pool-only
            # lifecycle answer through the combined Gateway capability.
            result["execution_processes_survive_restart"] = False
            result["running_tasks_survive_restart"] = True
            result["persistent_runtime"] = orchestrator.runtime_capabilities()
        return result
    if name in AGENT_ORCHESTRATOR_TOOL_NAMES:
        return _execute_orchestrator_tool(name, arguments, workspace, workspace_id, orchestrator)
    raise ValueError(f"未知 Agent 工具：{name}")
