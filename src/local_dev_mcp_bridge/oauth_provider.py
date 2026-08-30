"""Single-user OAuth provider implementing standard MCP OAuth (SDK 2.x).

Implements :class:`OAuthAuthorizationServerProvider` on top of the existing
secrets infra (Windows Credential Manager / DPAPI file) and short-lived
in-memory records. One local identity ("local-user"); no user registry.

SDK layers handle: discovery metadata, /authorize + /token + /register +
/revoke HTTP endpoints, PKCE S256 verification, redirect-uri consistency.
This module owns: consented client registrations (encrypted), single-use
short-TTL authorization codes, opaque hashed access tokens, rotating
refresh tokens (hashed, encrypted at rest), and strict RFC 8707 resource
binding. Only the Gemini-compatible scope
``ACCESS_VIEW_MANAGE_MCP_CONTENT`` is ever accepted.

No secret is ever logged or stored in plaintext.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import constants
from .secrets import SecretsStore


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _random_token() -> str:
    return _b64url(secrets.token_bytes(32))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redirect_uri(redirect_uri: str | None, **query: str | None) -> str:
    uri = redirect_uri or ""
    pairs = {k: v for k, v in query.items() if v is not None}
    sep = "&" if "?" in uri else "?"
    return f"{uri}{sep}{urlencode(pairs)}"


def _workspace_from_subject(subject: str | None) -> str:
    """Extract workspace_id from token subject ('local-user:{id}' → '{id}', 'local-user' → '')."""
    if subject and subject.startswith("local-user:"):
        return subject.split(":", 1)[1]
    return ""


class LocalOAuthProvider(OAuthAuthorizationServerProvider):
    """Minimal single-user OAuth 2.1 authorization server for the bridge."""

    def __init__(
        self,
        *,
        issuer_url: str,
        resource_url: str,
        workspace: str = "",
        store: SecretsStore | None = None,
        now: Callable[[], float] | None = None,
        access_ttl: int = constants.OAUTH_ACCESS_TOKEN_TTL_SECONDS,
        refresh_ttl: int = constants.OAUTH_REFRESH_TOKEN_TTL_SECONDS,
        code_ttl: int = constants.OAUTH_AUTHORIZATION_CODE_TTL_SECONDS,
        consent_ttl: int = constants.OAUTH_CONSENT_TTL_SECONDS,
        max_access_entries: int = 256,
        max_ephemeral_entries: int = 256,
    ) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url.rstrip("/")
        self.workspace = workspace
        self.store = store or SecretsStore()
        self._now = now or time.time
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self.code_ttl = code_ttl
        self.consent_ttl = consent_ttl
        self.max_access_entries = max(1, int(max_access_entries))
        self.max_ephemeral_entries = max(1, int(max_ephemeral_entries))
        self._codes: dict[str, AuthorizationCode] = {}
        self._consents: dict[str, dict[str, Any]] = {}
        self._access_tokens: dict[str, AccessToken] = {}

    def _prune_ephemeral(self) -> None:
        """Expire and bound short-lived authorization/consent state."""
        now = float(self._now())
        for consent_id, record in list(self._consents.items()):
            if now > float(record.get("expires_at") or 0):
                self._consents.pop(consent_id, None)
        for code_value, record in list(self._codes.items()):
            if now > float(getattr(record, "expires_at", 0) or 0):
                self._codes.pop(code_value, None)
        if len(self._consents) > self.max_ephemeral_entries:
            ordered = sorted(self._consents.items(), key=lambda item: float(item[1].get("expires_at") or 0))
            for consent_id, _ in ordered[: len(ordered) - self.max_ephemeral_entries]:
                self._consents.pop(consent_id, None)
        if len(self._codes) > self.max_ephemeral_entries:
            ordered_codes = sorted(self._codes.items(), key=lambda item: float(getattr(item[1], "expires_at", 0) or 0))
            for code_value, _ in ordered_codes[: len(ordered_codes) - self.max_ephemeral_entries]:
                self._codes.pop(code_value, None)

    # ------------------------------------------------------------ scopes
    @staticmethod
    def _check_scope(
        scopes: list[str] | None, error_factory: Callable[..., Exception]
    ) -> list[str]:
        result = list(scopes) if scopes is not None else [constants.OAUTH_SCOPE]
        allowed = {constants.OAUTH_SCOPE, constants.OAUTH_OFFLINE_SCOPE}
        requested = set(result)
        if constants.OAUTH_SCOPE not in requested or not requested.issubset(allowed):
            raise error_factory(
                error="invalid_scope",
                error_description=(
                    f"必须包含 scope：{constants.OAUTH_SCOPE}；"
                    f"可选：{constants.OAUTH_OFFLINE_SCOPE}"
                ),
            )
        return list(dict.fromkeys(result))

    # -------------------------------------------------------------- DCR
    @staticmethod
    def _client_key(client_id: str) -> str:
        return f"{constants.OAUTH_CLIENT_CRED_PREFIX}{_hash(client_id)}"

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.get(self._client_key(client_id))
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(json.loads(raw))
        except Exception:
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_client_metadata", "redirect_uris 不能为空")
        for uri in client_info.redirect_uris:
            value = str(uri)
            if value.startswith("http://") and not value.startswith("http://localhost"):
                raise RegistrationError("invalid_client_metadata", "仅允许 https 或 http://localhost 回调地址")
        payload = client_info.model_dump(exclude_none=True, mode="json")
        self.store.set(self._client_key(client_info.client_id), json.dumps(payload))

    # ---------------------------------------------------------- consent
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        requested_scopes = self._check_scope(params.scopes, AuthorizeError)
        if params.resource and params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError(
                error="invalid_target",
                error_description="resource 必须指向当前 MCP 服务器",
            )
        consent_id = _random_token()
        self._prune_ephemeral()
        self._consents[consent_id] = {
            "client_id": client.client_id,
            "state": params.state,
            "scopes": requested_scopes,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri) if params.redirect_uri else None,
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "expires_at": float(self._now()) + self.consent_ttl,
        }
        self._prune_ephemeral()
        return f"/consent?id={consent_id}"

    def bind_workspace(self, consent_id: str, workspace_id: str) -> None:
        """Store the user's workspace selection into the consent record."""
        record = self._consents.get(consent_id)
        if record is not None:
            record["workspace_id"] = workspace_id

    def get_consent(self, consent_id: str) -> dict[str, Any] | None:
        self._prune_ephemeral()
        record = self._consents.get(consent_id)
        if record is None:
            return None
        if self._now() > record["expires_at"]:
            self._consents.pop(consent_id, None)
            return None
        return record

    def approve(self, consent_id: str) -> str:
        """User allows access: mint a code and build the client redirect."""
        record = self.get_consent(consent_id)
        if record is None:
            raise ConsentExpired()
        self._consents.pop(consent_id, None)
        workspace_id = record.get("workspace_id", "")
        subject = f"local-user:{workspace_id}" if workspace_id else "local-user"
        code = AuthorizationCode(
            code=_random_token(),
            scopes=record["scopes"],
            expires_at=int(float(self._now()) + self.code_ttl),
            client_id=record["client_id"],
            code_challenge=record["code_challenge"],
            redirect_uri=record["redirect_uri"],
            redirect_uri_provided_explicitly=record["redirect_uri_provided_explicitly"],
            resource=record["resource"],
            subject=subject,
        )
        self._codes[code.code] = code
        self._prune_ephemeral()
        return _redirect_uri(record["redirect_uri"], code=code.code, state=record.get("state"))

    def deny(self, consent_id: str) -> str:
        record = self.get_consent(consent_id)
        if record is None:
            raise ConsentExpired()
        self._consents.pop(consent_id, None)
        return _redirect_uri(
            record["redirect_uri"],
            error="access_denied",
            error_description="用户在授权窗口点击了“取消”。",
            state=record.get("state"),
        )

    # ------------------------------------------------------- authz code
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._prune_ephemeral()
        auth_code = self._codes.pop(authorization_code, None)  # single-use: removed on read
        if auth_code is None or self._now() > auth_code.expires_at:
            return None
        return auth_code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._check_scope(authorization_code.scopes, TokenError)
        workspace_id = _workspace_from_subject(authorization_code.subject)
        return self._issue_tokens(authorization_code.client_id, authorization_code.scopes, workspace_id)

    # ------------------------------------------------------- refresh
    @staticmethod
    def _refresh_key(refresh: str) -> str:
        return f"{constants.OAUTH_REFRESH_CRED_PREFIX}{_hash(refresh)}"

    @staticmethod
    def _access_key(access: str) -> str:
        return f"{constants.OAUTH_REFRESH_CRED_PREFIX}Access:{_hash(access)}"

    @staticmethod
    def _access_index_key() -> str:
        return f"{constants.OAUTH_REFRESH_CRED_PREFIX}AccessIndex"

    def _persist_access_record(self, access: str, record: AccessToken) -> None:
        key = self._access_key(access)
        payload = {
            "client_id": record.client_id,
            "scopes": list(record.scopes),
            "expires_at": record.expires_at,
            "resource": record.resource,
            "subject": record.subject,
        }
        self.store.set(key, json.dumps(payload, separators=(",", ":")))
        index_key = self._access_index_key()
        try:
            raw_index = self.store.get(index_key)
            index = json.loads(raw_index) if raw_index else []
            if not isinstance(index, list):
                index = []
        except Exception:
            index = []
        now = float(self._now())
        live: list[dict[str, Any]] = []
        for item in index:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get("key") or "")
            expires_at = item.get("expires_at")
            if not item_key or item_key == key:
                continue
            if expires_at is not None and now > float(expires_at):
                self.store.delete(item_key)
                if "Access:" in item_key:
                    self._access_tokens.pop(item_key.rsplit("Access:", 1)[-1], None)
                continue
            live.append({"key": item_key, "expires_at": expires_at})
        live.append({"key": key, "expires_at": record.expires_at})
        while len(live) > self.max_access_entries:
            evicted = live.pop(0)
            evicted_key = str(evicted.get("key") or "")
            if evicted_key:
                self.store.delete(evicted_key)
                if "Access:" in evicted_key:
                    self._access_tokens.pop(evicted_key.rsplit("Access:", 1)[-1], None)
        self.store.set(index_key, json.dumps(live, separators=(",", ":")))

    def _delete_access_record(self, access: str) -> None:
        key = self._access_key(access)
        self._access_tokens.pop(_hash(access), None)
        self.store.delete(key)
        index_key = self._access_index_key()
        try:
            raw_index = self.store.get(index_key)
            index = json.loads(raw_index) if raw_index else []
            if not isinstance(index, list):
                index = []
        except Exception:
            index = []
        live = [
            item
            for item in index
            if isinstance(item, dict) and str(item.get("key") or "") != key
        ]
        self.store.set(index_key, json.dumps(live, separators=(",", ":")))

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        raw = self.store.get(self._refresh_key(refresh_token))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if data.get("client_id") != client.client_id:
            return None
        if data.get("expires_at") is not None and self._now() > data["expires_at"]:
            self.store.delete(self._refresh_key(refresh_token))
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=list(data.get("scopes", [constants.OAUTH_SCOPE])),
            expires_at=data.get("expires_at"),
            subject=data.get("workspace_id", ""),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        requested = self._check_scope(scopes, TokenError)
        self.store.delete(self._refresh_key(refresh_token.token))  # rotate
        workspace_id = refresh_token.subject or ""
        return self._issue_tokens(client.client_id, requested, workspace_id)

    # ----------------------------------------------------------- access
    async def load_access_token(self, token: str) -> AccessToken | None:
        # Freshly issued OAuth access records are memory-only fast-path reads. After
        # Gateway recreation the encrypted/native store lookup can block, so perform
        # only that cache-miss path off the uvicorn event loop.
        if _hash(token) in self._access_tokens:
            return self._load_access_token_impl(token)
        return await asyncio.to_thread(self._load_access_token_impl, token)

    def load_access_token_sync(self, token: str) -> AccessToken | None:
        """Synchronous alias for tests."""
        return self._load_access_token_impl(token)

    def _load_access_token_impl(self, token: str) -> AccessToken | None:
        key_hash = _hash(token)
        record = self._access_tokens.get(key_hash)
        if record is None:
            raw = self.store.get(self._access_key(token))
            if raw:
                try:
                    data = json.loads(raw)
                    record = AccessToken(
                        token=token,
                        client_id=str(data["client_id"]),
                        scopes=list(data.get("scopes") or [constants.OAUTH_SCOPE]),
                        expires_at=data.get("expires_at"),
                        resource=data.get("resource"),
                        subject=data.get("subject"),
                    )
                    self._access_tokens[key_hash] = record
                except Exception:
                    self.store.delete(self._access_key(token))
                    return None
        if record is None:
            return None
        if record.expires_at is not None and self._now() > record.expires_at:
            self._delete_access_record(token)
            return None
        if record.resource and str(record.resource).rstrip("/") != self.resource_url:
            return None
        return record

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._delete_access_record(token.token)
        self.store.delete(self._refresh_key(token.token))

    # ---------------------------------------------------------- helpers
    def _issue_tokens(self, client_id: str, scopes: list[str], workspace_id: str = "") -> OAuthToken:
        access = _random_token()
        refresh = _random_token()
        now = float(self._now())
        subject = f"local-user:{workspace_id}" if workspace_id else "local-user"
        access_record = AccessToken(
            token=access,
            client_id=client_id,
            scopes=list(scopes),
            expires_at=int(now + self.access_ttl),
            resource=self.resource_url,
            subject=subject,
        )
        self._access_tokens[_hash(access)] = access_record
        self._persist_access_record(access, access_record)
        self.store.set(
            self._refresh_key(refresh),
            json.dumps({
                "client_id": client_id,
                "scopes": list(scopes),
                "expires_at": int(now + self.refresh_ttl),
                "workspace_id": workspace_id,
            }),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )


class ConsentExpired(RuntimeError):
    """The consent record was missing or stale."""


# ---------------------------------------------------------------------------
# Gemini static confidential client
#
# Gemini Spark's "Custom Connected App" accepts pre-registered OAuth
# credentials (client_id + client_secret, token_endpoint_auth_method =
# client_secret_post). DCR stays untouched; this is only a fallback path.
# The client record is encrypted via SecretsStore like any DCR client; the
# redirect-URI lookup key lets the same URI reuse the same client_id.

def get_or_create_gemini_client(
    redirect_uri: str,
    *,
    store: SecretsStore | None = None,
    rotate_secret: bool = False,
) -> tuple[str, str]:
    """Get (create on first call) the static "Gemini Spark" client.

    ``rotate_secret=True`` keeps the same client_id but issues a new secret,
    making the previous one immediately invalid.
    """
    uri = (redirect_uri or "").strip()
    if not uri.startswith("https://") and not uri.startswith("http://localhost"):
        raise ValueError("Gemini Redirect URI 必须以 https:// 开头（或 http://localhost）")
    store = store or SecretsStore()
    lookup_key = f"{constants.OAUTH_STATIC_URI_LOOKUP_PREFIX}{_hash(uri)}"

    client_id: str | None = None
    raw_lookup = store.get(lookup_key)
    if raw_lookup:
        try:
            client_id = json.loads(raw_lookup).get("client_id")
        except Exception:
            client_id = None

    payload: dict[str, Any]
    record_key: str | None = None
    if client_id:
        raw = store.get(LocalOAuthProvider._client_key(client_id))
        if raw:
            try:
                payload = json.loads(raw)
                record_key = LocalOAuthProvider._client_key(client_id)
            except Exception:
                payload = {}
                record_key = None
        else:
            payload = {}
    else:
        client_id = f"gemini-{_random_token()}"
        record_key = LocalOAuthProvider._client_key(client_id)
        payload = {
            "client_id": client_id,
            "client_name": "Gemini Spark",
            "redirect_uris": [uri],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": constants.OAUTH_SCOPE,
        }

    if rotate_secret or not payload.get("client_secret"):
        payload["client_secret"] = _random_token()
        payload["client_id"] = client_id
        if record_key is None:
            record_key = LocalOAuthProvider._client_key(client_id)
    assert record_key is not None
    store.set(record_key, json.dumps(payload, ensure_ascii=False))
    store.set(lookup_key, json.dumps({"client_id": client_id}))

    # sanity: must round-trip through the same model get_client() uses
    OAuthClientInformationFull.model_validate(payload)
    return client_id, payload["client_secret"]


__all__ = ["LocalOAuthProvider", "ConsentExpired", "_random_token", "_hash", "_workspace_from_subject"]