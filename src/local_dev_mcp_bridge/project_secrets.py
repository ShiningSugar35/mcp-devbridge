"""Per-project encrypted secrets with backward-compatible migration.

Desktop multi-project mode must not share mutable credentials implicitly.  Each
project therefore owns its bearer access token and Cloudflare tunnel token in
``SecretsStore``.  Existing installations historically stored one global token;
on first access we copy that value into the project-scoped slot so upgrades do
not invalidate an already configured ChatGPT/Gemini connection.
"""

from __future__ import annotations

from typing import Any

from . import APP_IDENT, constants
from .secrets import SecretsStore, generate_token

PROJECT_ACCESS_TOKEN_PREFIX = f"{APP_IDENT}/ProjectAccessToken/"
PROJECT_TUNNEL_TOKEN_PREFIX = f"{APP_IDENT}/ProjectCloudflareTunnelToken/"
GLOBAL_TUNNEL_TOKEN_CRED_NAME = f"{APP_IDENT}/GlobalCloudflareTunnelToken"
LEGACY_TUNNEL_TOKEN_CRED_NAME = f"{APP_IDENT}/CloudflareTunnelToken"
LEGACY_ACCESS_OWNER_CRED_NAME = "LocalDevMCPBridge/LegacyAccessMigrationOwner"


def _key(prefix: str, project_id: str) -> str:
    project_id = project_id.strip()
    if not project_id:
        raise ValueError("project_id 不能为空。")
    return f"{prefix}{project_id}"


def get_project_access_token(
    project_id: str,
    *,
    store: Any | None = None,
    migrate_legacy: bool = True,
) -> str | None:
    """Return a project's bearer token, copying the legacy global value once."""
    secrets = store or SecretsStore()
    key = _key(PROJECT_ACCESS_TOKEN_PREFIX, project_id)
    value = secrets.get(key)
    if value:
        return value
    if migrate_legacy:
        legacy = secrets.get(constants.ACCESS_TOKEN_CRED_NAME)
        owner = secrets.get(LEGACY_ACCESS_OWNER_CRED_NAME)
        if legacy and (not owner or owner == project_id):
            if not owner:
                secrets.set(LEGACY_ACCESS_OWNER_CRED_NAME, project_id)
            secrets.set(key, legacy)
            return legacy
    return None


def ensure_project_access_token(project_id: str, *, store: Any | None = None) -> str:
    secrets = store or SecretsStore()
    value = get_project_access_token(project_id, store=secrets)
    if value:
        return value
    value = generate_token(256)
    secrets.set(_key(PROJECT_ACCESS_TOKEN_PREFIX, project_id), value)
    return value


def activate_project_access_token(project_id: str, *, store: Any | None = None) -> str:
    """Mirror the active project's token to the legacy slot used by old components.

    This keeps the running gateway and older clients compatible while the source
    of truth remains project-scoped.  It never rotates either value.
    """
    secrets = store or SecretsStore()
    value = ensure_project_access_token(project_id, store=secrets)
    secrets.set(constants.ACCESS_TOKEN_CRED_NAME, value)
    return value


def regenerate_project_access_token(project_id: str, *, store: Any | None = None) -> str:
    """Rotate only one project's bearer token."""
    secrets = store or SecretsStore()
    value = generate_token(256)
    secrets.set(_key(PROJECT_ACCESS_TOKEN_PREFIX, project_id), value)
    return value


def get_global_tunnel_token(*, store: Any | None = None) -> str | None:
    """Return the device-level Cloudflare token, migrating the historical global slot once."""
    secrets = store or SecretsStore()
    value = secrets.get(GLOBAL_TUNNEL_TOKEN_CRED_NAME)
    if value:
        return value
    legacy = secrets.get(LEGACY_TUNNEL_TOKEN_CRED_NAME)
    if legacy:
        secrets.set(GLOBAL_TUNNEL_TOKEN_CRED_NAME, legacy)
        return legacy
    return None


def remember_global_tunnel_token(token: str, *, store: Any | None = None) -> None:
    """Persist the device-level Cloudflare token in encrypted storage."""
    token = token.strip()
    if not token:
        return
    secrets = store or SecretsStore()
    secrets.set(GLOBAL_TUNNEL_TOKEN_CRED_NAME, token)


def clear_global_tunnel_token(*, store: Any | None = None) -> None:
    secrets = store or SecretsStore()
    secrets.delete(GLOBAL_TUNNEL_TOKEN_CRED_NAME)


def get_project_tunnel_token(
    project_id: str,
    *,
    store: Any | None = None,
    migrate_legacy: bool = True,
) -> str | None:
    """Return a project's Cloudflare token, preserving the old shared value."""
    secrets = store or SecretsStore()
    key = _key(PROJECT_TUNNEL_TOKEN_PREFIX, project_id)
    value = secrets.get(key)
    if value:
        return value
    if migrate_legacy:
        shared = get_global_tunnel_token(store=secrets)
        if shared:
            secrets.set(key, shared)
            return shared
    return None


def remember_project_tunnel_token(
    project_id: str,
    token: str,
    *,
    store: Any | None = None,
) -> None:
    token = token.strip()
    if not token:
        return
    secrets = store or SecretsStore()
    secrets.set(_key(PROJECT_TUNNEL_TOKEN_PREFIX, project_id), token)


def clear_project_tunnel_token(project_id: str, *, store: Any | None = None) -> None:
    """Delete one project's persisted Cloudflare Tunnel token."""
    secrets = store or SecretsStore()
    secrets.delete(_key(PROJECT_TUNNEL_TOKEN_PREFIX, project_id))


def load_project_ui_secrets(project_id: str, *, store: Any | None = None) -> tuple[str, str]:
    """Return (access, tunnel) values for one project, applying legacy migration."""
    secrets = store or SecretsStore()
    return (
        get_project_access_token(project_id, store=secrets) or "",
        get_project_tunnel_token(project_id, store=secrets) or "",
    )


__all__ = [
    "PROJECT_ACCESS_TOKEN_PREFIX",
    "PROJECT_TUNNEL_TOKEN_PREFIX",
    "GLOBAL_TUNNEL_TOKEN_CRED_NAME",
    "get_project_access_token",
    "ensure_project_access_token",
    "activate_project_access_token",
    "regenerate_project_access_token",
    "get_global_tunnel_token",
    "remember_global_tunnel_token",
    "clear_global_tunnel_token",
    "get_project_tunnel_token",
    "remember_project_tunnel_token",
    "clear_project_tunnel_token",
    "load_project_ui_secrets",
]
