"""Small transport adaptation for the pinned MCP SDK's OAuth client authentication.

The SDK validates secrets, the registered method, PKCE and grant lifetime.  This
module supplies the RFC 6749 Basic client identity when it is absent from the
form, and rejects ambiguous duplicate identities before grant processing.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import unquote

from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.routes import cors_middleware
from mcp.shared.auth import OAuthClientInformationFull
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.routing import Route


class StandardClientAuthenticator(ClientAuthenticator):
    """Request-local normalization; never changes stored registration or policy."""

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        headers = request.headers.getlist("authorization")
        if len(headers) > 1:
            raise AuthenticationError("Ambiguous client authentication")
        form = await request.form()
        for key in ("client_id", "client_secret", "grant_type", "code", "code_verifier", "redirect_uri", "refresh_token", "resource"):
            if len(form.getlist(key)) > 1:
                raise AuthenticationError("Duplicate OAuth parameter")
        if headers:
            header = headers[0]
            scheme, separator, encoded = header.partition(" ")
            if scheme.lower() != "basic" or not separator or not encoded or len(encoded) > 8192:
                raise AuthenticationError("Unsupported client authentication")
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                raw_identity, separator, _value = decoded.partition(":")
                if not separator or not raw_identity:
                    raise ValueError("Malformed Basic credentials")
                identity = unquote(raw_identity)
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise AuthenticationError("Malformed Basic authentication") from exc
            supplied = form.get("client_id")
            if supplied is not None and supplied != identity:
                raise AuthenticationError("Conflicting client identity")
            if form.get("client_secret"):
                raise AuthenticationError("Multiple client authentication methods")
            if scheme != "Basic":
                # The pinned SDK expects the canonical scheme spelling.
                raw_headers = [(k, v) for k, v in request.scope["headers"] if k.lower() != b"authorization"]
                raw_headers.append((b"authorization", ("Basic " + encoded).encode("ascii")))
                request.scope["headers"] = raw_headers
                if hasattr(request, "_headers"):
                    delattr(request, "_headers")
            if supplied is None:
                # Both SDK handlers read Request.form() after authentication, so
                # keep the normalized FormData on this request, not global state.
                request._form = FormData([*form.multi_items(), ("client_id", identity)])
        return await super().authenticate_request(request)


def client_auth_routes(provider: OAuthAuthorizationServerProvider[Any, Any, Any]) -> list[Route]:
    authenticator = StandardClientAuthenticator(provider)
    return [
        Route("/token", endpoint=cors_middleware(TokenHandler(provider, authenticator).handle, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
        Route("/revoke", endpoint=cors_middleware(RevocationHandler(provider, authenticator).handle, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
    ]
