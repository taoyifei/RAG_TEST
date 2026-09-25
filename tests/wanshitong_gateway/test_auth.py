"""独立网关复用 RDMS SSO 与候选管理员会话的合同测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from rag_app.wanshitong.sso_client import SsoClient
from rag_app.wanshitong.sso_session import SsoIdentity, SsoSessionService
from wanshitong_gateway.auth import GatewayAuth, register_auth_routes
from wanshitong_gateway.settings import GatewayAuthSettings

_ORIGIN = "http://candidate.test:8289"
_TICKET = "a" * 32
_BOOTSTRAP_TOKEN = "synthetic-admin-bootstrap-token"  # noqa: S105


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[tuple[GatewayAuth, TestClient]]:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    master = secrets_dir / "master-key"
    master.write_bytes(b"m" * 32)
    master.chmod(0o600)
    bootstrap = secrets_dir / "bootstrap-token"
    bootstrap.write_text(_BOOTSTRAP_TOKEN, encoding="utf-8")
    bootstrap.chmod(0o600)
    sso_secret = secrets_dir / "sso-secret"
    sso_secret.write_text("synthetic-sso-client-secret", encoding="utf-8")
    sso_secret.chmod(0o600)
    settings = GatewayAuthSettings.from_environment(
        {
            "WANSHITONG_GATEWAY_DATA_DIR": str(tmp_path / "gateway-data"),
            "WANSHITONG_GATEWAY_DEPLOYMENT_ID": "candidate_8289",
            "RAG_ROOT_PATH": "/kb",
            "RAG_MASTER_KEY_FILE": str(master),
            "RAG_ADMIN_BOOTSTRAP_TOKEN_FILE": str(bootstrap),
            "RAG_TRUSTED_ORIGINS": _ORIGIN,
            "RAG_WANSHITONG_AUTH_MODE": "sso",
            "RAG_WANSHITONG_SSO_DEPLOYMENT_ID": "candidate_8289",
            "RAG_WANSHITONG_SSO_ENTRIES": json.dumps(
                [
                    {
                        "id": "candidate",
                        "origin": _ORIGIN,
                        "authorize_url": "http://rdms.test/sso/authorize",
                    }
                ]
            ),
            "RAG_WANSHITONG_SSO_VALIDATE_URL": (
                "http://rdms-backend.test/sso/validate"
            ),
            "RAG_WANSHITONG_SSO_CLIENT_ID": "kb",
            "RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE": str(sso_secret),
        }
    )
    auth = GatewayAuth.from_settings(settings)
    app = FastAPI(root_path="/kb")
    register_auth_routes(app, auth)

    @app.post("/api/public/probe")
    def public_probe(request: Request) -> dict[str, str]:
        principal = auth.require_public(request)
        return {"owner_id": principal.owner_id}

    @app.post("/api/engine-admin/probe")
    def admin_probe(request: Request) -> dict[str, str]:
        principal = auth.require_admin(request)
        return {"audit_actor": principal.audit_actor}

    with TestClient(app, base_url=_ORIGIN) as browser:
        yield auth, browser


def _login_public(browser: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict:
    entry = browser.get(
        "/kb/sso/entry",
        params={"return_to": "/kb/"},
        follow_redirects=False,
    )
    assert entry.status_code == 302
    query = parse_qs(urlsplit(entry.headers["location"]).query)
    state = query["state"][0]
    assert query["service"] == [f"{_ORIGIN}/kb/sso/callback"]

    def validate(
        _self: SsoClient, *, ticket: str, service: str, state: str
    ) -> SsoIdentity:
        assert ticket == _TICKET
        assert service == f"{_ORIGIN}/kb/sso/callback"
        assert state
        return SsoIdentity(
            user_id="1001",
            username="tester",
            nick_name="测试用户",
            dept_id=None,
            roles=("admin",),
            permissions=("*:*:*",),
        )

    monkeypatch.setattr(SsoClient, "validate", validate)
    callback = browser.get(
        "/kb/sso/callback",
        params={"ticket": _TICKET, "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    session = browser.post(
        "/kb/api/public/session", headers={"Origin": _ORIGIN}
    )
    assert session.status_code == 200
    return session.json()


def test_sso_public_cookie_does_not_grant_admin(
    gateway: tuple[GatewayAuth, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, browser = gateway
    session = _login_public(browser, monkeypatch)
    assert session["user"] == {
        "user_id": "1001",
        "display_name": "测试用户",
    }
    assert session["deployment_id"] == "candidate_8289"
    public = browser.post(
        "/kb/api/public/probe",
        headers={"Origin": _ORIGIN, "X-CSRF-Token": session["csrf_token"]},
    )
    assert public.status_code == 200
    assert public.json() == {"owner_id": "rdms:1001"}
    assert (
        browser.post(
            "/kb/api/engine-admin/probe",
            headers={"Origin": _ORIGIN, "X-CSRF-Token": session["csrf_token"]},
        ).status_code
        == 401
    )
    assert auth.settings.admin_cookie_name not in browser.cookies


def test_admin_session_is_independent_and_csrf_protected(
    gateway: tuple[GatewayAuth, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, browser = gateway
    public = _login_public(browser, monkeypatch)
    login = browser.post(
        "/kb/api/v1/console/session",
        json={"bootstrap_token": _BOOTSTRAP_TOKEN},
        headers={"Origin": _ORIGIN},
    )
    assert login.status_code == 200
    assert auth.settings.admin_cookie_name in browser.cookies
    assert "rag_console_session" not in browser.cookies
    assert "path=/kb" in login.headers["set-cookie"].lower()
    assert "httponly" in login.headers["set-cookie"].lower()
    assert public["csrf_token"] != login.json()["csrf_token"]

    no_csrf = browser.post(
        "/kb/api/engine-admin/probe", headers={"Origin": _ORIGIN}
    )
    assert no_csrf.status_code == 403
    cross_site = browser.post(
        "/kb/api/engine-admin/probe",
        headers={
            "Origin": "http://other.test:8289",
            "X-CSRF-Token": login.json()["csrf_token"],
        },
    )
    assert cross_site.status_code == 403
    allowed = browser.post(
        "/kb/api/engine-admin/probe",
        headers={"Origin": _ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
    )
    assert allowed.status_code == 200
    assert allowed.json()["audit_actor"].startswith("admin_session:")

    logout = browser.delete(
        "/kb/api/v1/console/session",
        headers={"Origin": _ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
    )
    assert logout.status_code == 204
    assert (
        browser.post(
            "/kb/api/engine-admin/probe",
            headers={
                "Origin": _ORIGIN,
                "X-CSRF-Token": login.json()["csrf_token"],
            },
        ).status_code
        == 401
    )


def test_expired_sso_or_wrong_state_cannot_bootstrap(
    gateway: tuple[GatewayAuth, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, browser = gateway
    entry = browser.get("/kb/sso/entry", follow_redirects=False)
    assert entry.status_code == 302
    wrong_state = browser.get(
        "/kb/sso/callback",
        params={"ticket": _TICKET, "state": "b" * 64},
        follow_redirects=False,
    )
    assert wrong_state.status_code == 403
    assert (
        browser.post(
            "/kb/api/public/session", headers={"Origin": _ORIGIN}
        ).status_code
        == 401
    )
    _login_public(browser, monkeypatch)
    sessions = auth.public_sessions
    assert isinstance(sessions, SsoSessionService)
    now = int(sessions._clock())
    monkeypatch.setattr(sessions, "_clock", lambda: now + 7201)
    assert (
        browser.post(
            "/kb/api/public/session", headers={"Origin": _ORIGIN}
        ).status_code
        == 401
    )


def test_settings_require_isolated_data_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="WANSHITONG_GATEWAY_DATA_DIR"):
        GatewayAuthSettings.from_environment(
            {
                "RAG_ROOT_PATH": "/kb",
                "RAG_MASTER_KEY_FILE": str(tmp_path / "master"),
                "RAG_ADMIN_BOOTSTRAP_TOKEN_FILE": str(tmp_path / "bootstrap"),
            }
        )
