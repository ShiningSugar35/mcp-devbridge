"""Read-only-after-registration probe for a public MCP OAuth authorization redirect.

Does not display credentials, follow external redirects, approve consent, or mint tokens.
DCR creates one test client record at the tested server; use only on your own service.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx


def classify(location: str, base: str) -> tuple[str, str]:
    if not location:
        return "missing", ""
    uri = urlsplit(location)
    if uri.path == "/consent" and (not uri.netloc or uri.netloc == urlsplit(base).netloc):
        return "consent_page", ""
    error = parse_qs(uri.query).get("error", [""])[0]
    if error:
        allowed = {"invalid_scope", "invalid_target", "invalid_request", "unauthorized_client", "access_denied", "server_error"}
        return "oauth_error", error if error in allowed else "other_error"
    if uri.scheme == "https" and uri.netloc:
        return "external_redirect", ""
    return "unknown_redirect", ""


def probe(base: str) -> int:
    base = base.rstrip("/")
    resource = base + "/mcp"
    verifier = "mcp-devbridge-oauth-readonly-probe-0123456789abcd"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    callback = "https://example.invalid/only-for-mcp-test"
    with httpx.Client(timeout=12.0, follow_redirects=False, trust_env=False) as client:
        pr = client.get(base + "/.well-known/oauth-protected-resource/mcp")
        auth = client.get(base + "/.well-known/oauth-authorization-server")
        print(f"protected_metadata_http={pr.status_code} authorization_metadata_http={auth.status_code}")
        pr.raise_for_status()
        auth.raise_for_status()
        assert pr.json().get("resource") == resource
        meta = auth.json()
        assert meta.get("registration_endpoint") == base + "/register"
        registration = client.post(
            meta["registration_endpoint"],
            json={
                "client_name": "MCP DevBridge read-only handshake test",
                "redirect_uris": [callback],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "scope": "ACCESS_VIEW_MANAGE_MCP_CONTENT",
            },
        )
        print(f"registration_http={registration.status_code}")
        registration.raise_for_status()
        client_id = registration.json()["client_id"]
        response = client.get(
            meta["authorization_endpoint"],
            params={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": callback,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "ACCESS_VIEW_MANAGE_MCP_CONTENT",
                "resource": resource,
                "state": "temporary-readonly-probe",
            },
        )
        kind, error = classify(response.headers.get("location", ""), base)
        print(f"authorize_http={response.status_code} redirect_kind={kind} error_kind={error or 'none'}")
        altered_resource = client.get(
            meta["authorization_endpoint"],
            params={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": callback,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "ACCESS_VIEW_MANAGE_MCP_CONTENT",
                "resource": resource + "?probe=1",
                "state": "temporary-readonly-probe",
            },
        )
        altered_kind, altered_error = classify(altered_resource.headers.get("location", ""), base)
        print(f"query_resource_http={altered_resource.status_code} redirect_kind={altered_kind} error_kind={altered_error or 'none'}")
        if kind == "consent_page":
            location = response.headers["location"]
            target = base + location if location.startswith("/") else location
            page = client.get(target)
            print(f"consent_page_http={page.status_code}")
            return 0 if page.status_code == 200 else 2
        return 3


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="HTTPS origin of the user's own MCP")
    arguments = parser.parse_args()
    raise SystemExit(probe(arguments.base_url))
