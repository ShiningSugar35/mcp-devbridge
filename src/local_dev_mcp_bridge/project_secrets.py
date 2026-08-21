"""Per-project internal engine credentials and transport credentials.

Client authentication belongs to the shared Hub and is stored separately in
the global access credential.  Project access values are internal upstream
credentials only; they are never promoted to or inherited from the Hub bearer.
"""

from __future__ import annotations

from typing import Any

from . import APP_IDENT
from .secrets import SecretsStore, generate_token

PROJECT_ACCESS_TOKEN_PREFIX = f"{APP_IDENT}/ProjectAccessToken/"
PROJECT_TUNNEL_TOKEN_PREFIX = f"{APP_IDENT}/ProjectCloudflareTunnelToken/"
LEGACY_TUNNEL_TOKEN_CRED_NAME = f"{APP_IDENT}/CloudflareTunnelToken"


def _key(prefix: str, project_id: str) -> str:
    project_id = project_id.strip()
    if not project_id:
        raise ValueError("project_id 不能为空。")
    return f"{prefix}{project_id}"


def get_project_access_token(project_id: str, *, store: Any | None = None) -> str | None:
    """Return one project's private upstream bearer without Hub migration."""
    secrets = store or SecretsStore()
    return secrets.get(_key(PROJECT_ACCESS_TOKEN_PREFIX, project_id))


def ensure_project_access_token(project_id: str, *, store: Any | None = None) -> str:
    """Return or create one project's private upstream bearer."""
    secrets = store or SecretsStore()
    value = get_project_access_token(project_id, store=secrets)
    if value:
        return value
    value = generate_token(256)
    secrets.set(_key(PROJECT_ACCESS_TOKEN_PREFIX, project_id), value)
    return value


def get_project_tunnel_token(
    project_id: str,
    *,
    store: Any | None = None,
    migrate_legacy: bool = True,
) -> str | None:
    """Return a project's Cloudflare credential, preserving old tunnel migration."""
    secrets = store or SecretsStore()
    key = _key(PROJECT_TUNNEL_TOKEN_PREFIX, project_id)
    value = secrets.get(key)
    if value:
        return value
    if migrate_legacy:
        legacy = secrets.get(LEGACY_TUNNEL_TOKEN_CRED_NAME)
        if legacy:
            secrets.set(key, legacy)
            return legacy
    return None


def remember_project_tunnel_token(
    project_id: str,
    value: str,
    *,
    store: Any | None = None,
) -> None:
    value = value.strip()
    if not value:
        return
    secrets = store or SecretsStore()
    secrets.set(_key(PROJECT_TUNNEL_TOKEN_PREFIX, project_id), value)


def clear_project_tunnel_token(project_id: str, *, store: Any | None = None) -> None:
    """Delete one project's persisted Cloudflare credential."""
    secrets = store or SecretsStore()
    secrets.delete(_key(PROJECT_TUNNEL_TOKEN_PREFIX, project_id))


__all__ = [
    "PROJECT_ACCESS_TOKEN_PREFIX",
    "PROJECT_TUNNEL_TOKEN_PREFIX",
    "get_project_access_token",
    "ensure_project_access_token",
    "get_project_tunnel_token",
    "remember_project_tunnel_token",
    "clear_project_tunnel_token",
]