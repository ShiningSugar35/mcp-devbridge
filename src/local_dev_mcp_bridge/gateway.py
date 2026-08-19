"""OAuth gateway: public entry terminated by the Cloudflare Named Tunnel.

Listens on loopback only (``127.0.0.1:GATEWAY_PORT``). The Cloudflare route
for ``mcp.<domain>`` points at this app (instead of the engine), so the same
fixed URL ``https://<host>/mcp`` serves:

* **standard MCP OAuth** - protected-resource + authorization-server metadata,
  Dynamic Client Registration, /authorize (PKCE S256 with consent page),
  /token, /revoke - all via the mcp SDK 2.x ``create_auth_routes`` and our
  :class:`LocalOAuthProvider` (no hand-rolled protocol);
* **legacy ChatGPT bearer** - unchanged passthrough to the CodexPro engine,
  which validates the bearer itself (two-layer check);
* **OAuth access-token calls** - validated locally, then proxied to the
  engine with the engine bearer injected.

Only ``/mcp`` is exposed as the MCP resource; OAuth endpoints share the same
host. No second tunnel, no second URL, tokens never written to logs.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import threading
import time
from collections.abc import Callable
from email.utils import formatdate
from pathlib import Path
from typing import Any

import httpx
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from . import constants
from .agent_gateway import execute_agent_tool
from .agent_orchestrator import AgentOrchestrator
from .agent_pool import AgentPool
from .audit import AuditLogger
from .chatgpt_desktop import bridge_status, prepare_chatgpt_bridge, restore_normal_chatgpt_launch
from .constants import LOG_DIR as _LOG_DIR
from .device_hub import DeviceRegistry
from .oauth_provider import ConsentExpired, LocalOAuthProvider, _workspace_from_subject
from .platform_support import run_platform_kwargs
from .secrets import SecretsStore
from .shell import detect_binaries, get_shell_info, run_command, run_program

_DEVICE_TOOL_NAMES = frozenset(
    {
        "devbridge_list_devices",
        "devbridge_get_current_device",
        "devbridge_switch_device",
    }
)
_REMOTE_WORKSPACE_TOOL_NAMES = frozenset(
    {
        "devbridge_list_workspaces",
        "devbridge_get_current_workspace",
        "devbridge_switch_workspace",
    }
)
_AGENT_POOL_TOOL_NAMES = frozenset(
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
_AGENT_POOL_SPAWN_TOOL_NAMES = frozenset({"agent_pool_spawn", "agent_pool_spawn_batch"})
_AGENT_ORCHESTRATOR_TOOL_NAMES = frozenset(
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
_AGENT_ORCHESTRATOR_SPAWN_TOOL_NAMES = frozenset({"spawn_agent", "spawn_agent_team"})
_FORMAL_DEVICE_ROUTE_TOOL_NAMES = (
    _REMOTE_WORKSPACE_TOOL_NAMES | _AGENT_POOL_TOOL_NAMES | _AGENT_ORCHESTRATOR_TOOL_NAMES
)
_LOCAL_TOOL_NAMES = (
    frozenset(
        {
            "run_command",
            "run_program",
            "shell_self_test",
            "chatgpt_bridge_status",
            "prepare_chatgpt_bridge",
            "restore_chatgpt_bridge",
            "devbridge_list_workspaces",
            "devbridge_get_current_workspace",
            "devbridge_switch_workspace",
        }
    )
    | _DEVICE_TOOL_NAMES
    | _AGENT_POOL_TOOL_NAMES
    | _AGENT_ORCHESTRATOR_TOOL_NAMES
)

_ROUTE_WORKSPACE_ARG = "devbridge_workspace_id"
_ROUTE_DEVICE_ARG = "devbridge_device_id"
_ROUTE_HINT_DESCRIPTION = (
    "After a workspace or device switch, pass the returned routing value on later calls in this chat."
)

_PYTHON_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": "Run only a short PowerShell command (hard cap 20 seconds). For builds, tests, installs, crawls, or other long work, use the background bash task tool and poll with wait_task/get_task instead of holding one MCP call open.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to project root. Defaults to project root.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Short-call timeout in seconds (default 10, hard max 20). Use bash for long work.",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_program",
        "description": "Run only a short program directly (hard cap 20 seconds). For long-running programs use the background bash task tool and poll wait_task/get_task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "executable": {"type": "string", "description": "Program path or name"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Argument list",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to project root. Defaults to project root.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Short-call timeout in seconds (default 10, hard max 20). Use bash for long work.",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["executable"],
        },
    },
    {
        "name": "shell_self_test",
        "description": "Check whether the default shell is executable and whether python/git/pytest/pyright are available. Returns per-line checkmarks.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "devbridge_list_workspaces",
        "description": "List registered project workspaces. Pass device_id to query a specific online computer without relying on session switch state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "Optional device ID from devbridge_list_devices. Omit for the current/default computer.",
                }
            },
        },
    },
    {
        "name": "devbridge_get_current_workspace",
        "description": "Return the current workspace on a computer. Pass device_id for a stateless remote-device query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "Optional device ID from devbridge_list_devices.",
                }
            },
        },
    },
    {
        "name": "devbridge_switch_workspace",
        "description": "Switch the current MCP session to a different project workspace. Only affects the calling client, not other GPT/Gemini sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID from devbridge_list_workspaces output",
                },
                "device_id": {
                    "type": "string",
                    "description": "Optional device ID. When supplied, switches the workspace on that computer without depending on a previous device switch.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "devbridge_list_devices",
        "description": "List computers connected to this MCP DevBridge Hub and whether each one is online.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "devbridge_get_current_device",
        "description": "Return the computer currently selected for this MCP session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "devbridge_switch_device",
        "description": "Switch only the current MCP session to another online computer from devbridge_list_devices.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "Device ID from devbridge_list_devices",
                },
            },
            "required": ["device_id"],
        },
    },
    {
        "name": "chatgpt_bridge_status",
        "description": "Show whether the local ChatGPT Desktop ordinary-Chat Agent bridge is enabled and ready. This is read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prepare_chatgpt_bridge",
        "description": "Prepare local ChatGPT Desktop ordinary Chat as an MCP-backed Agent executor. By default it will not restart ChatGPT; pass restart=true only with explicit user approval because current ChatGPT Desktop windows will restart.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "restart": {"type": "boolean", "description": "Explicitly allow restarting ChatGPT Desktop if CDP is not already ready. Defaults false."},
                "debug_port": {"type": "integer", "minimum": 1024, "maximum": 65535, "description": "Optional loopback CDP port. Usually omit."},
            },
        },
    },
    {
        "name": "restore_chatgpt_bridge",
        "description": "Disable the ChatGPT ordinary-Chat Agent bridge and relaunch ChatGPT Desktop normally without the CDP port. This restarts ChatGPT Desktop.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agent_pool_capabilities",
        "description": "Show local Agent Pool executor availability, physical concurrency limits, and worktree-isolation support.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "Optional target device ID."}
            },
        },
    },
    {
        "name": "agent_pool_spawn",
        "description": "Queue one local implementation agent. Write-capable tasks use an isolated Git worktree/branch; the call returns immediately while physical concurrency stays bounded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed bounded task for the worker agent."},
                "title": {"type": "string", "description": "Short task title."},
                "executor": {"type": "string", "enum": ["auto", "chatgpt", "opencode", "claude"], "description": "Local executor. auto uses the preferred available CLI."},
                "model": {"type": "string", "description": "Optional provider/model identifier understood by OpenCode."},
                "write": {"type": "boolean", "description": "Allow code changes. Defaults true and requires a Git repository/worktree."},
                "device_id": {"type": "string", "description": "Optional target device ID."},
                "project_id": {"type": "string", "description": "Optional running project ID on the target device."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "agent_pool_spawn_batch",
        "description": "Queue up to 64 independent agent tasks. Tasks may outnumber the physical concurrency limit and will wait in the local queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "title": {"type": "string"},
                            "executor": {"type": "string", "enum": ["auto", "chatgpt", "opencode", "claude"]},
                            "model": {"type": "string"},
                            "write": {"type": "boolean"},
                        },
                        "required": ["prompt"],
                    },
                },
                "device_id": {"type": "string", "description": "Optional target device ID."},
                "project_id": {"type": "string", "description": "Optional running project ID on the target device."}
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "agent_pool_list",
        "description": "List recent Agent Pool tasks, including queued/running counts and the physical concurrency limit.",
        "inputSchema": {"type": "object", "properties": {"device_id": {"type": "string", "description": "Optional target device ID."}}},
    },
    {
        "name": "agent_pool_get",
        "description": "Get one Agent Pool task state and bounded output tail.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "device_id": {"type": "string", "description": "Optional target device ID."}}, "required": ["task_id"]},
    },
    {
        "name": "agent_pool_wait",
        "description": "Wait briefly for one Agent Pool task. The polling wait is capped at 30 seconds and never limits the worker process itself.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "wait_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                "device_id": {"type": "string", "description": "Optional target device ID."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "agent_pool_cancel",
        "description": "Cancel a queued/running Agent Pool task and terminate its process tree.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "device_id": {"type": "string", "description": "Optional target device ID."}}, "required": ["task_id"]},
    },
    {
        "name": "agent_pool_collect",
        "description": "Collect a terminal Agent Pool task with output tail plus bounded Git status/diff from its isolated worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "include_diff": {"type": "boolean", "description": "Include bounded unified diff. Defaults true."},
                "device_id": {"type": "string", "description": "Optional target device ID."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "agent_pool_cleanup",
        "description": "Remove a terminal task's worktree. The agent branch is preserved unless remove_branch=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "remove_branch": {"type": "boolean", "description": "Also delete the agent branch. Defaults false."},
                "device_id": {"type": "string", "description": "Optional target device ID."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "spawn_agent",
        "description": "Spawn one persistent logical coding Agent. Write agents use Git worktree isolation when available, or direct mode for non-Git targets. Use message_agent for later instructions; running one-shot CLIs receive them as a continuation turn on the same branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Bounded assignment."},
                "title": {"type": "string"},
                "role": {"type": "string", "enum": ["worker", "reviewer", "merger"], "description": "Defaults worker."},
                "executor": {"type": "string", "enum": ["auto", "chatgpt", "opencode", "claude"]},
                "model": {"type": "string"},
                "write": {"type": "boolean", "description": "Defaults true. auto uses a Git worktree in repositories and direct mode otherwise."},
                "device_id": {"type": "string", "description": "Optional target device ID."},
                "project_id": {"type": "string", "description": "Optional running project ID on the target device."}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "spawn_agent_team",
        "description": "Spawn a MiniMax-style coding team: parallel isolated workers, then optional automatic read-only Reviewer, then an isolated integration branch with a Merger agent. Returns immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "Overall team objective."},
                "title": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "title": {"type": "string"},
                            "executor": {"type": "string", "enum": ["auto", "chatgpt", "opencode", "claude"]},
                            "model": {"type": "string"},
                            "write": {"type": "boolean"}
                        },
                        "required": ["prompt"]
                    }
                },
                "executor": {"type": "string", "enum": ["auto", "chatgpt", "opencode", "claude"]},
                "model": {"type": "string"},
                "reviewer": {"type": "boolean", "description": "Run an automatic Reviewer after workers. Defaults true."},
                "merger": {"type": "boolean", "description": "Run an automatic Merger in an integration worktree after review. Defaults true for write teams."},
                "reviewer_prompt": {"type": "string", "description": "Optional additional review policy."},
                "merger_prompt": {"type": "string", "description": "Optional additional merge policy."},
                "reviewer_executor": {"type": "string", "enum": ["auto", "chatgpt", "opencode", "claude"]},
                "reviewer_model": {"type": "string"},
                "merger_executor": {"type": "string", "enum": ["auto", "chatgpt", "opencode", "claude"]},
                "merger_model": {"type": "string"},
                "device_id": {"type": "string", "description": "Optional target device ID."},
                "project_id": {"type": "string", "description": "Optional running project ID on the target device."}
            },
            "required": ["objective", "tasks"]
        }
    },
    {
        "name": "list_agents",
        "description": "List logical Agents and Agent Teams with roles, states, branches and current turns.",
        "inputSchema": {"type": "object", "properties": {"team_id": {"type": "string"}, "device_id": {"type": "string", "description": "Optional target device ID."}}}
    },
    {
        "name": "get_agent",
        "description": "Get one logical Agent including role, team, branch/worktree, messages, current turn and output tail.",
        "inputSchema": {"type": "object", "properties": {"agent_id": {"type": "string"}, "device_id": {"type": "string", "description": "Optional target device ID."}}, "required": ["agent_id"]}
    },
    {
        "name": "get_agent_team",
        "description": "Get one Agent Team including worker/reviewer/merger states and the final integration branch/worktree when available.",
        "inputSchema": {"type": "object", "properties": {"team_id": {"type": "string"}, "device_id": {"type": "string", "description": "Optional target device ID."}}, "required": ["team_id"]}
    },
    {
        "name": "message_agent",
        "description": "Send a follow-up instruction to a logical Agent. If its executor turn is still queued, the prompt is amended; if already running/finished, a continuation turn is queued on the same branch/worktree.",
        "inputSchema": {"type": "object", "properties": {"agent_id": {"type": "string"}, "message": {"type": "string"}, "device_id": {"type": "string", "description": "Optional target device ID."}}, "required": ["agent_id", "message"]}
    },
    {
        "name": "cancel_agent",
        "description": "Cancel the logical Agent's current executor turn, clear queued follow-up messages and terminate its process tree.",
        "inputSchema": {"type": "object", "properties": {"agent_id": {"type": "string"}, "device_id": {"type": "string", "description": "Optional target device ID."}}, "required": ["agent_id"]}
    },
    {
        "name": "wait_agents",
        "description": "Wait up to 30 seconds for selected logical Agents or a whole Team. Worker/model processes continue in the background after the polling wait returns.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "team_id": {"type": "string"},
                "wait_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                "device_id": {"type": "string", "description": "Optional target device ID."}
            }
        }
    },
]

def _augment_agent_tool_defs() -> None:
    spawn_names = {"agent_pool_spawn", "agent_pool_spawn_batch", "spawn_agent", "spawn_agent_team"}
    for tool in _PYTHON_TOOL_DEFS:
        name = str(tool.get("name") or "")
        if name not in spawn_names:
            continue
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        properties["target_path"] = {
            "type": "string",
            "description": "Optional absolute or workspace-relative directory for this agent. Defaults to the routed C:/D:/project root.",
        }
        if name != "agent_pool_spawn_batch":
            properties["isolation_mode"] = {
                "type": "string",
                "enum": ["auto", "git_worktree", "direct"],
                "description": "auto uses Git worktree inside a repository and direct local-write mode otherwise.",
            }
        if name == "spawn_agent_team":
            properties["success_policy"] = {
                "type": "string",
                "enum": ["all_required", "allow_partial"],
                "description": "Defaults all_required so partial worker success cannot be reported as team success.",
            }
        tasks = properties.get("tasks")
        if isinstance(tasks, dict):
            items = tasks.get("items")
            if isinstance(items, dict):
                item_props = items.get("properties")
                if isinstance(item_props, dict):
                    item_props["isolation_mode"] = {
                        "type": "string",
                        "enum": ["auto", "git_worktree", "direct"],
                    }
    _PYTHON_TOOL_DEFS.extend(
        [
            {
                "name": "cleanup_agent",
                "description": "Remove a terminal logical Agent plus its executor task metadata/worktree. Does not touch the primary workspace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "remove_branch": {"type": "boolean"},
                        "device_id": {"type": "string", "description": "Optional target device ID."},
                    },
                    "required": ["agent_id"],
                },
            },
            {
                "name": "cleanup_agent_team",
                "description": "Remove a terminal Agent Team, its logical agents, isolated worktrees and integration branch.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string"},
                        "remove_branches": {"type": "boolean"},
                        "device_id": {"type": "string", "description": "Optional target device ID."},
                    },
                    "required": ["team_id"],
                },
            },
        ]
    )


_augment_agent_tool_defs()

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>MCP DevBridge - 访问授权</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1f2328}}
h1{{font-size:20px}}
dl{{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:12px 16px}}
dt{{color:#57606a;font-size:12px;text-transform:uppercase;margin-top:8px}}
dd{{margin:2px 0 8px;word-break:break-all}}
form{{margin-top:16px;display:flex;gap:12px;align-items:center}}
button{{font-size:15px;padding:8px 22px;border-radius:6px;cursor:pointer}}
.allow{{background:#1f883d;color:#fff;border:1px solid #1f883d}}
.cancel{{background:#fff;border:1px solid #d0d7de}}
.note{{color:#57606a;font-size:13px;line-height:1.55}}
</style></head>
<body><h1>MCP DevBridge - 访问授权</h1>
<p>AI 客户端正在请求访问这个 MCP DevBridge Hub。</p>
<dl>{rows}</dl>
<p class="note">授权后无需在这里选择项目。连接成功后可以在同一个 ChatGPT / Gemini 会话中切换在线电脑和工作区；只有一个可用目标时会自动选择。</p>
<form method="post" action="/consent">
<input type="hidden" name="id" value="{cid}">
<button type="submit" name="decision" value="allow" class="allow">允许访问</button>
<button type="submit" name="decision" value="deny" class="cancel">取消</button>
</form></body></html>"""

_HOP_HEADERS = frozenset(
    {"host", "connection", "content-length", "transfer-encoding", "authorization"}
)

# --------------------------------------------------------- diagnostic logging
_DIAG_SENSITIVE_KEYS = frozenset(
    {
        "command",
        "code",
        "access_token",
        "refresh_token",
        "client_secret",
        "token",
        "authorization",
        "bearer",
        "code_verifier",
    }
)
_DIAG_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})
_DIAG_BODY_MAX = 2048
_DIAG_LOG_FILE: str = ""


def _diag_log_path() -> Path:
    log_dir = _LOG_DIR
    if not log_dir.exists():
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"gateway-{time.strftime('%Y-%m-%d')}.jsonl"


def _diag_short_hash(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _diag_redact_body(body: bytes | str) -> str:
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text[:_DIAG_BODY_MAX]
    if isinstance(data, dict):
        data = dict(data)
        for key in list(data):
            if key.lower() in _DIAG_SENSITIVE_KEYS:
                data[key] = "***REDACTED***"
    return json.dumps(data, ensure_ascii=False, default=str)[:_DIAG_BODY_MAX]


def _diag_redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: "***" if k.lower() in _DIAG_SENSITIVE_HEADERS else v for k, v in headers.items()}


def _write_diag_entry(**fields: Any) -> None:
    entry = {**fields, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    try:
        p = _diag_log_path()
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _row(label: str, value: str) -> str:
    return f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"


def _constant_time_eq(left: str, right: str | None) -> bool:
    return bool(right) and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _rewrite_server_identity(payload: bytes) -> bytes:
    """Replace the engine's serverInfo with MCP DevBridge on initialize.

    Only touches the display identity (name/title); neither the engine
    binary (a third-party upstream artifact) nor any protocol field changes.
    Handles both SSE (`data: {...}` lines) and plain JSON bodies.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload

    def _patch(obj: Any) -> Any:
        target = obj
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            target = obj["result"]
        if isinstance(target, dict) and isinstance(target.get("serverInfo"), dict):
            target["serverInfo"]["name"] = "mcp-devbridge"
            target["serverInfo"]["title"] = "MCP DevBridge"
            routing_note = (
                "\n\nMCP DevBridge routing: ChatGPT may recreate the MCP transport between "
                "tool-call batches. After devbridge_switch_workspace or "
                "devbridge_switch_device returns a routing value, include the returned "
                "devbridge_workspace_id / devbridge_device_id in subsequent tool calls "
                "in this conversation. Long-running local work must use the background "
                "bash task tool, then poll with wait_task/get_task; do not hold a single "
                "run_command/run_program call open for builds, tests, installs, or crawls."
            )
            current = str(target.get("instructions") or "")
            if "MCP DevBridge routing:" not in current:
                target["instructions"] = current + routing_note
        return obj

    if text.lstrip().startswith("data:") or "\ndata:" in text:
        out_lines = []
        for line in text.splitlines(keepends=True):
            if line.startswith("data:"):
                body_line = line[5:].lstrip()
                if body_line.strip() == "[DONE]":
                    continue
                try:
                    obj = json.loads(body_line.strip())
                    out_lines.append(f"data: {json.dumps(_patch(obj), ensure_ascii=False)}\n")
                    continue
                except json.JSONDecodeError:
                    pass
            out_lines.append(line)
        return "".join(out_lines).encode("utf-8")

    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and (
            "serverInfo" in obj
            or (isinstance(obj.get("result"), dict) and "serverInfo" in obj["result"])
        ):
            return json.dumps(_patch(obj), ensure_ascii=False).encode("utf-8")
    except json.JSONDecodeError:
        pass
    return payload


def _analyze_tools(payload: bytes) -> tuple[int, list[str]]:
    """Return (total_tool_count, list_of_duplicate_names) from a tools/list response."""
    count = 0
    dupes: list[str] = []
    try:
        text = payload.decode("utf-8")
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            tools = data["result"].get("tools") or []
            if isinstance(tools, list):
                names = [t.get("name", "") for t in tools if isinstance(t, dict)]
                count = len(names)
                seen: set[str] = set()
                for n in names:
                    if n in seen:
                        dupes.append(n)
                    seen.add(n)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return count, dupes


def _inject_tools(payload: bytes) -> bytes:
    """Add Gateway tools and stateless routing hints to tools/list."""
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload

    def add_route_args(tool: dict[str, Any]) -> None:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            return
        props = schema.setdefault("properties", {})
        if not isinstance(props, dict):
            return
        props.setdefault(
            _ROUTE_WORKSPACE_ARG,
            {"type": "string", "description": _ROUTE_HINT_DESCRIPTION},
        )
        props.setdefault(
            _ROUTE_DEVICE_ARG,
            {"type": "string", "description": _ROUTE_HINT_DESCRIPTION},
        )
        description = str(tool.get("description") or "")
        note = " Optional MCP DevBridge routing fields may be supplied after a switch."
        if note.strip() not in description:
            tool["description"] = description + note

    def patch_obj(obj: Any) -> Any:
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            tools = obj["result"].get("tools")
            if isinstance(tools, list):
                for item in tools:
                    if isinstance(item, dict):
                        add_route_args(item)
                names = {
                    str(item.get("name", ""))
                    for item in tools
                    if isinstance(item, dict) and item.get("name")
                }
                for tool in _PYTHON_TOOL_DEFS:
                    if tool["name"] in names:
                        continue
                    injected = dict(tool)
                    input_schema = dict(tool.get("inputSchema") or {})
                    input_schema["properties"] = dict(input_schema.get("properties") or {})
                    injected["inputSchema"] = input_schema
                    add_route_args(injected)
                    tools.append(injected)
                    names.add(tool["name"])
        return obj

    if decoded.lstrip().startswith("data:") or "\ndata:" in decoded:
        out_lines = []
        for line in decoded.splitlines(keepends=True):
            if line.startswith("data:"):
                body_line = line[5:].lstrip()
                if body_line.strip() == "[DONE]":
                    continue
                try:
                    obj = json.loads(body_line.strip())
                    out_lines.append(f"data: {json.dumps(patch_obj(obj), ensure_ascii=False)}\n")
                    continue
                except json.JSONDecodeError:
                    pass
            out_lines.append(line)
        return "".join(out_lines).encode("utf-8")

    try:
        obj = json.loads(decoded)
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict) and "tools" in obj["result"]:
            return json.dumps(patch_obj(obj), ensure_ascii=False).encode("utf-8")
    except json.JSONDecodeError:
        pass
    return payload


def _jsonrpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _mcp_tool_payload(value: Any) -> dict[str, Any]:
    """Return the standard MCP CallToolResult shape for Python-local tools."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    structured = value if isinstance(value, dict) else {"value": value}
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
    }


def _jsonrpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_arguments(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    value = params.get("arguments")
    return dict(value) if isinstance(value, dict) else {}


def _strip_route_arguments(body: bytes, rpc: dict[str, Any] | None, *, keep_workspace: bool) -> bytes:
    if rpc is None or str(rpc.get("method", "")) != "tools/call":
        return body
    params = rpc.get("params")
    if not isinstance(params, dict):
        return body
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return body
    if _ROUTE_DEVICE_ARG not in arguments and (keep_workspace or _ROUTE_WORKSPACE_ARG not in arguments):
        return body
    copied_rpc = dict(rpc)
    copied_params = dict(params)
    copied_args = dict(arguments)
    copied_args.pop(_ROUTE_DEVICE_ARG, None)
    if not keep_workspace:
        copied_args.pop(_ROUTE_WORKSPACE_ARG, None)
    copied_params["arguments"] = copied_args
    copied_rpc["params"] = copied_params
    return json.dumps(copied_rpc, ensure_ascii=False).encode("utf-8")


def _is_loopback(request: Request) -> bool:
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            if first:
                return first.startswith("127.")
    client = request.client
    return bool(
        client
        and (client.host in ("127.0.0.1", "::1", "localhost") or client.host.startswith("127."))
    )


class _DiagnosticMiddleware:
    """Pure ASGI middleware that logs every request/response to gateway JSONL.

    Uses pure ASGI (not BaseHTTPMiddleware) to safely wrap SSE streaming responses.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_ns = time.monotonic_ns()
        method: str = scope.get("method", "")
        path: str = scope.get("path", "")
        status: list[int] = [0]
        resp_content_type: list[str] = [""]
        resp_bytes: list[int] = [0]

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status[0] = message.get("status", 0)
                for h in message.get("headers") or []:
                    if h[0].decode("latin-1").lower() == "content-type":
                        resp_content_type[0] = h[1].decode("latin-1")
            elif message["type"] == "http.response.body":
                body = message.get("body") or b""
                resp_bytes[0] += len(body)
            await send(message)

        exc_type: str = ""
        exc_msg: str = ""
        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            exc_type = type(exc).__name__
            exc_msg = _diag_redact_body(str(exc)[:500])
            raise
        finally:
            duration_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)
            _write_diag_entry(
                path=path,
                method=method,
                status=status[0],
                content_type=resp_content_type[0],
                resp_bytes=resp_bytes[0],
                duration_ms=duration_ms,
                exc_type=exc_type,
                exc_msg=exc_msg,
            )


class OAuthGateway:
    """The public Starlette app + in-process uvicorn thread (loopback only)."""

    def __init__(
        self,
        *,
        public_hostname: str,
        workspace: str = "",
        upstream_url: str = "",
        upstream_legacy_token: Callable[[], str | None] | None = None,
        allow_local_anonymous: bool = True,
        store: SecretsStore | None = None,
        provider: LocalOAuthProvider | None = None,
        transport: Any | None = None,
        workspace_registry: Callable[[str], tuple[int, str] | None] | None = None,
        workspace_credential_registry: Callable[[str], str | None] | None = None,
        device_registry: DeviceRegistry | None = None,
        local_device_id: str = "",
    ) -> None:
        hostname = (public_hostname or "").strip().rstrip("/")
        base = (
            hostname
            if hostname.lower().startswith(("http://", "https://"))
            else f"https://{hostname}"
        )
        self.public_base = base.rstrip("/")
        self.resource_url = f"{self.public_base}{constants.DEFAULT_MCP_PATH}"
        if upstream_url:
            self.upstream_url = upstream_url
        else:
            try:
                from .engines import CODEXPRO_LOCAL_PORT
            except Exception:  # pragma: no cover - import always succeeds in-package
                CODEXPRO_LOCAL_PORT = 8787
            self.upstream_url = f"http://127.0.0.1:{CODEXPRO_LOCAL_PORT}"
        self.upstream_token_source = upstream_legacy_token or (
            lambda: SecretsStore().get(constants.ACCESS_TOKEN_CRED_NAME)
        )
        self.allow_local_anonymous = allow_local_anonymous
        self._workspace = Path(workspace) if workspace else None
        self._workspace_registry = workspace_registry
        self._workspace_credential_registry = workspace_credential_registry
        self._device_registry = device_registry
        self._local_device_id = local_device_id or (
            device_registry.local_device_id if device_registry else ""
        )
        self._audit = AuditLogger()
        self._provider = provider or LocalOAuthProvider(
            issuer_url=self.public_base,
            resource_url=self.resource_url,
            workspace=workspace,
            store=store or SecretsStore(),
        )
        self.app = self._build_app()
        self._server: Any | None = None
        self._thread: threading.Thread | None = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, connect=30.0),
            transport=transport,
        )
        # Per-client-session routing plus per-upstream MCP transport sessions.
        # One ChatGPT-facing session can switch among several independent CodexPro
        # servers, but every CodexPro server owns a different mcp-session-id.
        self._session_workspaces: dict[str, str] = {}
        self._session_devices: dict[str, str] = {}
        self._upstream_sessions: dict[str, dict[str, str]] = {}
        self._initialize_requests: dict[str, bytes] = {}
        self._session_lock = threading.Lock()
        self._agent_pool: AgentPool | None = None
        self._agent_orchestrator: AgentOrchestrator | None = None

    # ------------------------------------------------------------ build
    @property
    def provider(self) -> LocalOAuthProvider:
        return self._provider

    def _build_app(self) -> Any:
        registration = ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[constants.OAUTH_SCOPE],
            default_scopes=[constants.OAUTH_SCOPE],
        )
        routes: list[Route] = create_auth_routes(
            self._provider,
            issuer_url=AnyHttpUrl(self.public_base),
            client_registration_options=registration,
            revocation_options=RevocationOptions(enabled=True),
        )
        resource = create_protected_resource_routes(
            AnyHttpUrl(self.resource_url),
            [AnyHttpUrl(self.public_base)],
            scopes_supported=[constants.OAUTH_SCOPE],
            resource_name="MCP DevBridge",
        )
        routes.extend(resource)
        routes.append(
            Route(
                "/.well-known/oauth-protected-resource",
                endpoint=resource[0].endpoint,
                methods=["GET", "OPTIONS"],
            )
        )
        routes.append(
            Route(constants.DEFAULT_MCP_PATH, self._mcp_endpoint, methods=["GET", "POST"])
        )
        routes.append(Route("/consent", self._consent_page, methods=["GET"]))
        routes.append(Route("/consent", self._consent_submit, methods=["POST"]))
        routes.append(Route("/device/register", self._device_register, methods=["POST"]))
        routes.append(Route("/device/heartbeat", self._device_heartbeat, methods=["POST"]))
        routes.append(Route("/health", self._health))
        app = Starlette(routes=routes)
        return _DiagnosticMiddleware(app)

    # ---------------------------------------------------------- consent
    async def _consent_page(self, request: Request) -> Response:
        consent = self._provider.get_consent(request.query_params.get("id", ""))
        if consent is None:
            return HTMLResponse(
                "<html><body><p>授权请求已过期或不存在，请重新发起连接。</p></body></html>",
                status_code=410,
            )
        rows = [
            _row("MCP Server", self.resource_url),
            _row("访问范围", "此 Hub 下的在线设备与工作区（连接后按会话切换）"),
            _row("请求的权限范围", ", ".join(consent["scopes"])),
            _row("调用方 Client ID", consent["client_id"]),
        ]
        cid = html.escape(request.query_params.get("id", ""))
        return HTMLResponse(_PAGE.format(rows="".join(rows), cid=cid))

    def _build_workspace_options(self) -> str:
        """Build <option> tags for available projects (for consent page dropdown)."""
        if self._workspace_registry is None:
            options = ['<option value="">（当前激活项目）</option>']
        else:
            options = ['<option value="" selected disabled>（请选择一个运行中的项目）</option>']
        if self._workspace_registry is not None:
            try:
                from .config_store import load_projects

                projects = load_projects()
                for p in projects:
                    if p.id:
                        port = ""
                        info = self._workspace_registry(p.id)
                        if info:
                            port = f"（运行中 :{info[0]}）"
                        selected = ""
                        name = p.display_name or Path(p.root_path).name
                        options.append(
                            f'<option value="{html.escape(p.id)}" {selected}>'
                            f"{html.escape(name)} - {html.escape(p.root_path)}{html.escape(port)}"
                            f"</option>"
                        )
            except Exception:
                pass
        return "\n".join(options)

    async def _consent_submit(self, request: Request) -> Response:
        form = await request.form()
        consent_id = str(form.get("id", ""))
        decision = str(form.get("decision", "deny"))
        _write_diag_entry(path="/consent", method="POST", decision=decision)
        try:
            if decision == "allow":
                target = self._provider.approve(consent_id)
            else:
                target = self._provider.deny(consent_id)
        except ConsentExpired:
            _write_diag_entry(
                path="/consent",
                method="POST",
                error="consent_expired",
            )
            return HTMLResponse(
                "<html><body><p>授权已过期或不存在，请重新发起连接。</p></body></html>",
                status_code=410,
            )
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------ device pairing
    async def _device_register(self, request: Request) -> Response:
        if self._device_registry is None:
            return JSONResponse(
                {"ok": False, "message": "当前 Hub 未启用多设备功能。"}, status_code=404
            )
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确。")
            peer_value = self._device_registry.register_remote(
                pair_code=str(payload.get("pair_code", "")),
                device_id=str(payload.get("device_id", "")),
                name=str(payload.get("name", "")),
                endpoint_url=str(payload.get("mcp_url", "")),
                bearer=str(payload.get("bearer", "")),
            )
            _write_diag_entry(path="/device/register", method="POST", event="device_paired")
            return JSONResponse({"ok": True, "peer_secret": peer_value, "heartbeat_seconds": 15})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    async def _device_heartbeat(self, request: Request) -> Response:
        if self._device_registry is None:
            return JSONResponse(
                {"ok": False, "message": "当前 Hub 未启用多设备功能。"}, status_code=404
            )
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确。")
            device = self._device_registry.heartbeat(
                device_id=str(payload.get("device_id", "")),
                peer_secret=str(payload.get("peer_secret", "")),
                endpoint_url=str(payload.get("mcp_url", "")),
                name=str(payload.get("name", "")),
                bearer=str(payload.get("bearer", "")),
            )
            return JSONResponse({"ok": True, "device_id": device.id})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=401)

    # ------------------------------------------------------------- /mcp
    async def _mcp_endpoint(self, request: Request) -> Response:
        body = await request.body()
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        engine_credential = self.upstream_token_source()
        session_id = self._extract_session_id(request)

        jsonrpc_method = ""
        tool_name = ""
        rpc: dict[str, Any] | None = None
        if request.method == "POST":
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    rpc = parsed
                    jsonrpc_method = str(parsed.get("method", ""))
                    if jsonrpc_method == "tools/call":
                        tool_name = str((parsed.get("params") or {}).get("name", ""))
            except json.JSONDecodeError:
                rpc = None

        call_arguments = _tool_arguments(rpc.get("params") if rpc is not None else {})
        synthetic_workspace_route = str(call_arguments.get(_ROUTE_WORKSPACE_ARG) or "").strip()
        synthetic_device_route = str(call_arguments.get(_ROUTE_DEVICE_ARG) or "").strip()
        route_workspace_id = synthetic_workspace_route
        route_device_id = synthetic_device_route
        if not route_device_id and tool_name in _FORMAL_DEVICE_ROUTE_TOOL_NAMES:
            route_device_id = str(call_arguments.get("device_id") or "").strip()
        if (
            not route_workspace_id
            and tool_name in (_AGENT_POOL_SPAWN_TOOL_NAMES | _AGENT_ORCHESTRATOR_SPAWN_TOOL_NAMES)
            and (not route_device_id or route_device_id == self._local_device_id)
        ):
            route_workspace_id = str(call_arguments.get("project_id") or "").strip()

        proxy_token: str | None = None
        workspace_id = ""
        direct_workspace = ""
        upstream_target: str | None = None
        if bearer:
            direct_workspace = self._workspace_for_credential(bearer)
            if _constant_time_eq(bearer, engine_credential) or direct_workspace:
                workspace_id = direct_workspace
                proxy_token = self._credential_for_workspace(
                    workspace_id, engine_credential or bearer
                )
            else:
                record = await self._provider.load_access_token(bearer)
                if record is None:
                    return self._unauthorized()
                if record.resource and record.resource.rstrip("/") != self.resource_url:
                    return self._unauthorized()
                workspace_id = _workspace_from_subject(record.subject or "")
                proxy_token = self._credential_for_workspace(workspace_id, engine_credential)
                if not proxy_token:
                    return self._unauthorized()
        elif self.allow_local_anonymous and _is_loopback(request):
            proxy_token = None
        else:
            return self._unauthorized()

        if route_workspace_id and direct_workspace and route_workspace_id != direct_workspace:
            return JSONResponse(
                _jsonrpc_error(None, -32602, "当前 Bearer 已固定到另一个工作区，不能覆盖路由。")
            )

        if route_device_id:
            views = (
                self._device_registry.views(local_online=self._local_device_online())
                if self._device_registry is not None
                else []
            )
            if self._device_registry is None:
                if route_device_id != self._local_device_id:
                    return JSONResponse(_jsonrpc_error(None, -32602, "指定电脑不存在。"))
            else:
                target_view = next((view for view in views if view.id == route_device_id), None)
                if target_view is None or not target_view.online:
                    return JSONResponse(_jsonrpc_error(None, -32001, "指定电脑当前不可用。"), status_code=502)
            device_id = route_device_id
            if session_id and synthetic_device_route:
                with self._session_lock:
                    self._session_devices[session_id] = device_id
        else:
            device_id = self._effective_device(session_id)

        remote = None
        upstream_key = ""
        if self._device_registry is not None and device_id and device_id != self._local_device_id:
            remote = self._device_registry.resolve_remote(device_id)
            if remote is None:
                return JSONResponse(
                    _jsonrpc_error(
                        None, -32001, "目标电脑当前离线。请用 devbridge_list_devices 查看在线设备。"
                    ),
                    status_code=502,
                )
            upstream_target = remote.base_url
            upstream_key = f"remote:{device_id}"
            proxy_token = remote.bearer
        else:
            if route_workspace_id:
                if self._workspace_registry and not self._workspace_registry(route_workspace_id):
                    return JSONResponse(
                        _jsonrpc_error(None, -32000, "指定工作区尚未启动或不存在。"),
                        status_code=502,
                    )
                workspace_id = route_workspace_id
                if session_id and synthetic_workspace_route:
                    with self._session_lock:
                        self._session_workspaces[session_id] = workspace_id
            workspace_id = self._effective_workspace(
                workspace_id, session_id, pinned=bool(direct_workspace)
            )
            if workspace_id:
                upstream_target = self._resolve_upstream(workspace_id)
                upstream_key = f"local:{workspace_id}"
                if not upstream_target:
                    return JSONResponse(
                        _jsonrpc_error(
                            None, -32000, "当前项目尚未启动。请先在 MCP DevBridge 桌面启动它。"
                        ),
                        status_code=502,
                    )
                proxy_token = self._credential_for_workspace(
                    workspace_id, engine_credential or proxy_token
                )

        if rpc is not None and jsonrpc_method == "tools/call":
            params = rpc.get("params") or {}
            if tool_name in _DEVICE_TOOL_NAMES:
                result = await self._exec_local_tool(
                    tool_name, rpc, params, workspace_id, session_id
                )
                self._audit_gateway_tool(request, rpc, tool_name, workspace_id, device_id, True)
                return result
            if remote is None and tool_name in _LOCAL_TOOL_NAMES:
                result = await self._exec_local_tool(
                    tool_name, rpc, params, workspace_id, session_id
                )
                self._audit_gateway_tool(request, rpc, tool_name, workspace_id, device_id, True)
                return result

        body = _strip_route_arguments(body, rpc, keep_workspace=remote is not None)
        upstream_session_id = session_id
        if session_id and jsonrpc_method != "initialize" and upstream_target and upstream_key:
            ensured = await self._ensure_upstream_session(
                client_session_id=session_id,
                upstream_key=upstream_key,
                upstream_target=upstream_target,
                authorization=proxy_token,
            )
            if not ensured:
                return JSONResponse(
                    _jsonrpc_error(None, -32001, "无法为目标工作区建立 MCP 上游会话。"),
                    status_code=502,
                )
            upstream_session_id = ensured
        return await self._proxy(
            request,
            body,
            proxy_token,
            upstream_target=upstream_target,
            upstream_key=upstream_key,
            client_session_id=session_id,
            upstream_session_id=upstream_session_id,
            jsonrpc_method=jsonrpc_method,
            tool_name=tool_name,
            workspace_id=workspace_id,
            session_id=session_id,
            device_id=device_id,
            rpc=rpc,
        )

    async def _proxy(
        self,
        request: Request,
        body: bytes,
        authorization: str | None,
        *,
        upstream_target: str | None = None,
        upstream_key: str = "",
        client_session_id: str = "",
        upstream_session_id: str = "",
        jsonrpc_method: str = "",
        tool_name: str = "",
        workspace_id: str = "",
        session_id: str = "",
        device_id: str = "",
        rpc: dict[str, Any] | None = None,
    ) -> Response:
        base = upstream_target or self.upstream_url
        target = f"{base}{request.url.path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        headers = {
            key: value for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS
        }
        headers["accept-encoding"] = "identity"
        if authorization:
            headers["authorization"] = f"Bearer {authorization}"
        if upstream_session_id:
            headers["mcp-session-id"] = upstream_session_id
        else:
            headers.pop("mcp-session-id", None)
        started = time.monotonic()
        try:
            upstream = await self._http.send(
                self._http.build_request(request.method, target, content=body, headers=headers),
                stream=True,
            )
        except httpx.HTTPError:
            if tool_name:
                self._audit_gateway_tool(
                    request,
                    rpc,
                    tool_name,
                    workspace_id,
                    device_id,
                    False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_type="upstream_unreachable",
                )
            return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
        if tool_name:
            self._audit_gateway_tool(
                request,
                rpc,
                tool_name,
                workspace_id,
                device_id,
                upstream.status_code < 400,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_type="" if upstream.status_code < 400 else f"http_{upstream.status_code}",
            )
        filtered = {
            key: value for key, value in upstream.headers.items() if key.lower() not in _HOP_HEADERS
        }
        if request.method == "POST":
            if b'"tools/list"' in body:
                await upstream.aread()
                rewritten = _inject_tools(upstream.content)
                tool_count, dupes = _analyze_tools(rewritten)
                _write_diag_entry(
                    path=request.url.path,
                    method=request.method,
                    jsonrpc_method="tools/list",
                    upstream_status=upstream.status_code,
                    upstream_target=target,
                    injected_tool_count=len(_PYTHON_TOOL_DEFS),
                    total_tool_count=tool_count,
                    duplicate_tools=dupes,
                    workspace_hash=_diag_short_hash(workspace_id),
                )
                return Response(
                    content=rewritten, status_code=upstream.status_code, headers=filtered
                )
            if b"initialize" in body:
                await upstream.aread()
                returned_session_id = upstream.headers.get("mcp-session-id", "").strip()
                if returned_session_id and upstream_key:
                    # The first upstream session id is also the client-facing id.
                    # Later workspace/device targets receive their own mapped ids.
                    self._remember_upstream_session(
                        returned_session_id, upstream_key, returned_session_id, body
                    )
                rewritten = _rewrite_server_identity(upstream.content)
                _write_diag_entry(
                    path=request.url.path,
                    method=request.method,
                    jsonrpc_method="initialize",
                    upstream_status=upstream.status_code,
                    upstream_target=target,
                )
                return Response(
                    content=rewritten, status_code=upstream.status_code, headers=filtered
                )
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=filtered,
        )

    # ------------------------------------------------------- Agent Pool
    def _get_agent_pool(self) -> AgentPool:
        if self._agent_pool is None:
            self._agent_pool = AgentPool()
        return self._agent_pool

    def _get_agent_orchestrator(self) -> AgentOrchestrator:
        if self._agent_orchestrator is None:
            self._agent_orchestrator = AgentOrchestrator(self._get_agent_pool())
        return self._agent_orchestrator

    # ---------------------------------------------------- local tools
    async def _exec_local_tool(
        self,
        name: str,
        rpc: dict[str, Any],
        params: dict[str, Any],
        workspace_id: str = "",
        session_id: str = "",
    ) -> JSONResponse:
        rpc_id = rpc.get("id")
        arguments = _tool_arguments(params)
        workspace = self._resolve_workspace_path(workspace_id) or self._workspace or Path.cwd()
        try:
            if name == "run_command":
                command = str(arguments.get("command", ""))
                if not command.strip():
                    raise ValueError("command 不能为空。")
                cwd_rel = str(arguments.get("cwd", "")).strip()
                cwd = (workspace / cwd_rel).resolve() if cwd_rel else workspace
                timeout = max(1, min(int(arguments.get("timeout_seconds") or 10), 20))
                res = run_command(command, cwd=cwd, timeout_seconds=timeout)
                text = (
                    f"shell: {res.shell}\n"
                    f"exit_code: {res.exit_code}\n"
                    f"duration: {res.duration_seconds:.2f}s\n"
                    f"{'*** TIMED OUT ***' if res.timed_out else ''}\n"
                    f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
                )
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": text}]})
                )
            elif name == "run_program":
                executable = str(arguments.get("executable", ""))
                if not executable.strip():
                    raise ValueError("executable 不能为空。")
                args = [str(a) for a in (arguments.get("args") or [])]
                cwd_rel = str(arguments.get("cwd", "")).strip()
                cwd = (workspace / cwd_rel).resolve() if cwd_rel else workspace
                timeout = max(1, min(int(arguments.get("timeout_seconds") or 10), 20))
                res = run_program(executable, args, cwd=cwd, timeout_seconds=timeout)
                text = (
                    f"command: {res.command}\n"
                    f"exit_code: {res.exit_code}\n"
                    f"duration: {res.duration_seconds:.2f}s\n"
                    f"{'*** TIMED OUT ***' if res.timed_out else ''}\n"
                    f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
                )
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": text}]})
                )
            elif name == "shell_self_test":
                lines: list[str] = []
                info = get_shell_info()
                default = info.get("default") or {}
                if isinstance(default, dict):
                    if default.get("executable"):
                        lines.append(
                            f"[✓] shell: {default.get('name', '?')} ({default.get('path', '?')})"
                        )
                    else:
                        lines.append(f"[✗] shell: 不可执行 ({default.get('path', '?')})")
                bin_versions = detect_binaries()
                for tool in ("python", "git", "node", "npm"):
                    ver = bin_versions.get(tool, "")
                    symbol = "[✓]" if ver else "[✗]"
                    lines.append(f"{symbol} {tool}: {ver or '未安装'}")
                for tool in ("pytest", "pyright"):
                    import subprocess as _sp

                    try:
                        r = _sp.run(
                            ["python", "-m", tool, "--version"],
                            capture_output=True,
                            timeout=30,
                            **run_platform_kwargs(),
                        )
                        out = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
                        lines.append(f"[✓] {tool}: {out[0] if out else 'ok'} (python -m {tool})")
                    except Exception:
                        lines.append(f"[✗] {tool}: 未安装或不可调用")
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id, {"content": [{"type": "text", "text": "\n".join(lines)}]}
                    )
                )
            elif name == "devbridge_list_workspaces":
                result = self._list_workspaces()
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": result}]})
                )
            elif name == "devbridge_get_current_workspace":
                result = self._get_current_workspace(workspace_id, session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_WORKSPACE_ARG: workspace_id},
                        },
                    )
                )
            elif name == "devbridge_switch_workspace":
                target_id = str(arguments.get("project_id", ""))
                if not target_id:
                    raise ValueError("project_id 不能为空。")
                result = self._do_switch_workspace(target_id, session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_WORKSPACE_ARG: target_id},
                        },
                    )
                )
            elif name == "devbridge_list_devices":
                result = self._list_devices(session_id)
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": result}]})
                )
            elif name == "devbridge_get_current_device":
                result = self._get_current_device(session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_DEVICE_ARG: self._effective_device(session_id)},
                        },
                    )
                )
            elif name == "devbridge_switch_device":
                target_id = str(arguments.get("device_id", ""))
                if not target_id:
                    raise ValueError("device_id 不能为空。")
                result = self._do_switch_device(target_id, session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_DEVICE_ARG: target_id},
                        },
                    )
                )
            elif name == "chatgpt_bridge_status":
                return JSONResponse(_jsonrpc_result(rpc_id, _mcp_tool_payload(bridge_status())))
            elif name == "prepare_chatgpt_bridge":
                result = prepare_chatgpt_bridge(
                    restart=bool(arguments.get("restart", False)),
                    debug_port=int(arguments.get("debug_port") or 0),
                )
                return JSONResponse(_jsonrpc_result(rpc_id, _mcp_tool_payload(result)))
            elif name == "restore_chatgpt_bridge":
                return JSONResponse(_jsonrpc_result(rpc_id, _mcp_tool_payload(restore_normal_chatgpt_launch())))
            elif name in (_AGENT_POOL_TOOL_NAMES | _AGENT_ORCHESTRATOR_TOOL_NAMES):
                result = execute_agent_tool(
                    name=name,
                    arguments=arguments,
                    workspace=workspace,
                    workspace_id=workspace_id,
                    pool=self._get_agent_pool(),
                    orchestrator=self._get_agent_orchestrator(),
                )
                return JSONResponse(_jsonrpc_result(rpc_id, _mcp_tool_payload(result)))
            else:
                raise ValueError(f"未知的本地工具: {name}")
        except ValueError as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32602, str(exc)))
        except Exception as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32603, f"工具执行失败: {exc}"))

    # ----------------------------------------------------------- device helpers
    def _local_device_online(self) -> bool:
        if self._workspace_registry is None:
            return True
        try:
            from .config_store import load_projects

            return any(p.id and self._workspace_registry(p.id) for p in load_projects())
        except Exception:
            return True

    def _effective_device(self, session_id: str) -> str:
        if self._device_registry is None:
            return self._local_device_id
        online = self._device_registry.online_ids(local_online=self._local_device_online())
        with self._session_lock:
            selected = self._session_devices.get(session_id, "") if session_id else ""
            if selected in online:
                return selected
            if len(online) == 1:
                if session_id:
                    self._session_devices[session_id] = online[0]
                return online[0]
        if self._local_device_id in online:
            return self._local_device_id
        return online[0] if online else ""

    def _list_devices(self, session_id: str) -> str:
        if self._device_registry is None:
            return "当前仅启用本机设备。"
        current = self._effective_device(session_id)
        views = self._device_registry.views(local_online=self._local_device_online())
        rows = ["可用电脑："]
        for view in views:
            state = "在线" if view.online else "离线"
            suffix = "（本机）" if view.local else ""
            selected = " ← 当前" if view.id == current else ""
            rows.append(f"- {view.name}{suffix} · {state} · id={view.id}{selected}")
        online_count = sum(1 for view in views if view.online)
        if online_count == 1:
            rows.append("\n目前只有一台电脑在线，系统已自动使用它。")
        elif online_count > 1:
            rows.append("\n多台电脑在线时，可用 devbridge_switch_device 切换；只影响当前聊天会话。")
        return "\n".join(rows)

    def _get_current_device(self, session_id: str) -> str:
        current = self._effective_device(session_id)
        if self._device_registry is None:
            return "当前电脑：本机"
        view = next(
            (
                item
                for item in self._device_registry.views(local_online=self._local_device_online())
                if item.id == current
            ),
            None,
        )
        if view is None:
            return "当前没有可用电脑。"
        return f"当前电脑：{view.name}{'（本机）' if view.local else ''}\nid={view.id}"

    def _do_switch_device(self, device_id: str, session_id: str) -> str:
        if self._device_registry is None:
            raise ValueError("当前没有启用 Multi-Device Hub。")
        views = self._device_registry.views(local_online=self._local_device_online())
        target = next((view for view in views if view.id == device_id), None)
        if target is None:
            raise ValueError(f"找不到电脑：{device_id}。请先用 devbridge_list_devices 查看。")
        if not target.online:
            raise ValueError(f"电脑“{target.name}”当前离线。")
        if session_id:
            with self._session_lock:
                self._session_devices[session_id] = device_id
                self._session_workspaces.pop(session_id, None)
        return (
            f"已切换到电脑：{target.name}\n"
            f"后续工具调用请携带 {_ROUTE_DEVICE_ARG}={device_id}；这不依赖底层 MCP transport session。\n"
            "该电脑会自动选择唯一运行的工作区；有多个时可继续切换工作区。"
        )

    def _audit_gateway_tool(
        self,
        request: Request,
        rpc: dict[str, Any] | None,
        tool_name: str,
        workspace_id: str,
        device_id: str,
        success: bool,
        *,
        duration_ms: int = 0,
        error_type: str = "",
    ) -> None:
        params = (rpc or {}).get("params") or {}
        arguments = params.get("arguments") if isinstance(params, dict) else {}
        if not isinstance(arguments, dict):
            arguments = {}
        workspace = str(self._resolve_workspace_path(workspace_id) or "")
        if device_id and device_id != self._local_device_id:
            workspace = f"remote:{device_id}"
        ua = request.headers.get("user-agent", "")
        self._audit.log_tool_call(
            request_id=str((rpc or {}).get("id", "")) or None,
            client_name=ua[:120] or None,
            tool_name=tool_name,
            parameters=arguments,
            workspace=workspace,
            permission_mode="gateway",
            duration_ms=max(0, duration_ms),
            success=success,
            error_type=error_type or None,
            extra={"device_id": device_id},
        )

    # ------------------------------------------------------ upstream sessions
    def _remember_upstream_session(
        self, client_session_id: str, upstream_key: str, upstream_session_id: str, initialize_body: bytes
    ) -> None:
        if not client_session_id or not upstream_key or not upstream_session_id:
            return
        with self._session_lock:
            self._upstream_sessions.setdefault(client_session_id, {})[upstream_key] = upstream_session_id
            if initialize_body:
                self._initialize_requests[client_session_id] = bytes(initialize_body)

    def _mapped_upstream_session(self, client_session_id: str, upstream_key: str) -> str:
        if not client_session_id or not upstream_key:
            return ""
        with self._session_lock:
            return self._upstream_sessions.get(client_session_id, {}).get(upstream_key, "")

    def _forget_upstream_session(self, client_session_id: str, upstream_key: str) -> None:
        if not client_session_id or not upstream_key:
            return
        with self._session_lock:
            mappings = self._upstream_sessions.get(client_session_id)
            if mappings is not None:
                mappings.pop(upstream_key, None)
                if not mappings:
                    self._upstream_sessions.pop(client_session_id, None)

    async def _ensure_upstream_session(
        self,
        *,
        client_session_id: str,
        upstream_key: str,
        upstream_target: str,
        authorization: str | None,
    ) -> str:
        existing = self._mapped_upstream_session(client_session_id, upstream_key)
        if existing:
            return existing
        with self._session_lock:
            initialize_body = self._initialize_requests.get(client_session_id, b"")
        if not initialize_body:
            # Compatibility fallback for clients/tests that do not expose a stable
            # initialize exchange to the Gateway. The target may still accept the
            # caller-provided session id.
            return client_session_id

        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if authorization:
            headers["authorization"] = f"Bearer {authorization}"
        try:
            response = await self._http.post(
                f"{upstream_target}/mcp", content=initialize_body, headers=headers
            )
            if response.status_code >= 400:
                return ""
            upstream_session_id = response.headers.get("mcp-session-id", "").strip()
            if not upstream_session_id:
                return ""
            initialized_headers = dict(headers)
            initialized_headers["mcp-session-id"] = upstream_session_id
            initialized = await self._http.post(
                f"{upstream_target}/mcp",
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                ).encode("utf-8"),
                headers=initialized_headers,
            )
            if initialized.status_code >= 400:
                return ""
        except httpx.HTTPError:
            return ""
        self._remember_upstream_session(
            client_session_id, upstream_key, upstream_session_id, initialize_body
        )
        return upstream_session_id

    # -------------------------------------------------------- workspace helpers
    def _credential_for_workspace(self, workspace_id: str, fallback: str | None) -> str | None:
        if workspace_id and self._workspace_credential_registry is not None:
            value = self._workspace_credential_registry(workspace_id)
            if value:
                return value
        return fallback

    def _workspace_for_credential(self, value: str) -> str:
        if not value or self._workspace_credential_registry is None:
            return ""
        try:
            from .config_store import load_projects

            for project in load_projects():
                if not project.id:
                    continue
                candidate = self._workspace_credential_registry(project.id)
                if candidate and _constant_time_eq(value, candidate):
                    return project.id
        except Exception:
            return ""
        return ""

    def _resolve_upstream(self, workspace_id: str) -> str | None:
        """Return the upstream target URL for a workspace, or None if not running."""
        if not workspace_id:
            return self.upstream_url
        if self._workspace_registry is None:
            return self.upstream_url
        info = self._workspace_registry(workspace_id)
        if info is None:
            return None
        port, _root = info
        return f"http://127.0.0.1:{port}"

    def _resolve_workspace_path(self, workspace_id: str) -> Path | None:
        """Return the root Path for a workspace, or None."""
        if not workspace_id or self._workspace_registry is None:
            return None
        info = self._workspace_registry(workspace_id)
        if info is None:
            return None
        _port, root = info
        return Path(root) if root else None

    @staticmethod
    def _extract_session_id(request: Request) -> str:
        """Extract mcp-session-id header from request."""
        sid = request.headers.get("mcp-session-id", "")
        return sid.strip()

    def _running_workspace_ids(self) -> list[str]:
        if self._workspace_registry is None:
            return []
        try:
            from .config_store import load_projects

            return [
                project.id
                for project in load_projects()
                if project.id and self._workspace_registry(project.id) is not None
            ]
        except Exception:
            return []

    def _entry_workspace_id(self, running: list[str]) -> str:
        if not self._workspace or not running:
            return ""
        try:
            target = self._workspace.expanduser().resolve()
            from .config_store import load_projects

            for project in load_projects():
                if project.id not in running:
                    continue
                try:
                    if Path(project.root_path).expanduser().resolve() == target:
                        return project.id
                except OSError:
                    if project.root_path.casefold() == str(self._workspace).casefold():
                        return project.id
        except Exception:
            return ""
        return ""

    def _effective_workspace(self, token_workspace: str, session_id: str, *, pinned: bool = False) -> str:
        """Resolve a workspace without binding OAuth identity to one project.

        Direct per-project Bearer credentials remain pinned for backward compatibility.
        OAuth sessions can switch freely. With no explicit selection, one running
        workspace is auto-selected; otherwise the Hub entry project is preferred.
        """
        if pinned and token_workspace:
            return token_workspace
        running = self._running_workspace_ids()
        with self._session_lock:
            selected = self._session_workspaces.get(session_id, "") if session_id else ""
            if selected and selected in running:
                return selected
            if selected and selected not in running and session_id:
                self._session_workspaces.pop(session_id, None)
        if token_workspace and (not running or token_workspace in running):
            chosen = token_workspace
        elif len(running) == 1:
            chosen = running[0]
        else:
            chosen = self._entry_workspace_id(running) or (running[0] if running else "")
        if chosen and session_id:
            with self._session_lock:
                self._session_workspaces[session_id] = chosen
        return chosen

    def _list_workspaces(self) -> str:
        """Build a text report of all registered projects with status."""
        lines = ["已注册的工作区：", ""]
        try:
            from .config_store import load_projects

            projects = load_projects()
            for i, p in enumerate(projects, 1):
                port_info = ""
                running = "（未运行）"
                if self._workspace_registry and p.id:
                    info = self._workspace_registry(p.id)
                    if info:
                        port_info = f" : {info[0]}"
                        running = "（运行中）"
                name = p.display_name or Path(p.root_path).name
                lines.append(f"{i}. id={p.id}  {name}  {p.root_path}{port_info} {running}")
            if not projects:
                lines.append("（无已注册项目）")
        except Exception:
            lines.append("（无法加载项目列表）")
        return "\n".join(lines)

    def _get_current_workspace(self, workspace_id: str, session_id: str) -> str:
        """Return the current session workspace, applying automatic selection."""
        effective = self._effective_workspace(workspace_id, session_id)
        if not effective:
            return "当前没有运行中的工作区。请先在 MCP DevBridge 桌面启动一个项目服务。"
        root = str(self._resolve_workspace_path(effective) or "未知")
        return f"当前工作区：id={effective}\n路径：{root}"

    def _do_switch_workspace(self, project_id: str, session_id: str) -> str:
        """Switch the current session's workspace binding."""
        # Verify the project exists
        try:
            from .config_store import load_projects

            projects = load_projects()
            match = next((p for p in projects if p.id == project_id), None)
            if match is None:
                raise ValueError(f"未找到项目：{project_id}。可用 devbridge_list_workspaces 查看。")
        except Exception:
            pass
        # Verify engine is running for this workspace
        if self._workspace_registry and not self._workspace_registry(project_id):
            raise ValueError(
                f"项目 {project_id} 的 CodexPro 引擎未运行。"
                "请先在 MCP DevBridge 桌面上启动该项目的服务。"
            )
        if session_id:
            with self._session_lock:
                self._session_workspaces[session_id] = project_id
        try:
            from .config_store import load_projects

            projects = load_projects()
            match = next((p for p in projects if p.id == project_id), None)
            name = (match.display_name or Path(match.root_path).name) if match else project_id
        except Exception:
            name = project_id
        return (
            f"已切换到工作区：{name}（{project_id}）\n"
            f"后续工具调用请携带 {_ROUTE_WORKSPACE_ARG}={project_id}；这不依赖底层 MCP transport session。"
        )

    # ----------------------------------------------------------- helper
    def _unauthorized(self) -> Response:
        return JSONResponse(
            {
                "error": "unauthorized",
                "message": "需要 OAuth access token 或有效的 Bearer 访问令牌。",
            },
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer realm="MCP DevBridge", resource_metadata="'
                    f'{self.public_base}/.well-known/oauth-protected-resource{constants.DEFAULT_MCP_PATH}"'
                ),
                "Date": formatdate(time.time(), usegmt=True),
            },
        )

    async def _health(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "app": "oauth-gateway", "resource": self.resource_url})

    @property
    def is_running(self) -> bool:
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and self._server is not None
            and not self._server.should_exit
        )

    # ---------------------------------------------------------- runtime
    def start(self, port: int = constants.GATEWAY_PORT) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=constants.GATEWAY_HOST,
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,  # PyInstaller 冻结环境：禁止 dictConfig 动态导入 uvicorn.logging 格式化器
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._agent_orchestrator is not None:
            self._agent_orchestrator.shutdown()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


__all__ = ["OAuthGateway"]
