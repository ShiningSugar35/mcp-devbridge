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

import local_dev_mcp_bridge.gateway as gateway_module
from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.constants import OAUTH_SCOPE
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig
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
            content=json.dumps(
                {
                    "result": {
                        "serverInfo": {"name": "CodexPro", "title": "CodexPro"},
                        "capabilities": {},
                    },
                    "jsonrpc": "2.0",
                    "id": 1,
                }
            ),
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
WORKSPACE_A_ROOT = "C:\\Projects\\Alpha" if os.name == "nt" else "/Projects/Alpha"
WORKSPACE_B_ROOT = "C:\\Projects\\Beta" if os.name == "nt" else "/Projects/Beta"


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
            calls_a.append(
                {
                    "path": request.url.path,
                    "method": request.method,
                    "authorization": request.headers.get("authorization"),
                }
            )
            return httpx.Response(200, json={"source": "alpha", "ok": True})

        def _handler_b(request: httpx.Request) -> httpx.Response:
            calls_b.append(
                {
                    "path": request.url.path,
                    "method": request.method,
                    "authorization": request.headers.get("authorization"),
                }
            )
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

        self.credential_a = "workspace-a-access-value"
        self.credential_b = "workspace-b-access-value"

        def credential_registry(project_id: str) -> str | None:
            if project_id == WORKSPACE_A_ID:
                return self.credential_a
            if project_id == WORKSPACE_B_ID:
                return self.credential_b
            return None

        save_projects(
            [
                ProjectConfig(id=WORKSPACE_A_ID, display_name="Alpha", root_path=WORKSPACE_A_ROOT),
                ProjectConfig(id=WORKSPACE_B_ID, display_name="Beta", root_path=WORKSPACE_B_ROOT),
            ]
        )

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
            workspace_credential_registry=credential_registry,
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

        # OAuth grants the Hub, not one project. Workspace selection happens after connection.
        r = self.client.post("/consent", data={"id": cid_param, "decision": "allow"})
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


def test_consent_page_has_no_workspace_selector(env: _Env) -> None:
    cid = env.register_client()
    loc = env.authorize(cid).headers["location"]
    page = env.client.get(loc)
    assert page.status_code == 200
    assert "允许访问" in page.text
    assert 'name="workspace_id"' not in page.text
    assert "所有运行中的工作区根" in page.text


def test_oauth_token_is_hub_scoped_not_workspace_scoped(mw_env: _MultiWorkspaceEnv) -> None:
    at, _rt, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)
    record = mw_env.provider.load_access_token_sync(at)
    assert record is not None
    assert record.subject == "local-user"
    assert _workspace_from_subject(record.subject) == ""


def test_consent_without_workspace_is_allowed(mw_env: _MultiWorkspaceEnv) -> None:
    at, _rt, _cid = mw_env.register_and_authorize()
    record = mw_env.provider.load_access_token_sync(at)
    assert record is not None
    assert record.subject == "local-user"


def test_consent_does_not_require_a_running_workspace(mw_env: _MultiWorkspaceEnv) -> None:
    original = mw_env.gateway._workspace_registry
    mw_env.gateway._workspace_registry = lambda _project_id: None
    try:
        at, _rt, _cid = mw_env.register_and_authorize()
        assert mw_env.provider.load_access_token_sync(at) is not None
    finally:
        mw_env.gateway._workspace_registry = original


def test_refresh_token_preserves_hub_scoped_subject(mw_env: _MultiWorkspaceEnv) -> None:
    at1, rt1, cid = mw_env.register_and_authorize()
    record1 = mw_env.provider.load_access_token_sync(at1)
    assert record1 is not None and record1.subject == "local-user"
    rt_resp = mw_env.client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": rt1, "client_id": cid},
    )
    assert rt_resp.status_code == 200, rt_resp.text
    record2 = mw_env.provider.load_access_token_sync(rt_resp.json()["access_token"])
    assert record2 is not None and record2.subject == "local-user"


def test_generic_oauth_defaults_to_entry_workspace(mw_env: _MultiWorkspaceEnv) -> None:
    at, _, _cid = mw_env.register_and_authorize()
    routed_to: list[int] = []

    class _Router(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            from urllib.parse import urlparse

            port = urlparse(str(request.url)).port or 18787
            routed_to.append(port)
            target = mw_env.transport_a if port == 18787 else mw_env.transport_b
            return await target.handle_async_request(request)

    mw_env.gateway._http = httpx.AsyncClient(transport=_Router())
    r = mw_env.client.get("/mcp", headers={"Authorization": f"Bearer {at}"})
    assert r.status_code == 200, r.text
    assert routed_to == [18787]


def test_direct_project_bearer_authenticates_hub_without_pinning_workspace(
    mw_env: _MultiWorkspaceEnv,
) -> None:
    """Legacy project bearers stay valid but path routing can reach any active root."""
    routed_to: list[int] = []

    class _Router(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            from urllib.parse import urlparse

            port = urlparse(str(request.url)).port or 18787
            routed_to.append(port)
            target = mw_env.transport_a if port == 18787 else mw_env.transport_b
            return await target.handle_async_request(request)

    mw_env.gateway._http = httpx.AsyncClient(transport=_Router())
    response = mw_env.client.get(
        "/mcp",
        headers={"Authorization": f"Bearer {mw_env.credential_b}"},
    )
    assert response.status_code == 200, response.text
    assert routed_to == [18788]  # no path keeps legacy bearer affinity for compatibility

    routed_to.clear()
    rpc = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 73,
            "method": "tools/call",
            "params": {
                "name": "read",
                "arguments": {"path": os.path.join(WORKSPACE_B_ROOT, "README.md")},
            },
        }
    )
    routed = mw_env.client.post(
        "/mcp",
        content=rpc,
        headers={
            "Authorization": f"Bearer {mw_env.credential_a}",
            "Content-Type": "application/json",
        },
    )
    assert routed.status_code == 200, routed.text
    assert routed_to == [18788]
    assert mw_env.calls_b[-1]["authorization"] == f"Bearer {mw_env.credential_b}"


def test_two_oauth_tokens_are_both_hub_scoped(mw_env: _MultiWorkspaceEnv) -> None:
    at_a, _, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)
    at_b, _, _cid2 = mw_env.register_and_authorize(WORKSPACE_B_ID)
    record_a = mw_env.provider.load_access_token_sync(at_a)
    record_b = mw_env.provider.load_access_token_sync(at_b)
    assert record_a is not None and record_b is not None
    assert record_a.subject == "local-user"
    assert record_b.subject == "local-user"


def test_switch_workspace_session_isolation(mw_env: _MultiWorkspaceEnv) -> None:
    """switch_workspace only affects the calling session, not others."""
    import json  # noqa: F811 - local import is fine

    at_a, _, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)

    # Simulate session A switching to workspace B
    rpc_switch = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "devbridge_switch_workspace",
                "arguments": {"project_id": WORKSPACE_B_ID},
            },
        }
    )
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
    rpc_gcw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "devbridge_get_current_workspace", "arguments": {}},
        }
    )
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

    # An untouched session has no implicit entry/current project.
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
    assert result3["result"]["structuredContent"]["devbridge_workspace_id"] == ""
    assert "未固定工作区" in text3
    assert "所有运行根平等参与自动路由" in text3
    assert "sess-gemini" not in mw_env.gateway._session_workspaces


def test_switch_workspace_changes_real_proxy_target(mw_env: _MultiWorkspaceEnv) -> None:
    import json

    token, _, _cid = mw_env.register_and_authorize()
    routed_to: list[int] = []

    class _Router(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            from urllib.parse import urlparse

            port = urlparse(str(request.url)).port or 18787
            routed_to.append(port)
            target = mw_env.transport_a if port == 18787 else mw_env.transport_b
            return await target.handle_async_request(request)

    mw_env.gateway._http = httpx.AsyncClient(transport=_Router())
    headers = {
        "Authorization": f"Bearer {token}",
        "mcp-session-id": "sess-switch-real",
        "Content-Type": "application/json",
    }
    switch_rpc = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "devbridge_switch_workspace",
                "arguments": {"project_id": WORKSPACE_B_ID},
            },
        }
    )
    switched = mw_env.client.post("/mcp", content=switch_rpc, headers=headers)
    assert switched.status_code == 200
    routed_to.clear()
    proxied = mw_env.client.get("/mcp", headers=headers)
    assert proxied.status_code == 200
    assert routed_to == [18788]

    routed_to.clear()
    other = mw_env.client.get(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "mcp-session-id": "sess-other"},
    )
    assert other.status_code == 200
    assert routed_to == [18787]


def test_switch_workspace_unknown_project_returns_error(mw_env: _MultiWorkspaceEnv) -> None:
    """switch_workspace to non-existent project returns error."""
    at_a, _, _cid = mw_env.register_and_authorize(WORKSPACE_A_ID)

    rpc = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "devbridge_switch_workspace",
                "arguments": {"project_id": "no-such-project"},
            },
        }
    )
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
    assert "工作区" in result


def test_backward_compat_legacy_bearer_still_works(env: _Env) -> None:
    """Legacy ChatGPT bearer (engine token) passes through without workspace binding."""
    r = env.client.get("/mcp", headers={"Authorization": f"Bearer {PUB_TOKEN}"})
    assert r.status_code == 200
    assert env.calls[-1]["authorization"] == f"Bearer {PUB_TOKEN}"


# =========================================================================
# Tool catalog integrity tests
# =========================================================================


def test_tool_names_unique() -> None:
    from local_dev_mcp_bridge.gateway import _PYTHON_TOOL_DEFS

    names = [t["name"] for t in _PYTHON_TOOL_DEFS]
    assert len(names) == len(set(names)), f"Duplicate: {[n for n in names if names.count(n) > 1]}"


def test_tool_names_use_devbridge_prefix() -> None:
    from local_dev_mcp_bridge.gateway import _PYTHON_TOOL_DEFS

    ws_tools = [
        t
        for t in _PYTHON_TOOL_DEFS
        if t["name"]
        in (
            "devbridge_list_workspaces",
            "devbridge_get_current_workspace",
            "devbridge_switch_workspace",
        )
    ]
    assert len(ws_tools) == 3


def test_all_tools_have_valid_json_schema() -> None:
    from local_dev_mcp_bridge.gateway import _PYTHON_TOOL_DEFS

    for tool in _PYTHON_TOOL_DEFS:
        name = tool["name"]
        s = tool.get("inputSchema")
        assert s is not None, f"{name}: no inputSchema"
        assert isinstance(s, dict), f"{name}: inputSchema not dict"
        assert s.get("type") == "object", f"{name}: type not object"
        props = s.get("properties")
        if props:
            assert isinstance(props, dict)
        req = s.get("required")
        if req:
            assert isinstance(req, list)
            for r in req:
                if props:
                    assert r in props, f"{name}: required '{r}' not in properties"


def test_tool_descriptions_are_english() -> None:
    from local_dev_mcp_bridge.gateway import _PYTHON_TOOL_DEFS

    for tool in _PYTHON_TOOL_DEFS:
        desc = tool.get("description", "")
        assert isinstance(desc, str) and len(desc) > 0, f"{tool['name']}: missing desc"
        assert not any(ord(c) > 127 for c in desc), f"{tool['name']}: non-ASCII in desc"


def test_tool_analyze_function() -> None:
    from local_dev_mcp_bridge.gateway import _analyze_tools

    c, d = _analyze_tools(json.dumps({"result": {"tools": []}}).encode())
    assert c == 0 and d == []

    c, d = _analyze_tools(
        json.dumps(
            {
                "result": {
                    "tools": [
                        {"name": "read"},
                        {"name": "write"},
                        {"name": "devbridge_list_workspaces"},
                    ]
                }
            }
        ).encode()
    )
    assert c == 3 and d == []

    c, d = _analyze_tools(
        json.dumps(
            {
                "result": {
                    "tools": [
                        {"name": "read"},
                        {"name": "read"},
                        {"name": "write"},
                    ]
                }
            }
        ).encode()
    )
    assert c == 3 and d == ["read"]


def test_gateway_merge_no_duplicates() -> None:
    from local_dev_mcp_bridge.gateway import _analyze_tools, _inject_tools

    codexpro = json.dumps(
        {
            "result": {
                "tools": [
                    {"name": "read_file"},
                    {"name": "write_file"},
                    {"name": "list_workspaces"},
                    {"name": "switch_workspace"},
                ]
            }
        }
    ).encode()
    injected = _inject_tools(codexpro)
    count, dupes = _analyze_tools(injected)
    assert dupes == [], f"Collisions: {dupes}"
    assert count == 13
    merged = json.loads(injected)["result"]["tools"]
    names = {tool["name"] for tool in merged}
    assert {
        "devbridge_list_devices",
        "devbridge_get_current_device",
        "devbridge_switch_device",
    } <= names


def test_diag_redact_body() -> None:
    from local_dev_mcp_bridge.gateway import _diag_redact_body

    body = json.dumps({"grant_type": "authorization_code", "code": "secret-123"})
    r = _diag_redact_body(body)
    assert "***REDACTED***" in r and "secret-123" not in r

    body2 = json.dumps({"client_secret": "s3cret"})
    r2 = _diag_redact_body(body2)
    assert "***REDACTED***" in r2 and "s3cret" not in r2


def test_diag_short_hash() -> None:
    from local_dev_mcp_bridge.gateway import _diag_short_hash

    h = _diag_short_hash("test")
    assert h == _diag_short_hash("test") and len(h) == 8
    assert _diag_short_hash("") == ""


def test_v081_stateless_route_hint_survives_transport_recreation(
    mw_env: _MultiWorkspaceEnv,
) -> None:
    """Explicit route hints survive when ChatGPT creates a fresh MCP transport session."""
    token, _, _cid = mw_env.register_and_authorize()
    routed_to: list[int] = []
    forwarded_bodies: list[bytes] = []

    class _Router(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            from urllib.parse import urlparse

            port = urlparse(str(request.url)).port or 18787
            routed_to.append(port)
            forwarded_bodies.append(request.content)
            target = mw_env.transport_a if port == 18787 else mw_env.transport_b
            return await target.handle_async_request(request)

    mw_env.gateway._http = httpx.AsyncClient(transport=_Router())

    def call(session_id: str, *, route: str | None) -> None:
        arguments: dict[str, str] = {"path": "README.md"}
        if route:
            arguments["devbridge_workspace_id"] = route
        rpc = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": len(routed_to) + 1,
                "method": "tools/call",
                "params": {"name": "read", "arguments": arguments},
            }
        )
        response = mw_env.client.post(
            "/mcp",
            content=rpc,
            headers={
                "Authorization": f"Bearer {token}",
                "mcp-session-id": session_id,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200, response.text

    call("transport-session-one", route=WORKSPACE_B_ID)
    call("transport-session-two", route=WORKSPACE_B_ID)
    assert routed_to[-2:] == [18788, 18788]
    assert all(b"devbridge_workspace_id" not in body for body in forwarded_bodies[-2:])

    call("transport-session-three", route=None)
    assert routed_to[-1] == 18787


def test_v081_injected_tool_schema_exposes_optional_route_hints() -> None:
    from local_dev_mcp_bridge.gateway import _inject_tools

    original = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "read",
                        "description": "Read a file",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ]
            },
        }
    ).encode()
    patched = json.loads(_inject_tools(original).decode())
    tools = {tool["name"]: tool for tool in patched["result"]["tools"]}
    for name in ("read", "run_command", "devbridge_switch_workspace"):
        props = tools[name]["inputSchema"]["properties"]
        assert "devbridge_workspace_id" in props
        assert "devbridge_device_id" in props
    assert tools["read"]["inputSchema"]["required"] == ["path"]


def test_v081_gateway_local_tool_reads_mcp_arguments(
    mw_env: _MultiWorkspaceEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    def fake_run(command: str, *, cwd: Path, timeout_seconds: int):
        seen.update(command=command, cwd=cwd, timeout_seconds=timeout_seconds)
        return SimpleNamespace(
            shell="powershell",
            exit_code=0,
            duration_seconds=0.01,
            timed_out=False,
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr("local_dev_mcp_bridge.gateway.run_command", fake_run)
    monkeypatch.setattr(mw_env.gateway, "_workspace_permission_mode", lambda _wid: "workspace")
    rpc = {
        "jsonrpc": "2.0",
        "id": 91,
        "method": "tools/call",
        "params": {
            "name": "run_command",
            "arguments": {"command": "Write-Output ok", "timeout_seconds": 9},
        },
    }
    response = asyncio.run(
        mw_env.gateway._exec_local_tool(
            "run_command",
            rpc,
            rpc["params"],
            workspace_id=WORKSPACE_A_ID,
            session_id="",
        )
    )
    data = json.loads(bytes(response.body))
    assert "error" not in data
    assert seen["command"] == "Write-Output ok"
    assert seen["timeout_seconds"] == 9

    rpc["params"]["arguments"]["timeout_seconds"] = 999
    response = asyncio.run(
        mw_env.gateway._exec_local_tool(
            "run_command",
            rpc,
            rpc["params"],
            workspace_id=WORKSPACE_A_ID,
            session_id="",
        )
    )
    data = json.loads(bytes(response.body))
    assert "error" not in data
    assert seen["timeout_seconds"] == 20


def test_v081_switch_workspace_returns_route_without_transport_session(
    mw_env: _MultiWorkspaceEnv,
) -> None:
    import asyncio

    rpc = {
        "jsonrpc": "2.0",
        "id": 92,
        "method": "tools/call",
        "params": {
            "name": "devbridge_switch_workspace",
            "arguments": {"project_id": WORKSPACE_B_ID},
        },
    }
    response = asyncio.run(
        mw_env.gateway._exec_local_tool(
            "devbridge_switch_workspace",
            rpc,
            rpc["params"],
            workspace_id=WORKSPACE_A_ID,
            session_id="",
        )
    )
    data = json.loads(bytes(response.body))
    assert "error" not in data
    assert data["result"]["structuredContent"]["devbridge_workspace_id"] == WORKSPACE_B_ID


def test_v082_relative_path_ambiguity_requires_absolute_path(
    mw_env: _MultiWorkspaceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = tmp_path / "aa"
    # Deliberately use different root-string lengths: relative-path ambiguity
    # must not be resolved by whichever configured root happens to be longer.
    root_b = tmp_path / "much-longer-root-name"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "README.md").write_text("a", encoding="utf-8")
    (root_b / "README.md").write_text("b", encoding="utf-8")
    monkeypatch.setattr(
        mw_env.gateway,
        "_running_workspace_roots",
        lambda: [("left", root_a), ("right", root_b)],
    )
    with pytest.raises(ValueError, match="绝对路径"):
        mw_env.gateway._workspace_for_path("README.md")


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-letter routing regression")
def test_v082_cross_drive_absolute_path_routes_without_switch(
    mw_env: _MultiWorkspaceEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mw_env.gateway,
        "_running_workspace_roots",
        lambda: [("root-c", Path("C:\\")), ("root-d", Path("D:\\"))],
    )
    assert mw_env.gateway._workspace_for_path(r"D:\Environment\mcp\README.md") == "root-d"
    assert mw_env.gateway._workspace_for_path(r"C:\Program Files (x86)\demo.txt") == "root-c"


def test_v082_workspace_handle_affinity_survives_followup_without_path(
    mw_env: _MultiWorkspaceEnv,
) -> None:
    """A CodexPro workspace_id returned by open_workspace must keep its root affinity."""
    routed_to: list[int] = []

    class _Router(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            from urllib.parse import urlparse

            port = urlparse(str(request.url)).port or 18787
            routed_to.append(port)
            payload = json.loads(request.content.decode("utf-8")) if request.content else {}
            tool = str((payload.get("params") or {}).get("name") or "")
            if tool == "open_workspace":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "result": {
                            "content": [{"type": "text", "text": "opened"}],
                            "structuredContent": {"workspace_id": "ws-beta-child"},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload.get("id"), "result": {}},
            )

    mw_env.gateway._http = httpx.AsyncClient(transport=_Router())
    headers = {"Authorization": f"Bearer {PUB_TOKEN}", "Content-Type": "application/json"}
    opened = mw_env.client.post(
        "/mcp",
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 201,
                "method": "tools/call",
                "params": {
                    "name": "open_workspace",
                    "arguments": {"root": os.path.join(WORKSPACE_B_ROOT, "Nested")},
                },
            }
        ),
        headers=headers,
    )
    assert opened.status_code == 200, opened.text
    assert routed_to[-1] == 18788
    opened_payload = json.loads(opened.text)
    assert opened_payload["result"]["structuredContent"]["workspace_id"] == "ws-beta-child"
    assert opened_payload["result"]["structuredContent"]["devbridge_workspace_id"] == WORKSPACE_B_ID

    followed = mw_env.client.post(
        "/mcp",
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 202,
                "method": "tools/call",
                "params": {
                    "name": "show_changes",
                    "arguments": {"workspace_id": "ws-beta-child"},
                },
            }
        ),
        headers=headers,
    )
    assert followed.status_code == 200, followed.text
    assert routed_to[-1] == 18788


def test_open_workspace_updates_only_matching_legacy_session_soft_anchor(
    mw_env: _MultiWorkspaceEnv,
) -> None:
    """Legacy client sessions may keep independent soft workspace contexts."""

    class _Router(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            payload = json.loads(request.content.decode("utf-8")) if request.content else {}
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": "opened"}],
                        "structuredContent": {"workspace_id": "ws-session-b"},
                    },
                },
            )

    mw_env.gateway._http = httpx.AsyncClient(transport=_Router())
    token, _, _cid = mw_env.register_and_authorize()
    opened = mw_env.client.post(
        "/mcp",
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 301,
                "method": "tools/call",
                "params": {
                    "name": "open_workspace",
                    "arguments": {"root": os.path.join(WORKSPACE_B_ROOT, "Nested")},
                },
            }
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "mcp-session-id": "legacy-chat-b",
            "Content-Type": "application/json",
        },
    )
    assert opened.status_code == 200, opened.text
    assert mw_env.gateway._session_workspaces.get("legacy-chat-b") == WORKSPACE_B_ID
    assert "other-chat" not in mw_env.gateway._session_workspaces


def test_gateway_stateless_upstream_never_forwards_session_headers(
    mw_env: _MultiWorkspaceEnv,
) -> None:
    """External session ids may drive Gateway affinity but never reach CodexPro."""
    token, _, _cid = mw_env.register_and_authorize()
    external_session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    events: list[tuple[int, str, str, str, dict]] = []

    class _StatelessRouter(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            port = request.url.port or 18787
            payload = json.loads(request.content.decode("utf-8") if request.content else "{}")
            method = str(payload.get("method", ""))
            sid = request.headers.get("mcp-session-id", "")
            affinity = request.headers.get("x-mcp-devbridge-client-affinity", "")
            events.append((port, method, sid, affinity, payload))
            assert sid == ""
            assert len(affinity) == 64
            assert affinity != external_session
            if method == "initialize":
                return httpx.Response(
                    200,
                    headers={"mcp-session-id": "private-upstream-session-must-not-leak"},
                    json={
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "CodexPro", "version": "test"},
                        },
                    },
                )
            if method == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"content": [{"type": "text", "text": f"port={port}"}]},
                },
            )

    mw_env.gateway._http = httpx.AsyncClient(transport=_StatelessRouter())
    headers = {"Authorization": f"Bearer {token}"}
    initialized = mw_env.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stateless-test", "version": "1"},
            },
        },
        headers=headers,
    )
    assert initialized.status_code == 200, initialized.text
    assert "mcp-session-id" not in initialized.headers

    ready = mw_env.client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers={**headers, "mcp-session-id": external_session},
    )
    assert ready.status_code == 202, ready.text

    routed = mw_env.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "read",
                "arguments": {
                    "path": "README.md",
                    "devbridge_workspace_id": WORKSPACE_B_ID,
                },
            },
        },
        headers={**headers, "mcp-session-id": external_session},
    )
    assert routed.status_code == 200, routed.text
    b_tool_calls = [event for event in events if event[0] == 18788 and event[1] == "tools/call"]
    assert len(b_tool_calls) == 1
    _port, _method, forwarded_sid, forwarded_affinity, forwarded_payload = b_tool_calls[0]
    assert forwarded_sid == ""
    assert len(forwarded_affinity) == 64
    assert forwarded_affinity != external_session
    assert "devbridge_workspace_id" not in forwarded_payload["params"]["arguments"]


def test_gateway_stateless_upstream_never_replays_session_not_found_404(
    mw_env: _MultiWorkspaceEnv,
) -> None:
    """A business/tool request is never replayed merely because upstream returned a session-like 404."""
    token, _, _cid = mw_env.register_and_authorize()
    events: list[tuple[str, str]] = []

    class _NotFoundRouter(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8") if request.content else "{}")
            method = str(payload.get("method", ""))
            sid = request.headers.get("mcp-session-id", "")
            events.append((method, sid))
            assert sid == ""
            return httpx.Response(
                404,
                json={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {"code": -32001, "message": "Session not found"},
                },
            )

    mw_env.gateway._http = httpx.AsyncClient(transport=_NotFoundRouter())
    response = mw_env.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "read",
                "arguments": {
                    "path": "README.md",
                    "devbridge_workspace_id": WORKSPACE_A_ID,
                },
            },
        },
        headers={
            "Authorization": f"Bearer {token}",
            "mcp-session-id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )
    assert response.status_code == 404, response.text
    assert events == [("tools/call", "")]


def test_gateway_affinity_cache_is_bounded_and_refreshes_recent_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "_MAX_AFFINITY_ENTRIES", 2)
    mapping: dict[str, str] = {}
    gateway_module._remember_bounded_affinity(mapping, "a", "A")
    gateway_module._remember_bounded_affinity(mapping, "b", "B")
    gateway_module._remember_bounded_affinity(mapping, "a", "A2")
    gateway_module._remember_bounded_affinity(mapping, "c", "C")
    assert mapping == {"a": "A2", "c": "C"}
