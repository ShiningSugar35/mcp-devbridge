"""v0.9.0: OAuth and existing personal connector coexist on one Hub.

This is an isolated protocol regression, not proof of a Google account-linking
session. It never visits Google or touches a live connector or its credentials.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
from starlette.testclient import TestClient

import local_dev_mcp_bridge.gateway as gateway_module
from local_dev_mcp_bridge.constants import OAUTH_SCOPE
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.oauth_provider import LocalOAuthProvider
from local_dev_mcp_bridge.secrets import SecretsStore

ORIGIN = "https://mcp.example.test"
RESOURCE = ORIGIN + "/mcp"
CALLBACK = "https://oauth-redirect.googleusercontent.com/complete"
VERIFIER = "gemini-dcr-verifier-0123456789-1234567890-xyz"


def _challenge() -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(VERIFIER.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")


def test_gemini_dcr_oauth_and_existing_chatgpt_url_share_one_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "isolated-config"))
    monkeypatch.setattr(gateway_module, "_LOG_DIR", tmp_path / "logs")
    legacy_access_code = secrets.token_urlsafe(32)
    calls: list[tuple[str, str, str]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append((
            request.headers.get("authorization", ""),
            request.url.path,
            request.url.query.decode("ascii"),
        ))
        return httpx.Response(200, json={"ok": True})

    provider = LocalOAuthProvider(
        issuer_url=ORIGIN,
        resource_url=RESOURCE,
        store=SecretsStore(),
    )
    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_legacy_token=lambda: legacy_access_code,
        provider=provider,
        transport=httpx.MockTransport(upstream),
    )

    with TestClient(gateway.app, follow_redirects=False) as client:
        # The old URL still works and does not leak its query to the engine.
        old = client.get("/mcp", params={"token": legacy_access_code})
        assert old.status_code == 200
        assert calls[-1] == (f"Bearer {legacy_access_code}", "/mcp", "")

        assert client.get("/mcp").status_code == 401
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["registration_endpoint"] == ORIGIN + "/register"

        registration = client.post(
            "/register",
            json={
                "redirect_uris": [CALLBACK],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": OAUTH_SCOPE,
            },
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]
        params = {
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
            "scope": OAUTH_SCOPE,
            "resource": RESOURCE,
            "state": "opaque-test-state",
        }
        authorize = client.get("/authorize", params=params)
        assert authorize.status_code == 302
        location = authorize.headers["location"]
        assert location.startswith("/consent?id=")
        assert client.get(location).status_code == 200

        approved = client.post(
            "/consent",
            data={"id": parse_qs(urlsplit(location).query)["id"][0], "decision": "allow"},
        )
        assert approved.status_code == 302
        callback_url = urlsplit(approved.headers["location"])
        assert callback_url.scheme == "https"
        assert callback_url.netloc == urlsplit(CALLBACK).netloc
        returned = parse_qs(callback_url.query)
        assert returned["state"] == [params["state"]]

        exchanged = client.post(
            "/token",
            data={
                "client_id": client_id,
                "grant_type": "authorization_code",
                "redirect_uri": CALLBACK,
                "code": returned["code"][0],
                "code_verifier": VERIFIER,
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        oauth = exchanged.json()
        assert oauth["access_token"] and oauth["refresh_token"]

        # No second service instance and no changes to the ChatGPT access code.
        modern = client.get(
            "/mcp", headers={"Authorization": f"Bearer {oauth['access_token']}"}
        )
        assert modern.status_code == 200
        assert calls[-1] == (f"Bearer {legacy_access_code}", "/mcp", "")
        old_again = client.get("/mcp", params={"token": legacy_access_code})
        assert old_again.status_code == 200
        assert calls[-1] == (f"Bearer {legacy_access_code}", "/mcp", "")

        refreshed = client.post(
            "/token",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": oauth["refresh_token"],
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["refresh_token"] != oauth["refresh_token"]

        # Removing Gemini's independent OAuth authorization must not change
        # the existing ChatGPT connection or its server-side credential.
        revoked = client.post(
            "/revoke",
            data={"client_id": client_id, "client_secret": "", "token": refreshed.json()["refresh_token"]},
        )
        assert revoked.status_code == 200
        assert client.post(
            "/token",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refreshed.json()["refresh_token"],
            },
        ).status_code == 400
        unaffected = client.get("/mcp", params={"token": legacy_access_code})
        assert unaffected.status_code == 200
        assert calls[-1] == (f"Bearer {legacy_access_code}", "/mcp", "")

        # A pasted No Auth URL is not an OAuth resource identifier; fail closed.
        bad_resource = client.get(
            "/authorize", params={**params, "resource": RESOURCE + "?example=1"}
        )
        assert bad_resource.status_code == 302
        assert parse_qs(urlsplit(bad_resource.headers["location"]).query).get("error") == [
            "invalid_target"
        ]
