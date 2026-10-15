"""Isolated transport-adapter tests; these do not substitute production routing tests."""
from __future__ import annotations

import base64
import time
from typing import Any, cast
from urllib.parse import quote, quote_plus, urlencode

import pytest
from mcp.server.auth.middleware.client_auth import AuthenticationError
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request

from local_dev_mcp_bridge.oauth_transport import StandardClientAuthenticator


def _request(fields: list[tuple[str, str]], headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    body = urlencode(fields).encode()
    incoming = [(b"content-type", b"application/x-www-form-urlencoded"), *(headers or [])]
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/token", "headers": incoming}, receive)


def _basic(identity: str, credential: str, encoder=quote, scheme="Basic") -> list[tuple[bytes, bytes]]:
    encoded = base64.b64encode((encoder(identity, safe="") + ":" + encoder(credential, safe="")).encode()).decode()
    return [(b"authorization", (scheme + " " + encoded).encode())]


def _auth(method: str, identity="adapter-fixture", credential="fixture-only", expires=None):
    registered = OAuthClientInformationFull(
        client_id=identity,
        client_secret=None if method == "none" else credential,
        client_secret_expires_at=expires,
        redirect_uris=[AnyUrl("https://example.test/callback")],
        token_endpoint_auth_method=method,
    )

    class Provider:
        async def get_client(self, candidate):
            return registered if candidate == identity else None

    provider = cast(OAuthAuthorizationServerProvider[Any, Any, Any], Provider())
    return StandardClientAuthenticator(provider), registered


@pytest.mark.asyncio
@pytest.mark.parametrize("encoder", [quote, quote_plus])
@pytest.mark.parametrize("identity,credential", [("adapter-fixture", "fixture-only"), ("client with space", "phrase with space"), ("client+plus", "phrase+plus"), ("client/é", "phrase&=:é")])
async def test_basic_only_decodes_form_components(encoder, identity, credential):
    authenticator, registered = _auth("client_secret_basic", identity, credential)
    request = _request([("grant_type", "authorization_code")], _basic(identity, credential, encoder))
    assert await authenticator.authenticate_request(request) is registered
    assert (await request.form())["client_id"] == identity


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["Basic", "basic", "BASIC", "bAsIc"])
async def test_basic_scheme_case_is_insensitive(scheme):
    authenticator, registered = _auth("client_secret_basic")
    request = _request([], _basic("adapter-fixture", "fixture-only", scheme=scheme))
    assert await authenticator.authenticate_request(request) is registered


@pytest.mark.asyncio
async def test_matching_body_identifier_is_compatible_but_conflicting_one_is_not():
    authenticator, registered = _auth("client_secret_basic")
    headers = _basic("adapter-fixture", "fixture-only")
    assert await authenticator.authenticate_request(_request([("client_id", "adapter-fixture")], headers)) is registered
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate_request(_request([("client_id", "other-client")], headers))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "fixture-only"])
async def test_body_credential_field_cannot_be_mixed_with_basic_even_when_empty(value):
    authenticator, _ = _auth("client_secret_basic")
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate_request(_request([("client_secret", value)], _basic("adapter-fixture", "fixture-only")))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["none", "client_secret_post"])
async def test_basic_header_requires_basic_registered_method(method):
    authenticator, _ = _auth(method)
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate_request(_request([("client_id", "adapter-fixture")], _basic("adapter-fixture", "fixture-only")))


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["client_id", "client_secret", "grant_type", "code", "code_verifier", "redirect_uri", "refresh_token", "resource", "token"])
async def test_duplicate_singleton_parameters_are_rejected(parameter):
    authenticator, _ = _auth("none")
    fields = [] if parameter == "client_id" else [("client_id", "adapter-fixture")]
    fields.extend([(parameter, "one"), (parameter, "one")])
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate_request(_request(fields))


@pytest.mark.asyncio
async def test_duplicate_authorization_headers_are_rejected():
    authenticator, _ = _auth("client_secret_basic")
    headers = _basic("adapter-fixture", "fixture-only")
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate_request(_request([], headers + headers))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"Basic ???", b"Basic Og==", b"Basic", b"Bearer fixture", b"Basic " + b"a" * 8193])
async def test_malformed_or_oversized_header_is_rejected(value):
    authenticator, _ = _auth("client_secret_basic")
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate_request(_request([], [(b"authorization", value)]))


@pytest.mark.asyncio
async def test_invalid_and_expired_credentials_remain_invalid():
    authenticator, _ = _auth("client_secret_basic")
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate_request(_request([], _basic("adapter-fixture", "wrong-value")))
    expired, _ = _auth("client_secret_basic", expires=int(time.time()) - 60)
    with pytest.raises(AuthenticationError):
        await expired.authenticate_request(_request([], _basic("adapter-fixture", "fixture-only")))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["none", "client_secret_post"])
async def test_existing_body_flows_are_preserved(method):
    authenticator, registered = _auth(method)
    fields = [("client_id", "adapter-fixture")]
    if method == "client_secret_post":
        fields.append(("client_secret", "fixture-only"))
    assert await authenticator.authenticate_request(_request(fields)) is registered
