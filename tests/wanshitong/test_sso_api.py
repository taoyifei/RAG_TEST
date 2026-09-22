"""湾事通 SSO 浏览器流、公共 Facade 与管理员隔离回归。"""

from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_app.wanshitong.public_api import (
    PUBLIC_CAPABILITIES_PATH,
    PUBLIC_SESSION_PATH,
)
from rag_app.wanshitong.sso_client import SsoClient, SsoValidationError
from rag_app.wanshitong.sso_session import SsoIdentity, SsoSessionService
from tests.product_support import ProductHarness, build_product_harness

_ORIGIN = "http://10.242.180.54:8289"
_CALLBACK = f"{_ORIGIN}/kb/sso/callback"
_TICKET = "a" * 32


@dataclass(slots=True)
class SsoHarness:
    """组合 Product 测试宿主与同源浏览器客户端。"""

    product: ProductHarness
    browser: TestClient

    @property
    def sessions(self) -> SsoSessionService:
        """返回当前应用实际安装的 SSO 会话服务。"""
        service = self.app.state.wanshitong_public_session_service
        if not isinstance(service, SsoSessionService):
            raise AssertionError("测试未启用 SSO 会话服务。")
        return service

    @property
    def app(self) -> FastAPI:
        """返回当前 Product FastAPI 应用。"""
        app = self.product.client.app
        if not isinstance(app, FastAPI):
            raise AssertionError("测试 Product App 类型错误。")
        return app


@pytest.fixture
def sso_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[SsoHarness, None, None]:
    secret = tmp_path / "sso-client-secret"
    secret.write_text("synthetic-sso-client-secret", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    monkeypatch.setenv("RAG_ROOT_PATH", "/kb")
    monkeypatch.setenv("RAG_WANSHITONG_DEMO_ALLOW_HTTP", "true")
    monkeypatch.setenv("RAG_WANSHITONG_AUTH_MODE", "sso")
    monkeypatch.setenv(
        "RAG_WANSHITONG_SSO_DEPLOYMENT_ID", "candidate_8289"
    )
    monkeypatch.setenv(
        "RAG_WANSHITONG_SSO_ENTRIES",
        json.dumps(
            [
                {
                    "id": "internal",
                    "origin": _ORIGIN,
                    "authorize_url": "http://rdms.test/sso/authorize",
                }
            ]
        ),
    )
    monkeypatch.setenv(
        "RAG_WANSHITONG_SSO_VALIDATE_URL",
        "http://rdms-backend.test:21000/sso/validate",
    )
    monkeypatch.setenv("RAG_WANSHITONG_SSO_CLIENT_ID", "kb")
    monkeypatch.setenv(
        "RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE", str(secret)
    )
    product = build_product_harness(
        tmp_path,
        root_path="/kb",
        trusted_origins=(_ORIGIN,),
    )
    browser = TestClient(product.client.app, base_url=_ORIGIN)
    try:
        yield SsoHarness(product=product, browser=browser)
    finally:
        browser.close()
        product.close()


def _identity() -> SsoIdentity:
    return SsoIdentity(
        user_id="1001",
        username="tester",
        nick_name="测试用户",
        dept_id="42",
        roles=("admin",),
        permissions=("*:*:*",),
    )


def _entry(harness: SsoHarness, return_to: str = "/kb/") -> str:
    response = harness.browser.get(
        "/kb/sso/entry",
        params={"return_to": return_to},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert "path=/kb/sso" in response.headers["set-cookie"].lower()
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert query["service"] == [_CALLBACK]
    assert len(query["state"][0]) == 64
    return cast(str, query["state"][0])


def _login(
    harness: SsoHarness, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    state = _entry(harness, "/kb/admin/history")

    def _validate(
        _client: SsoClient,
        *,
        ticket: str,
        service: str,
        state: str,
    ) -> SsoIdentity:
        assert ticket == _TICKET
        assert service == _CALLBACK
        assert len(state) == 64
        return _identity()

    monkeypatch.setattr(SsoClient, "validate", _validate)
    callback = harness.browser.get(
        "/kb/sso/callback",
        params={"ticket": _TICKET, "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/kb/admin/history"
    bootstrap = harness.browser.post(f"/kb{PUBLIC_SESSION_PATH}")
    bootstrap.raise_for_status()
    return cast(dict[str, object], bootstrap.json())


def test_sso_entry_callback_bootstrap_and_admin_isolation(
    sso_harness: SsoHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _login(sso_harness, monkeypatch)

    assert session["user"] == {
        "user_id": "1001",
        "display_name": "测试用户",
    }
    assert session["deployment_id"] == "candidate_8289"
    principal = sso_harness.sessions.authenticate(
        sso_harness.browser.cookies[
            "kb_user_session_candidate_8289"
        ].strip('"'),
        str(session["csrf_token"]),
    )
    assert principal.owner_id == "rdms:1001"
    capabilities = sso_harness.browser.get(
        f"/kb{PUBLIC_CAPABILITIES_PATH}"
    )
    assert capabilities.status_code == 200

    denied = sso_harness.browser.get(
        "/kb/api/v1/admin/wanshitong/scope"
    )
    assert denied.status_code == 401


def test_wrong_state_never_calls_idp_or_clears_current_pending(
    sso_harness: SsoHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _entry(sso_harness)
    pending_before = sso_harness.browser.cookies[
        sso_harness.sessions.pending_cookie_name
    ]

    def _unexpected(*_args: object, **_kwargs: object) -> SsoIdentity:
        raise AssertionError("错误 state 不得调用 IdP。")

    monkeypatch.setattr(SsoClient, "validate", _unexpected)
    response = sso_harness.browser.get(
        "/kb/sso/callback",
        params={"ticket": _TICKET, "state": "wrong"},
    )

    assert response.status_code == 403
    assert state not in response.text
    assert (
        sso_harness.browser.cookies[
            sso_harness.sessions.pending_cookie_name
        ]
        == pending_before
    )


def test_failed_new_callback_preserves_existing_user_session(
    sso_harness: SsoHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    established = _login(sso_harness, monkeypatch)
    user_cookie = sso_harness.browser.cookies[
        sso_harness.sessions.cookie_name
    ]
    state = _entry(sso_harness)

    def _replayed(
        _client: SsoClient, **_kwargs: str
    ) -> SsoIdentity:
        raise SsoValidationError(
            "AUTH_LOGIN_EXPIRED",
            "登录已失效，请重新登录。",
            status_code=429,
        )

    monkeypatch.setattr(SsoClient, "validate", _replayed)
    failed = sso_harness.browser.get(
        "/kb/sso/callback",
        params={"ticket": _TICKET, "state": state},
    )

    assert failed.status_code == 429
    assert sso_harness.browser.cookies[sso_harness.sessions.cookie_name] == (
        user_cookie
    )
    resumed = sso_harness.browser.post(f"/kb{PUBLIC_SESSION_PATH}")
    assert resumed.status_code == 200
    assert resumed.json()["session_id"] == established["session_id"]


def test_logout_is_local_csrf_protected_and_does_not_touch_admin(
    sso_harness: SsoHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _login(sso_harness, monkeypatch)

    missing_csrf = sso_harness.browser.post("/kb/sso/logout")
    assert missing_csrf.status_code == 401
    logged_out = sso_harness.browser.post(
        "/kb/sso/logout",
        headers={"X-CSRF-Token": str(session["csrf_token"])},
    )
    assert logged_out.status_code == 200
    assert logged_out.json() == {
        "status": "logged_out",
        "scope": "kb_local",
    }
    assert (
        sso_harness.browser.post(f"/kb{PUBLIC_SESSION_PATH}").status_code
        == 401
    )
    assert (
        sso_harness.product.client.get(
                "/kb/api/v1/admin/wanshitong/scope"
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "return_to",
    [
        "https://attacker.test/kb/",
        "//attacker.test/kb/",
        "/kb/../outside",
        "/kb/%2e%2e/outside",
        "/kb/sso/callback",
        "/outside",
    ],
)
def test_entry_rejects_open_redirects(
    sso_harness: SsoHarness, return_to: str
) -> None:
    response = sso_harness.browser.get(
        "/kb/sso/entry", params={"return_to": return_to}
    )

    assert response.status_code == 400
    assert "attacker.test" not in response.headers.get("location", "")


def test_entry_rejects_unknown_host_and_ignores_untrusted_forwarding(
    sso_harness: SsoHarness,
) -> None:
    unknown = sso_harness.browser.get(
        "/kb/sso/entry",
        headers={"Host": "attacker.test"},
        follow_redirects=False,
    )
    assert unknown.status_code == 400
    assert "location" not in unknown.headers

    direct = sso_harness.browser.get(
        "/kb/sso/entry",
        headers={
            "X-Forwarded-Host": "attacker.test",
            "X-Forwarded-Proto": "https",
        },
        follow_redirects=False,
    )
    assert direct.status_code == 302
    service = parse_qs(urlsplit(direct.headers["location"]).query)[
        "service"
    ]
    assert service == [_CALLBACK]


def test_callback_rejects_duplicate_query_without_calling_idp(
    sso_harness: SsoHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _entry(sso_harness)

    def _unexpected(*_args: object, **_kwargs: object) -> SsoIdentity:
        raise AssertionError("重复 query 参数不得调用 IdP。")

    monkeypatch.setattr(SsoClient, "validate", _unexpected)
    response = sso_harness.browser.get(
        f"/kb/sso/callback?ticket={_TICKET}&ticket={'b' * 32}"
        f"&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 400
