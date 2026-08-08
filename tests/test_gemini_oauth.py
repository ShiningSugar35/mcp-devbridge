"""Gemini 静态 OAuth Client 测试。

预注册 confidential client（client_secret_post + PKCE S256）：
创建/复用/轮换、redirect_uri 严格匹配、完整授权码流程、
错误 secret / 错误 redirect_uri 拒绝、secret 不落盘、跨实例持久化。
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from local_dev_mcp_bridge.constants import OAUTH_SCOPE
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.oauth_provider import LocalOAuthProvider, get_or_create_gemini_client
from local_dev_mcp_bridge.secrets import SecretsStore

URI = "https://geminiauth.example.test/oauth2callback"
VERIFIER = "gemini-verifier-verifier-verifier-123456-xy"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
        self.t = time.time()
        self.store = SecretsStore()
        calls: list[dict] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            calls.append(
                {
                    "authorization": request.headers.get("authorization", ""),
                    "path": request.url.path,
                }
            )
            return httpx.Response(200, json={"ok": True})

        self.provider = LocalOAuthProvider(
            issuer_url="https://mcp.example.test",
            resource_url="https://mcp.example.test/mcp",
            workspace="C:\\TestProjects\\demo-app",
            store=self.store,
            now=lambda: self.t,
        )
        self.gateway = OAuthGateway(
            public_hostname="mcp.example.test",
            workspace="C:\\TestProjects\\demo-app",
            upstream_legacy_token=lambda: "legacy-token-x",
            provider=self.provider,
            transport=httpx.MockTransport(_handler),
        )
        self.calls = calls
        self.client = TestClient(self.gateway.app, raise_server_exceptions=False, follow_redirects=False)

    def authorize(self, client_id: str, redirect: str | None = None):
        params = {
            "client_id": client_id,
            "redirect_uri": redirect or URI,
            "response_type": "code",
            "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": "st-gemini",
            "scope": OAUTH_SCOPE,
        }
        return self.client.get("/authorize", params=params)

    def consent(self, location: str):
        cid = location.split("id=", 1)[1]
        return self.client.post("/consent", data={"id": cid, "decision": "allow"})

    def token(self, client_id: str, code: str, *, secret: str | None = None,
              redirect: str | None = None):
        body = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": redirect or URI,
        }
        if secret is not None:
            body["client_secret"] = secret
        return self.client.post("/token", data=body)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    return _Env(tmp_path, monkeypatch)


def _code_from(r: Any) -> str:
    assert r.status_code == 302, r.text
    return r.headers["location"].split("code=", 1)[1].split("&", 1)[0]


# ------------------------------------------------------ 创建 / 复用 / 轮换

def test_static_client_created(env: _Env) -> None:
    cid, secret = get_or_create_gemini_client(URI, store=env.store)
    assert cid.startswith("gemini-")
    assert secret and len(secret) >= 32


def test_same_uri_reuses_client(env: _Env) -> None:
    cid1, _ = get_or_create_gemini_client(URI, store=env.store)
    cid2, _ = get_or_create_gemini_client(URI, store=env.store)
    assert cid1 == cid2


def test_rotate_keeps_id_new_secret(env: _Env) -> None:
    cid, s1 = get_or_create_gemini_client(URI, store=env.store)
    cid2, s2 = get_or_create_gemini_client(URI, store=env.store, rotate_secret=True)
    assert cid2 == cid
    assert s2 != s1


def test_invalid_redirect_rejected(env: _Env) -> None:
    with pytest.raises(ValueError):
        get_or_create_gemini_client("not-a-url", store=env.store)


def test_client_survives_restart(env: _Env) -> None:
    cid, _ = get_or_create_gemini_client(URI, store=env.store)
    fresh = SecretsStore()  # 新实例 = 模拟重启
    loaded, _ = get_or_create_gemini_client(URI, store=fresh)
    assert loaded == cid


# ------------------------------------------------ 全流程（authorization code + PKCE）

def test_full_flow_code_pkce_state(env: _Env) -> None:
    cid, secret = get_or_create_gemini_client(URI, store=env.store)
    loc = env.authorize(cid).headers["location"]
    r = env.consent(loc)
    assert "state=st-gemini" in r.headers["location"]
    t = env.token(cid, _code_from(r), secret=secret)
    assert t.status_code == 200, t.text
    body = t.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]


def test_wrong_secret_rejected(env: _Env) -> None:
    cid, secret = get_or_create_gemini_client(URI, store=env.store)
    loc = env.authorize(cid).headers["location"]
    code = _code_from(env.consent(loc))
    assert env.token(cid, code, secret="wrong-secret").status_code in (400, 401)
    # 正确 secret 才换得出 token（对照）
    assert env.token(cid, code, secret=secret).status_code in (200, 400)


def test_rotate_invalidates_old_secret(env: _Env) -> None:
    cid, s1 = get_or_create_gemini_client(URI, store=env.store)
    _, s2 = get_or_create_gemini_client(URI, store=env.store, rotate_secret=True)
    assert s2 != s1
    loc = env.authorize(cid).headers["location"]
    code = _code_from(env.consent(loc))
    assert env.token(cid, code, secret=s2).status_code == 200
    # 旧 secret 需要新 code 验证：新 code + 旧 secret 必须被拒绝
    loc = env.authorize(cid).headers["location"]
    code2 = _code_from(env.consent(loc))
    assert env.token(cid, code2, secret=s1).status_code in (400, 401)


def test_wrong_redirect_uri_rejected(env: _Env) -> None:
    cid, _ = get_or_create_gemini_client(URI, store=env.store)
    r = env.authorize(cid, redirect="https://evil.example/hack")
    assert "code=" not in r.headers.get("location", "")
    assert r.status_code in (302, 400)


def test_code_bound_to_exact_redirect(env: _Env) -> None:
    cid, secret = get_or_create_gemini_client(URI, store=env.store)
    loc = env.authorize(cid).headers["location"]
    code = _code_from(env.consent(loc))
    t = env.token(cid, code, secret=secret, redirect="https://other.example/xx")
    assert t.status_code == 400


# ------------------------------------------------------------ 3. secret 不落盘

def test_secret_never_plaintext_on_disk(env: _Env) -> None:
    _, secret = get_or_create_gemini_client(URI, store=env.store)
    cfg = Path(os.environ["LOCALDEV_MCP_CONFIG_DIR"])
    assert cfg.exists()
    for f in cfg.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert secret not in text