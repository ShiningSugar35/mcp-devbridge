"""Centralized storage locations and default constants."""

from __future__ import annotations

import os
from pathlib import Path

APP_IDENT = "LocalDevMCPBridge"


def _base_config_dir() -> Path:
    if os.environ.get("LOCALDEV_MCP_CONFIG_DIR"):
        return Path(os.environ["LOCALDEV_MCP_CONFIG_DIR"]).resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_IDENT


def config_dir() -> Path:
    return _base_config_dir()


def config_file() -> Path:
    return _base_config_dir() / "config.json"


def projects_file() -> Path:
    return _base_config_dir() / "projects.json"


def rc_file() -> Path:
    return _base_config_dir() / "runtime.json"


def state_file() -> Path:
    return _base_config_dir() / "state.json"


def port_file() -> Path:
    return _base_config_dir() / "port.json"


def log_dir() -> Path:
    return _base_config_dir() / "logs"


def process_log_dir() -> Path:
    return _base_config_dir() / "process_logs"


def backup_dir() -> Path:
    return _base_config_dir() / "backups"

CONFIG_DIR = _base_config_dir()
LOG_DIR = CONFIG_DIR / "logs"
PROCESS_LOG_DIR = CONFIG_DIR / "process_logs"
BACKUP_DIR = CONFIG_DIR / "backups"

CONFIG_FILE = CONFIG_DIR / "config.json"
PROJECTS_FILE = CONFIG_DIR / "projects.json"
RC_FILE = CONFIG_DIR / "runtime.json"
STATE_FILE = CONFIG_DIR / "state.json"
PORT_FILE = CONFIG_DIR / "port.json"

DEFAULT_GATEWAY_PORT = 8786
DEFAULT_CODEXPRO_PORT = 8787
DEFAULT_WINDOWS_MCP_PORT = 28731
DEFAULT_LEGACY_BACKEND_PORT = 8765
# 兼容别名（新代码请使用上面 4 个 DEFAULT_*_PORT）
DEFAULT_LOCAL_PORT = DEFAULT_LEGACY_BACKEND_PORT
GATEWAY_PORT = DEFAULT_GATEWAY_PORT
DEFAULT_HEALTH_PATH = "/health"
DEFAULT_MCP_PATH = "/mcp"

MAX_FILE_BYTES = 1_000_000
MAX_TEXT_OUTPUT_CHARS = 60_000
MAX_SEARCH_RESULTS = 200
MAX_DIRECTORY_ENTRIES = 2_000
MAX_READ_FILES_COUNT = 20
MAX_READ_FILES_PER_FILE_BYTES = 64_000
MAX_READ_FILES_TOTAL_CHARS = 60_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
MAX_COMMAND_TIMEOUT_SECONDS = 7_200
DEFAULT_COMMAND_OUTPUT_CHARS = 60_000
MAX_PROCESS_LOG_BYTES = 4_000_000

RETENTION_DAYS = 14
MAX_JSONL_BYTES = 50_000_000

ACCESS_TOKEN_USERNAME = "LocalDevMCPBridge"
ACCESS_TOKEN_CRED_NAME = "LocalDevMCPBridge/AccessToken"
CLOUDFLARE_TOKEN_CRED_NAME = "LocalDevMCPBridge/CloudflareToken"

# OAuth (Phase 8): public entry gateway + single-user authorization server.
GATEWAY_HOST = "127.0.0.1"
OAUTH_SCOPE = "ACCESS_VIEW_MANAGE_MCP_CONTENT"
OAUTH_ACCESS_TOKEN_TTL_SECONDS = 3600  # short-lived per MCP spec recs
OAUTH_REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
OAUTH_AUTHORIZATION_CODE_TTL_SECONDS = 900
OAUTH_CONSENT_TTL_SECONDS = 600
OAUTH_CLIENT_CRED_PREFIX = "LocalDevMCPBridge/OAuthClient:"
OAUTH_REFRESH_CRED_PREFIX = "LocalDevMCPBridge/OAuthRefresh:"
OAUTH_STATIC_URI_LOOKUP_PREFIX = "LocalDevMCPBridge/OAuthStaticUriLookup:"

SENSITIVE_NAME_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "COOKIE",
    "AUTH",
)

AUTH_FAIL_LIMIT = 10
AUTH_FAIL_WINDOW_SECONDS = 300
AUTH_LOCKOUT_SECONDS = 300

TIMEOUT_ERROR = "TIMEOUT"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
PERMISSION_DENIED = "PERMISSION_DENIED"
PATH_ESCAPE = "PATH_ESCAPE"
PATH_NOT_FOUND = "PATH_NOT_FOUND"
AUTH_FAILED = "AUTH_FAILED"
CSC_AUTH_FAILED = "AUTH_FAILED"
RATE_LIMITED = "RATE_LIMITED"


def ensure_dirs() -> None:
    for d in (config_dir(), log_dir(), process_log_dir(), backup_dir()):
        d.mkdir(parents=True, exist_ok=True)