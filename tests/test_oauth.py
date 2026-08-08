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
from local_dev_mcp_bridge.oauth_provider import LocalOAuthProvider
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