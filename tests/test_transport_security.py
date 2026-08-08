"""Phase 4: DNS rebinding protection for the public tunnel Host.

The Python backend keeps ``enable_dns_rebinding_protection=True`` (loopback
always allowed) and only whitelists the configured public hostname so the
Cloudflare / ngrok tunnel Host is accepted. Non-whitelisted Hosts must be
rejected with 421.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from local_dev_mcp_bridge.models import RuntimeConfig
from local_dev_mcp_bridge.server_factory import (
    TRANSPORT_LOOPBACK_HOSTS,
    build_backend,
    build_transport_security,
)

HOSTNAME = "mcp.example.com"


def _guard_app(public_hostname: str = "") -> Starlette:
    """App exercising the same TransportSecurityMiddleware used by /mcp."""
    security = TransportSecurityMiddleware(build_transport_security(public_hostname))

    app = Starlette()

    async def guard(request: Request) -> JSONResponse:
        error = await security.validate_request(request, is_post=False)
        if error is not None:
            return JSONResponse({"error": "rejected"}, status_code=error.status_code)
        return JSONResponse({"ok": True})

    async def guard_post(request: Request) -> JSONResponse:
        error = await security.validate_request(request, is_post=True)
        if error is not None:
            return JSONResponse({"error": "rejected"}, status_code=error.status_code)
        return JSONResponse({"ok": True})

    app.add_route("/guard", guard, methods=["GET"])
    app.add_route("/guard-post", guard_post, methods=["POST"])
    return app


class TestBuildTransportSecurity:
    def test_loopback_always_allowed(self) -> None:
        settings = build_transport_security()
        assert settings.enable_dns_rebinding_protection is True
        assert all(h in settings.allowed_hosts for h in TRANSPORT_LOOPBACK_HOSTS)

    def test_public_hostname_appended_with_and_without_port(self) -> None:
        settings = build_transport_security(HOSTNAME)
        assert HOSTNAME in settings.allowed_hosts
        assert f"{HOSTNAME}:*" in settings.allowed_hosts
        assert "evil.example.com" not in settings.allowed_hosts

    def test_scheme_and_slash_normalized(self) -> None:
        settings = build_transport_security(f"https://{HOSTNAME}/mcp")
        assert HOSTNAME in settings.allowed_hosts

    def test_runtime_config_round_trip(self) -> None:
        rc = RuntimeConfig(workspace=str(Path.cwd()), public_hostname=HOSTNAME)
        assert rc.public_hostname == HOSTNAME


class TestHostGate:
    def test_loopback_accepted(self) -> None:
        with TestClient(_guard_app()) as client:
            assert client.get("/guard", headers={"Host": "127.0.0.1:8000"}).status_code == 200

    def test_public_tunnel_host_accepted(self) -> None:
        with TestClient(_guard_app(HOSTNAME)) as client:
            assert client.get("/guard", headers={"Host": HOSTNAME}).status_code == 200

    def test_public_host_with_port_accepted(self) -> None:
        with TestClient(_guard_app(HOSTNAME)) as client:
            assert client.get("/guard", headers={"Host": f"{HOSTNAME}:443"}).status_code == 200

    def test_rogue_host_rejected(self) -> None:
        with TestClient(_guard_app(HOSTNAME)) as client:
            assert client.get("/guard", headers={"Host": "evil.example.net"}).status_code == 421

    def test_direct_ip_rejected(self) -> None:
        with TestClient(_guard_app(HOSTNAME)) as client:
            assert client.get("/guard", headers={"Host": "203.0.113.9"}).status_code == 421

    def test_post_requires_json_content_type(self) -> None:
        with TestClient(_guard_app(HOSTNAME)) as client:
            bad = client.post("/guard-post", headers={"Host": HOSTNAME})
            assert bad.status_code == 400
            good = client.post(
                "/guard-post",
                headers={"Host": HOSTNAME, "Content-Type": "application/json"},
                content=b"{}",
            )
            assert good.status_code == 200

    def test_no_hostname_configured_still_rejects_rogue(self) -> None:
        with TestClient(_guard_app("")) as client:
            assert client.get("/guard", headers={"Host": HOSTNAME}).status_code == 421


class TestBuildBackendWiring:
    def test_streamable_http_app_receives_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_streamable_http_app(self, **kwargs) -> Starlette:
            captured.update(kwargs)
            return Starlette()

        monkeypatch.setattr(
            "local_dev_mcp_bridge.server_factory.MCPServer.streamable_http_app",
            fake_streamable_http_app,
        )
        rc = RuntimeConfig(workspace=str(Path.cwd()), public_hostname=HOSTNAME)
        build_backend(rc)
        settings = captured.get("transport_security")
        assert isinstance(settings, TransportSecuritySettings)
        assert settings.enable_dns_rebinding_protection is True
        assert HOSTNAME in settings.allowed_hosts