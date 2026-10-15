"""Request-local compatibility for the pinned SDK's OAuth authentication.

The SDK still validates the registered credential, lifetime and authorization
method. This adapter normalizes the Basic identity and rejects ambiguous input
before either token exchange or revocation can have side effects.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote, unquote_plus

from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.routes import cors_middleware
from mcp.shared.auth import OAuthClientInformationFull
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.routing import Route

_SINGLETON_FIELDS = (
    "client_id", "client_secret", "grant_type", "code", "code_verifier",
    "redirect_uri", "refresh_token", "resource", "scope", "token", "token_type_hint",
)


class StandardClientAuthenticator(ClientAuthenticator):
    """Normalize one request without mutating client registration or grant state."""

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        headers = request.headers.getlist("authorization")
        if len(headers) > 1:
            raise AuthenticationError("Ambiguous client authentication")
        form = await request.form()
        for key in _SINGLETON_FIELDS:
            if len(form.getlist(key)) > 1:
                raise AuthenticationError("Duplicate OAuth parameter")
        if headers:
            scheme, separator, encoded = headers[0].partition(" ")
            if scheme.lower() != "basic" or not separator or not encoded or len(encoded) > 8192:
                raise AuthenticationError("Unsupported client authentication")
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                raw_identity, separator, raw_value = decoded.partition(":")
                if not separator or not raw_identity:
                    raise ValueError("Malformed Basic credentials")
                # RFC 6749 2.3.1 applies form encoding to EACH component before
                # joining them. A form-encoded plus denotes a space, not a plus.
                identity = unquote_plus(raw_identity, errors="strict")
                value = unquote_plus(raw_value, errors="strict")
                normalized = base64.b64encode(
                    (quote(identity, safe="") + ":" + quote(value, safe="")).encode("utf-8")
                ).decode("ascii")
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise AuthenticationError("Malformed Basic authentication") from exc
            supplied = form.get("client_id")
            if supplied is not None and supplied != identity:
                raise AuthenticationError("Conflicting client identity")
            if "client_secret" in form:
                raise AuthenticationError("Multiple client authentication methods")
            # The pinned SDK expects canonical Basic spelling and percent-decodes
            # with unquote. Percent-encode spaces here so its validation is exact.
            raw_headers = [(key, item) for key, item in request.scope["headers"]
                           if key.lower() != b"authorization"]
            raw_headers.append((b"authorization", ("Basic " + normalized).encode("ascii")))
            request.scope["headers"] = raw_headers
            if hasattr(request, "_headers"):
                delattr(request, "_headers")
            if supplied is None:
                request._form = FormData([*form.multi_items(), ("client_id", identity)])
        registered = await super().authenticate_request(request)
        if headers and registered.token_endpoint_auth_method != "client_secret_basic":
            raise AuthenticationError("Authentication method does not match registration")
        return registered


def client_auth_routes(provider: OAuthAuthorizationServerProvider[Any, Any, Any]) -> list[Route]:
    authenticator = StandardClientAuthenticator(provider)
    return [
        Route(
            "/token", methods=["POST", "OPTIONS"],
            endpoint=cors_middleware(TokenHandler(provider, authenticator).handle, ["POST", "OPTIONS"]),
        ),
        Route(
            "/revoke", methods=["POST", "OPTIONS"],
            endpoint=cors_middleware(RevocationHandler(provider, authenticator).handle, ["POST", "OPTIONS"]),
        ),
    ]
