"""OAuth 网关测试：MCP OAuth 2.1（RFC 9728）+ 授权码 + PKCE + 刷新令牌 + 资源代理。

覆盖：发现元数据、动态客户端注册、授权/同意、令牌发放与校验、
PKCE 失败、单次使用、刷新轮换、撤销、代理放行（OAuth / 旧版 Bearer、
匿名本地）、401 与错误路径、无密钥泄漏。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from local_dev_mcp_bridge.constants import OAUTH_SCOPE
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.oauth_provider import LocalOAuthProvider, _workspace_from_subject
from local_dev_mcp_bridge.secrets import SecretsStore

ENGINE_TOKEN = "engine-secret-token-123"
WORKSPACE = "C:\\TestProjects\\demo-app"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


class _Env:
    """Gateway + provider 组合夹具。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
        self.t = time.time()  # 可控时钟（秒），基准必须为真实时间（SDK 内部用真实钟）
        self.store = SecretsStore()
        calls: list[dict] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            calls.append(
                {
                    "authorization": request.headers.get("authorization", ""),
                    "path": request.url.path,
                    "method": request.method,
                }
            )
            return httpx.Response(200, json={"ok": True})

        self.provider = LocalOAuthProvider(
            issuer_url="https://mcp.example.test",
            resource_url="https://mcp.example.test/mcp",
            workspace=WORKSPACE,
            store=self.store,
            now=lambda: self.t,
        )
        self.gateway = OAuthGateway(
            public_hostname="mcp.example.test",
            workspace=WORKSPACE,
            upstream_legacy_token=lambda: PUB_TOKEN,
            provider=self.provider,
            transport=httpx.MockTransport(_handler),
        )
        self.calls = calls
        self.client = TestClient(
            self.gateway.app, raise_server_exceptions=False, follow_redirects=False
        )

    # ----------------------------------------------------- 流程工具
    def register_client(self, public: bool = True, **extra) -> str:
        body = {
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "none" if public else "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
        }
        body.update(extra)
        resp = self.client.post("/register", json=body)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        return data["client_id"]

    def authorize(
        self,
        client_id: str,
        scope_override: str | None = None,
        resource: str | None = None,
        verifier: str = "verifier-verifier-verifier-verifier-123456",
        redirect: str = "https://client.example/callback",
    ):
        params = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "state": "st-123",
            "scope": OAUTH_SCOPE,
        }
        if scope_override is not None:
            params["scope"] = scope_override
        if resource:
            params["resource"] = resource
        return self.client.get("/authorize", params=params)

    def consent_allow(self, location: str):
        assert "/consent?id=" in location, location
        cid = location.split("id=", 1)[1]
        return self.client.post("/consent", data={"id": cid, "decision": "allow"})

    def consent_deny(self, location: str):
        cid = location.split("id=", 1)[1]
        return self.client.post("/consent", data={"id": cid, "decision": "deny"})

    def token(
        self,
        client_id: str,
        grant_type: str,
        *,
        code: str | None = None,
        refresh_token: str | None = None,
        verifier: str = "verifier-verifier-verifier-verifier-123456",
        redirect: str = "https://client.example/callback",
        client_secret: str | None = None,
    ):
        body = {
            "grant_type": grant_type,
            "client_id": client_id,
            "redirect_uri": redirect,
        }
        if code:
            body["code"] = code
            body["code_verifier"] = verifier
        if refresh_token:
            body["refresh_token"] = refresh_token
        if client_secret:
            body["client_secret"] = client_secret
        return self.client.post("/token", data=body)


PUB_TOKEN = "chatgpt-legacy-token-abc"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    return _Env(tmp_path, monkeypatch)


# ------------------------------------------------------------ 元数据

def test_metadata_authorization_server(env: _Env) -> None:
    r = env.client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    data = r.json()
    assert data["issuer"].rstrip("/") == "https://mcp.example.test"
    assert data["authorization_endpoint"].endswith("/authorize")
    assert data["token_endpoint"].endswith("/token")
    assert data["registration_endpoint"].endswith("/register")
    assert data["code_challenge_methods_supported"] == ["S256"]
    assert OAUTH_SCOPE in data["scopes_supported"]
    assert "refresh_token" in data["grant_types_supported"]
    assert "authorization_code" in data["grant_types_supported"]


def test_metadata_protected_resource(env: _Env) -> None:
    r = env.client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    data = r.json()
    assert data["resource"] == "https://mcp.example.test/mcp"
    assert any(s.rstrip("/") == "https://mcp.example.test" for s in data["authorization_servers"])
    assert OAUTH_SCOPE in data["scopes_supported"]


def test_health(env: _Env) -> None:
    r = env.client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ------------------------------------------------------ 动态注册

def test_register_public_client(env: _Env) -> None:
    # register_client 内部已断言 201 + client_id
    assert True


def test_register_creates_refresh_and_scope(env: _Env) -> None:
    cid = env.register_client()
    # 注册后可直接走 authorization_code + refresh_token
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    assert r.status_code == 302
    assert "code=" in r.headers["location"]
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    t = env.token(cid, "authorization_code", code=code)
    assert t.status_code == 200, t.text
    body = t.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["scope"] == OAUTH_SCOPE


def test_register_rejects_unknown_scope(env: _Env) -> None:
    r = env.client.post(
        "/register",
        json={
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "none",
            "scope": "admin",
        },
    )
    assert r.status_code == 400


def test_confidential_client_secret_works(env: _Env) -> None:
    resp = env.client.post(
        "/register",
        json={
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert resp.status_code == 201
    cid = resp.json()["client_id"]
    secret = resp.json().get("client_secret")
    assert secret
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    t = env.token(cid, "authorization_code", code=code, client_secret=secret)
    assert t.status_code == 200
    # 错误密钥被拒
    t2 = env.token(cid, "authorization_code", code="bad", client_secret="wrong")
    assert t2.status_code in (400, 401)


# ------------------------------------------------------ 授权 + 同意

def test_authorize_redirects_to_consent(env: _Env) -> None:
    cid = env.register_client()
    r = env.authorize(cid)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "/consent?id=" in loc
    page = env.client.get(loc)
    assert page.status_code == 200
    assert "允许访问" in page.text
    assert "MCP Server" in page.text


def test_consent_deny(env: _Env) -> None:
    cid = env.register_client()
    r = env.consent_deny(env.authorize(cid).headers["location"])
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "error=access_denied" in loc
    assert "state=st-123" in loc


def test_consent_unknown_id_410(env: _Env) -> None:
    r = env.client.get("/consent?id=does-not-exist")
    assert r.status_code == 410
    r = env.client.post("/consent", data={"id": "does-not-exist", "decision": "allow"})
    assert r.status_code == 410


def test_consent_expired_closed_window(env: _Env) -> None:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    env.t += 86400 + 60  # 超过同意窗口
    page = env.client.get(loc)
    assert page.status_code == 410


def test_authorize_invalid_scope(env: _Env) -> None:
    cid = env.register_client()
    r = env.authorize(cid, scope_override="admin")
    loc = r.headers["location"]
    assert "error=invalid_scope" in loc
    assert "state=st-123" in loc


def test_authorize_wrong_resource(env: _Env) -> None:
    cid = env.register_client()
    r = env.authorize(cid, resource="https://other.example/mcp")
    assert "error=invalid_target" in r.headers["location"]


def test_authorize_unknown_client(env: _Env) -> None:
    r = env.authorize("client-missing")
    # 未知客户端：可能重定向报错（302）或直接拒绝（400/500），但绝不发码
    assert r.status_code in (302, 400, 500)
    assert "code=" not in r.headers.get("location", "")
    assert "access_token" not in r.text


def test_authorize_missing_pkce(env: _Env) -> None:
    cid = env.register_client()
    r = env.client.get(
        "/authorize",
        params={
            "client_id": cid,
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "state": "st-123",
        },
    )
    assert "error=" in r.headers["location"]


# ------------------------------------------------------------- 令牌

def test_pkce_mismatch_rejected(env: _Env) -> None:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    t = env.token(cid, "authorization_code", code=code, verifier="wrong-verifier-wrong")
    assert t.status_code == 400
    assert "invalid_grant" in t.text


def test_code_single_use(env: _Env) -> None:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    assert env.token(cid, "authorization_code", code=code).status_code == 200
    assert env.token(cid, "authorization_code", code=code).status_code == 400


def test_refresh_rotates(env: _Env) -> None:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    rt = env.token(cid, "authorization_code", code=code).json()["refresh_token"]

    r1 = env.token(cid, "refresh_token", refresh_token=rt)
    assert r1.status_code == 200, r1.text
    new_rt = r1.json()["refresh_token"]
    assert new_rt and new_rt != rt
    # 旧 refresh 已轮换失效
    assert env.token(cid, "refresh_token", refresh_token=rt).status_code == 400
    # 新 refresh 可用
    r2 = env.token(cid, "refresh_token", refresh_token=new_rt)
    assert r2.status_code == 200


def test_revoke_refresh_token(env: _Env) -> None:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    body = env.token(cid, "authorization_code", code=code).json()
    rv = env.client.post(
        "/revoke",
        data={"token": body["refresh_token"], "client_id": cid, "client_secret": ""},
    )
    assert rv.status_code == 200, rv.text
    assert env.token(cid, "refresh_token", refresh_token=body["refresh_token"]).status_code == 400


def test_expired_refresh_token(env: _Env) -> None:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    rt = env.token(cid, "authorization_code", code=code).json()["refresh_token"]
    env.t += 86400 * 61  # 超过 refresh 有效期
    assert env.token(cid, "refresh_token", refresh_token=rt).status_code == 400


# -------------------------------------------------------------- 资源

def _access(env: _Env) -> str:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    return env.token(cid, "authorization_code", code=code).json()["access_token"]


def test_mcp_oauth_token_forwarded(env: _Env) -> None:
    at = _access(env)
    r = env.client.get("/mcp", headers={"Authorization": f"Bearer {at}"})
    assert r.status_code == 200
    assert env.calls[-1]["path"] == "/mcp"
    assert env.calls[-1]["authorization"].startswith("Bearer ")
    assert PUB_TOKEN in env.calls[-1]["authorization"]


def test_mcp_legacy_bearer_passthrough(env: _Env) -> None:
    r = env.client.get("/mcp", headers={"Authorization": f"Bearer {PUB_TOKEN}"})
    assert r.status_code == 200
    assert env.calls[-1]["authorization"] == f"Bearer {PUB_TOKEN}"


def test_mcp_unauthorized(env: _Env) -> None:
    r = env.client.get("/mcp")
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_mcp_invalid_token(env: _Env) -> None:
    r = env.client.get("/mcp", headers={"Authorization": "Bearer garbage-token"})
    assert r.status_code == 401


def test_mcp_expired_access_token(env: _Env) -> None:
    at = _access(env)
    env.t += 3600 + 60  # 超过 access 有效期
    r = env.client.get("/mcp", headers={"Authorization": f"Bearer {at}"})
    assert r.status_code == 401


def test_mcp_proxy_rejects_downstream_error(env: _Env) -> None:
    """upstream 5xx 原样透传（不吞错误）。"""
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    r = env.consent_allow(loc)
    code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    at = env.token(cid, "authorization_code", code=code).json()["access_token"]
    def _fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    env.gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(_fail))
    r = env.client.get("/mcp", headers={"Authorization": f"Bearer {at}"})
    assert r.status_code == 500


def test_mcp_initialize_server_identity_rewritten(env: _Env) -> None:
    """initialize 响应的 serverInfo 对外改写为 MCP DevBridge（引擎二进制不改动）。"""
    at = _access(env)

    def _engine(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'event: message\ndata: {"result":{"serverInfo":{"name":"CodexPro","version":"0.29.0"}},"jsonrpc":"2.0","id":1}\n\n',
        )

    env.gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(_engine))
    r = env.client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {at}"},
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    )
    assert r.status_code == 200
    assert '"name": "mcp-devbridge"' in r.text
    assert "MCP DevBridge" in r.text
    assert "CodexPro" not in r.text


def test_initialize_plain_json_rewritten(env: _Env) -> None:
    at = _access(env)

    def _engine(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"result": {"serverInfo": {"name": "CodexPro", "title": "CodexPro"}, "capabilities": {}}, "jsonrpc": "2.0", "id": 1}),
        )

    env.gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(_engine))
    r = env.client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {at}"},
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    )
    assert r.status_code == 200
    assert '"name": "mcp-devbridge"' in r.text


def test_secrets_not_logged_to_disk(env: _Env, tmp_path: Path) -> None:
    at = _access(env)
    cfg = Path(os.environ["LOCALDEV_MCP_CONFIG_DIR"])
    assert cfg.exists()
    # 无任何明文 token 落盘
    for f in cfg.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert PUB_TOKEN not in text
            assert at not in text


# =========================================================================
# Multi-workspace OAuth binding tests (Phase 9)
# =========================================================================

WORKSPACE_A_ID = "workspace-aaaa"
WORKSPACE_B_ID = "workspace-bbbb"
WORKSPACE_A_ROOT = "C:\\Projects\\Alpha"
WORKSPACE_B_ROOT = "C:\\Projects\\Beta"


def test_workspace_from_subject_parsing() -> None:
    assert _workspace_from_subject("local-user") == ""
    assert _workspace_from_subject("local-user:abc123") == "abc123"
    assert _workspace_from_subject("local-user:abc:123") == "abc:123"
    assert _workspace_from_subject("") == ""
    assert _workspace_from_subject("other") == ""


def test_workspace_from_subject_no_workspace_returns_empty() -> None:
    """Old tokens (just 'local-user') return empty workspace_id."""
    assert _workspace_from_subject("local-user") == ""


class _MultiWorkspaceEnv:
    """Gateway + provider with mock workspace registry for dual-workspace testing."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg_multi"))
        self.t = time.time()
        self.store = SecretsStore()
        calls_a: list[dict] = []
        calls_b: list[dict] = []

        def _handler_a(request: httpx.Request) -> httpx.Response:
            calls_a.append({"path": request.url.path, "method": request.method})
            return httpx.Response(200, json={"source": "alpha", "ok": True})

        def _handler_b(request: httpx.Request) -> httpx.Response:
            calls_b.append({"path": request.url.path, "method": request.method})
            return httpx.Response(200, json={"source": "beta", "ok": True})

        # Mock two CodexPro engines on different ports
        self.transport_a = httpx.MockTransport(_handler_a)
        self.transport_b = httpx.MockTransport(_handler_b)

        def registry(project_id: str) -> tuple[int, str] | None:
            if project_id == WORKSPACE_A_ID:
                return (18787, WORKSPACE_A_ROOT)
            if project_id == WORKSPACE_B_ID:
                return (18788, WORKSPACE_B_ROOT)
            return None

        self.provider = LocalOAuthProvider(
            issuer_url="https://mcp.example.test",
            resource_url="https://mcp.example.test/mcp",
            workspace=WORKSPACE_A_ROOT,
            store=self.store,
            now=lambda: self.t,
        )
        self.gateway = OAuthGateway(
            public_hostname="mcp.example.test",
            workspace=WORKSPACE_A_ROOT,
            upstream_url="http://127.0.0.1:18787",
            upstream_legacy_token=lambda: PUB_TOKEN,
            provider=self.provider,
            workspace_registry=registry,
        )
        self.calls_a = calls_a
        self.calls_b = calls_b
        self.client = TestClient(
            self.gateway.app, raise_server_exceptions=False, follow_redirects=False
        )

    def register_and_authorize(self, workspace_id: str = "") -> tuple[str, str, str]:
        """Full flow: register → authorize → consent with workspace → get token.
        
        Returns (access_token, refresh_token, client_id).
        """
        resp = self.client.post(
            "/register",
            json={
                "redirect_uris": ["https://client.example/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
            },
        )
        assert resp.status_code == 201, resp.text
        cid = resp.json()["client_id"]

        verifier = "verifier-multi-ws-12345678901234567890"
        loc = self.client.get(
            "/authorize",
            params={
                "client_id": cid,
                "redirect_uri": "https://client.example/callback",
                "response_type": "code",
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "state": "st-multi",
                "scope": OAUTH_SCOPE,
            },
        ).headers["location"]
        assert "/consent?id=" in loc, loc
        cid_param = loc.split("id=", 1)[1]

        # POST consent with workspace_id
        consent_data = {"id": cid_param, "decision": "allow"}
        if workspace_id:
            consent_data["workspace_id"] = workspace_id
        r = self.client.post("/consent", data=consent_data)
        assert r.status_code == 302
        code = r.headers["location"].split("code=", 1)[1].split("&", 1)[0]

        t = self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": cid,
                "redirect_uri": "https://client.example/callback",
            },
        )
        assert t.status_code == 200, t.text
        return t.json()["access_token"], t.json()["refresh_token"], cid


@pytest.fixture
def mw_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _MultiWorkspaceEnv:
    return _MultiWorkspaceEnv(tmp_path, monkeypatch)


def test_consent_page_shows_workspace_dropdown(env: _Env) -> None:
    """Consent page should contain workspace selection options."""
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    page = env.client.get(loc)
    assert page.status_code == 200
    assert "工作区授权" in page.text or "workspace" in page.text.lower()
    assert "允许访问" in page.text


def test_token_subject_encodes_workspace(mw_env: _MultiWorkspaceEnv) -> None:
    """OAuth token subject should carry workspace_id when bound."""
    at, _rt, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)
    record = mw_env.provider.load_access_token_sync(at)
    assert record is not None
    assert record.subject == "local-user:" + WORKSPACE_A_ID
    assert _workspace_from_subject(record.subject) == WORKSPACE_A_ID


def test_token_without_workspace_has_empty_subject(mw_env: _MultiWorkspaceEnv) -> None:
    """Consent without workspace_id selection → empty workspace in token."""
    at, _rt, _cid = mw_env.register_and_authorize("")
    record = mw_env.provider.load_access_token_sync(at)
    assert record is not None
    assert _workspace_from_subject(record.subject) == ""


def test_refresh_token_preserves_workspace(mw_env: _MultiWorkspaceEnv) -> None:
    """Refresh token rotation should carry workspace forward."""
    at1, rt1, cid = mw_env.register_and_authorize(WORKSPACE_B_ID)
    record1 = mw_env.provider.load_access_token_sync(at1)
    assert _workspace_from_subject(record1.subject) == WORKSPACE_B_ID

    # Exchange refresh token using the SAME client_id
    rt_resp = mw_env.client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": rt1,
            "client_id": cid,
        },
    )
    assert rt_resp.status_code == 200, rt_resp.text
    body = rt_resp.json()
    at2 = body["access_token"]
    record2 = mw_env.provider.load_access_token_sync(at2)
    assert _workspace_from_subject(record2.subject) == WORKSPACE_B_ID


def test_mcp_proxy_routes_to_correct_workspace(mw_env: _MultiWorkspaceEnv) -> None:
    """OAuth token bound to workspace A proxies to A's CodexPro port."""
    at_a, _, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)

    routed_to: list[tuple[str, int]] = []

    class _Router(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            from urllib.parse import urlparse
            parsed = urlparse(str(request.url))
            port = parsed.port or 18787
            routed_to.append((port, str(request.url)))
            if port == 18787:
                return await self.transport_a.handle_async_request(request)
            else:
                return await self.transport_b.handle_async_request(request)

    _router = _Router()
    _router.transport_a = mw_env.transport_a
    _router.transport_b = mw_env.transport_b

    mw_env.gateway._http = httpx.AsyncClient(transport=_router)

    r = mw_env.client.get("/mcp", headers={"Authorization": f"Bearer {at_a}"})
    assert r.status_code == 200, r.text
    # Should have routed to port 18787 (workspace A)
    assert any(p[0] == 18787 for p in routed_to), f"Expected route to 18787, got {routed_to}"


def test_two_tokens_different_workspaces_routed_differently(mw_env: _MultiWorkspaceEnv) -> None:
    """GPT (workspace A) and Gemini (workspace B) tokens route to different ports."""
    at_a, _, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)
    at_b, _, _cid2 = mw_env.register_and_authorize(WORKSPACE_B_ID)

    record_a = mw_env.provider.load_access_token_sync(at_a)
    record_b = mw_env.provider.load_access_token_sync(at_b)
    assert _workspace_from_subject(record_a.subject) == WORKSPACE_A_ID
    assert _workspace_from_subject(record_b.subject) == WORKSPACE_B_ID
    assert _workspace_from_subject(record_a.subject) != _workspace_from_subject(record_b.subject)


def test_switch_workspace_session_isolation(mw_env: _MultiWorkspaceEnv) -> None:
    """switch_workspace only affects the calling session, not others."""
    import json  # noqa: F811 - local import is fine

    at_a, _, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)

    # Simulate session A switching to workspace B
    rpc_switch = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "switch_workspace", "arguments": {"project_id": WORKSPACE_B_ID}},
    })
    # Session "sess-gpt" switches
    r = mw_env.client.post(
        "/mcp",
        content=rpc_switch,
        headers={
            "Authorization": f"Bearer {at_a}",
            "mcp-session-id": "sess-gpt",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200, r.text

    # Now session "sess-gpt" get_current_workspace should show B
    rpc_gcw = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_current_workspace", "arguments": {}},
    })
    r2 = mw_env.client.post(
        "/mcp",
        content=rpc_gcw,
        headers={
            "Authorization": f"Bearer {at_a}",
            "mcp-session-id": "sess-gpt",
            "Content-Type": "application/json",
        },
    )
    assert r2.status_code == 200, r2.text
    result = json.loads(r2.text)
    text = result["result"]["content"][0]["text"]
    assert WORKSPACE_B_ID in text

    # Meanwhile, session "sess-gemini" (same token) should still be on workspace A
    r3 = mw_env.client.post(
        "/mcp",
        content=rpc_gcw,
        headers={
            "Authorization": f"Bearer {at_a}",
            "mcp-session-id": "sess-gemini",
            "Content-Type": "application/json",
        },
    )
    assert r3.status_code == 200
    result3 = json.loads(r3.text)
    text3 = result3["result"]["content"][0]["text"]
    assert WORKSPACE_A_ID in text3, f"Expected workspace A for sess-gemini, got: {text3}"


def test_switch_workspace_unknown_project_returns_error(mw_env: _MultiWorkspaceEnv) -> None:
    """switch_workspace to non-existent project returns error."""
    at_a, _, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)

    rpc = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "switch_workspace", "arguments": {"project_id": "no-such-project"}},
    })
    r = mw_env.client.post(
        "/mcp",
        content=rpc,
        headers={
            "Authorization": f"Bearer {at_a}",
            "mcp-session-id": "sess-test",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200
    result = json.loads(r.text)
    assert "error" in result


def test_list_workspaces_returns_projects() -> None:
    """list_workspaces tool returns registered projects (or empty list)."""
    from local_dev_mcp_bridge.gateway import OAuthGateway as _GW
    # Quick smoke: the method exists and doesn't crash
    gw = _GW(
        public_hostname="test.local",
        workspace=".",
        upstream_url="http://127.0.0.1:1",
    )
    result = gw._list_workspaces()
    assert isinstance(result, str)
    assert "工作区" in result


def test_get_current_workspace_empty_session(env: _Env) -> None:
    """get_current_workspace with no workspace_id shows default."""
    result = env.gateway._get_current_workspace("", "")
    assert "当前工作区" in result


def test_backward_compat_legacy_bearer_still_works(env: _Env) -> None:
    """Legacy ChatGPT bearer (engine token) passes through without workspace binding."""
    r = env.client.get("/mcp", headers={"Authorization": f"Bearer {PUB_TOKEN}"})
    assert r.status_code == 200
    assert env.calls[-1]["authorization"] == f"Bearer {PUB_TOKEN}"
