"""ChatGPT OAuth support for the isolated MCP Live relay."""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import RegistrationError
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .constants import OAUTH_OFFLINE_SCOPE, OAUTH_SCOPE
from .oauth_provider import ConsentExpired, LocalOAuthProvider
from .secrets import SecretsStore

_METADATA_PATH = "/.well-known/oauth-protected-resource"
_LOG = logging.getLogger("uvicorn.error")
_CLIENT_INDEX = "McpLive/OAuthClientIndex"
_MAX_CLIENTS = 64
_CONSENT_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _chatgpt_callback(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com" or parsed.query or parsed.fragment:
        return False
    return parsed.path == "/connector_platform_oauth_redirect" or bool(
        re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]{1,128}", parsed.path)
    )


class _LiveProvider(LocalOAuthProvider):
    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirects = client_info.redirect_uris
        if not redirects or len(redirects) != 1 or not _chatgpt_callback(str(redirects[0])):
            raise RegistrationError("invalid_redirect_uri", "仅接受 ChatGPT OAuth 回调地址。")

        clients = json.loads(self.store.get(_CLIENT_INDEX) or "[]")
        if len(clients) >= _MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "已达到连接数量上限。")
        await super().register_client(client_info)
        self.store.set(_CLIENT_INDEX, json.dumps([*clients, client_info.client_id]))


class LiveOAuth:
    def __init__(self, origin: str, approval_token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", approval_token):
            raise ValueError("MCP Live OAuth 需要独立的随机批准凭据（32 至 128 位字符）。")
        self.approval_token = approval_token
        self.origin = origin.rstrip("/")
        self.resource_url = self.origin + "/mcp"
        self._provider = _LiveProvider(
            issuer_url=self.origin,
            resource_url=self.resource_url,
            store=SecretsStore(use_credential_manager=False),
            code_ttl=180,
        )

    @property
    def challenge(self) -> str:
        return f'Bearer resource_metadata="{self.origin}{_METADATA_PATH}/mcp", scope="{OAUTH_SCOPE}"'

    async def verify_access(self, token: str) -> bool:
        record = await self._provider.load_access_token(token)
        return bool(record and str(record.resource) == self.resource_url and OAUTH_SCOPE in record.scopes)

    def routes(self) -> list[Route]:
        issuer = AnyHttpUrl(self.origin)
        scopes = [OAUTH_SCOPE, OAUTH_OFFLINE_SCOPE]
        auth_routes = create_auth_routes(
            self._provider,
            issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=scopes, default_scopes=[OAUTH_SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        metadata_routes = create_protected_resource_routes(
            AnyHttpUrl(self.resource_url),
            [issuer],
            scopes_supported=scopes,
            resource_name="MCP Live",
        )
        return [
            *auth_routes,
            *metadata_routes,
            Route(_METADATA_PATH, metadata_routes[0].endpoint, methods=["GET", "OPTIONS"]),
            Route("/consent", self._consent_page, methods=["GET"]),
            Route("/consent", self._consent_submit, methods=["POST"]),
        ]

    async def _consent_page(self, request: Request) -> HTMLResponse:
        consent_id = request.query_params.get("id", "")
        if self._provider.get_consent(consent_id) is None:
            return HTMLResponse("授权请求已过期，请从 ChatGPT 重新连接。", 410, _CONSENT_HEADERS)

        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>授权 MCP Live</title><main><h1>授权 ChatGPT 使用 MCP Live</h1>'
            '<p>ChatGPT 将可以查看当前设备，并读取设备允许访问的目录中的文本。</p>'
            '<form method="post" action="/consent">'
            f'<input type="hidden" name="id" value="{html.escape(consent_id, quote=True)}">'
            '<label>批准凭据 <input type="password" name="credential" '
            'required autocomplete="off" maxlength="128"></label>'
            '<button type="submit">批准并返回 ChatGPT</button></form></main></html>',
            headers=_CONSENT_HEADERS,
        )

    async def _consent_submit(self, request: Request) -> Response:
        kind = request.headers.get("content-type", "").partition(";")[0]
        if kind != "application/x-www-form-urlencoded":
            return HTMLResponse("请求格式错误。", 415, _CONSENT_HEADERS)

        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 4096:
                return HTMLResponse("请求过长。", 413, _CONSENT_HEADERS)
            payload.extend(chunk)
        try:
            values = parse_qs(payload.decode("utf-8"), max_num_fields=2)
        except ValueError:
            return HTMLResponse("请求格式错误。", 400, _CONSENT_HEADERS)

        credential = values.get("credential", [""])[0]
        if not hmac.compare_digest(credential.encode(), self.approval_token.encode()):
            return HTMLResponse("批准凭据不匹配。请返回授权页重试。", 401, _CONSENT_HEADERS)

        try:
            redirect = self._provider.approve(values.get("id", [""])[0])
        except ConsentExpired:
            return HTMLResponse("授权请求已过期，请从 ChatGPT 重新连接。", 410, _CONSENT_HEADERS)
        _LOG.info("MCP Live OAuth consent approved, returning to ChatGPT")
        return RedirectResponse(redirect, status_code=303, headers=_CONSENT_HEADERS)
