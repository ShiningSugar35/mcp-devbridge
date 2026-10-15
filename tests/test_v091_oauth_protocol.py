"""Account-linking regressions; memory-only credentials, no real Google account."""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

import local_dev_mcp_bridge.gateway as gm
from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.oauth_provider import LocalOAuthProvider, _redirect_uri
from local_dev_mcp_bridge.secrets import SecretsStore

ORIGIN = "https://mcp.example.test"
CALLBACK = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-test?existing=a%2Bb"
VERIFIER = "v091-memory-only-pkce-verifier-012345678901234567890"


class MemoryStore:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


@pytest.fixture
def oauth_client(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "_LOG_DIR", tmp_path / "logs")
    provider = LocalOAuthProvider(
        issuer_url=ORIGIN, resource_url=ORIGIN + "/mcp",
        store=cast(SecretsStore, MemoryStore()),
    )
    legacy = secrets.token_urlsafe(32)
    gateway = gm.OAuthGateway(
        public_hostname=ORIGIN, provider=provider,
        upstream_legacy_token=lambda: legacy,
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"ok": True})),
    )
    with TestClient(gateway.app, follow_redirects=False) as client:
        yield client, legacy


STATES = ["plain", "a+b/c==", "a b&c=d?e#f", "%2B%20%26", "中文/é/🙂", "", "A" * 2048]


@pytest.mark.parametrize("state", STATES)
def test_redirect_state_roundtrip_preserves_original_query(state):
    url = _redirect_uri(CALLBACK, code="temporary-code", state=state)
    parsed = urlsplit(url)
    values = parse_qs(parsed.query, keep_blank_values=True)
    assert values["state"] == [state]
    assert values["existing"] == ["a+b"]
    assert values["code"] == ["temporary-code"]
    assert parsed.fragment == ""


def _grant(client, method, state="opaque-state"):
    registration = client.post("/register", json={
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": " ".join(constants.OAUTH_SUPPORTED_SCOPES),
    })
    assert registration.status_code == 201
    info = registration.json()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
    response = client.get("/authorize", params={
        "client_id": info["client_id"], "response_type": "code", "redirect_uri": CALLBACK,
        "scope": " ".join(constants.OAUTH_SUPPORTED_SCOPES), "resource": ORIGIN + "/mcp",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    assert response.status_code == 302
    destination = response.headers["location"]
    assert client.get(destination).status_code == 200
    cid = parse_qs(urlsplit(destination).query)["id"][0]
    allowed = client.post("/consent", data={"id": cid, "decision": "allow"})
    assert allowed.status_code == 302
    returned = parse_qs(urlsplit(allowed.headers["location"]).query, keep_blank_values=True)
    assert returned["state"] == [state]
    assert returned["existing"] == ["a+b"]
    return info, {"grant_type": "authorization_code", "code": returned["code"][0],
                  "redirect_uri": CALLBACK, "code_verifier": VERIFIER}


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
@pytest.mark.parametrize("state", STATES[:5])
def test_realistic_account_linking_response_and_refresh(oauth_client, method, state):
    client, legacy = oauth_client
    info, form = _grant(client, method, state)
    auth = None
    if method == "client_secret_basic":
        auth = (info["client_id"], info["client_secret"])
        # RFC 6749 authenticated Basic client does not need a duplicate body id.
    else:
        form["client_id"] = info["client_id"]
        if method == "client_secret_post":
            form["client_secret"] = info["client_secret"]
    exchange = client.post("/token", data=form, auth=auth)
    assert exchange.status_code == 200, exchange.status_code
    payload = exchange.json()
    assert payload["token_type"].lower() == "bearer"
    assert isinstance(payload["expires_in"], int) and payload["expires_in"] > 0
    assert payload["access_token"] and payload["refresh_token"]
    assert client.get("/mcp", headers={"Authorization": "Bearer " + payload["access_token"]}).status_code == 200
    refresh = {"grant_type": "refresh_token", "refresh_token": payload["refresh_token"]}
    if method != "client_secret_basic":
        refresh["client_id"] = info["client_id"]
        if method == "client_secret_post":
            refresh["client_secret"] = info["client_secret"]
    new = client.post("/token", data=refresh, auth=auth)
    assert new.status_code == 200
    assert new.json()["refresh_token"] != payload["refresh_token"]
    assert client.get("/mcp", params={"token": legacy}).status_code == 200


def test_basic_identity_conflict_fails_without_consuming_code(oauth_client):
    client, _legacy = oauth_client
    info, form = _grant(client, "client_secret_basic")
    auth = (info["client_id"], info["client_secret"])
    bad = client.post("/token", data={**form, "client_id": "different-client"}, auth=auth)
    assert bad.status_code in (400, 401)
    assert client.post("/token", data=form, auth=auth).status_code == 200


def test_post_client_cannot_downgrade_authentication(oauth_client):
    client, _legacy = oauth_client
    info, form = _grant(client, "client_secret_post")
    assert client.post("/token", data={**form, "client_id": info["client_id"]}).status_code == 401
    assert client.post("/token", data={**form, "client_id": info["client_id"], "client_secret": info["client_secret"]}).status_code == 200


def test_duplicate_identity_parameters_are_not_silently_collapsed(oauth_client):
    client, _legacy = oauth_client
    info, form = _grant(client, "client_secret_post")
    body = urlencode({**form, "client_id": info["client_id"], "client_secret": info["client_secret"]})
    body += "&client_id=" + info["client_id"]
    response = client.post("/token", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code in (400, 401)
