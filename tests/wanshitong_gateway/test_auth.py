"""独立网关复用 RDMS SSO 与候选管理员会话的合同测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from wanshitong_gateway.auth import GatewayAuth, register_auth_routes
from wanshitong_gateway.compat.wanshitong.sso_client import SsoClient
from wanshitong_gateway.compat.wanshitong.sso_session import (
    SsoIdentity,
    SsoSessionService,
)
from wanshitong_gateway.roles import Role, UserProfile
from wanshitong_gateway.settings import GatewayAuthSettings
from wanshitong_gateway.weknora.client import NativeHttpError

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


def _login_public(
    browser: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    username: str = "tester",
    user_id: str = "1001",
    dept_id: str | None = None,
) -> dict:
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
            user_id=user_id,
            username=username,
            nick_name="测试用户",
            dept_id=dept_id,
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
        "username": "tester",
        "dept_id": None,
        "is_super_admin": False,
        "role": "user",
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


def test_taoyifei_sso_admin_uses_console_and_keeps_department(
    gateway: tuple[GatewayAuth, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, browser = gateway
    session = _login_public(
        browser, monkeypatch, username="taoyifei", user_id="325", dept_id="42"
    )
    assert session["user"]["role"] == "admin"
    assert session["user"]["is_super_admin"] is True
    assert session["user"]["dept_id"] == "42"
    assert auth.users is not None
    assert auth.users.get("325").dept_id == "42"
    assert auth.settings.admin_cookie_name not in browser.cookies

    resumed = browser.get("/kb/api/v1/console/session")
    assert resumed.status_code == 200
    assert resumed.json()["authenticated"] is True
    assert resumed.json()["csrf_token"] == session["csrf_token"]
    assert resumed.json()["session_id"] == session["session_id"]
    assert auth.settings.admin_cookie_name not in browser.cookies

    headers = {"Origin": _ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    assert (
        browser.post("/kb/api/v1/console/session", headers=headers).status_code
        == 200
    )
    assert (
        browser.post(
            "/kb/api/v1/console/session:rotate", headers=headers
        ).status_code
        == 200
    )
    assert (
        browser.delete(
            "/kb/api/v1/console/session", headers=headers
        ).status_code
        == 204
    )
    assert browser.get("/kb/api/v1/console/session").status_code == 401


def test_local_role_change_applies_to_existing_sso_session(
    gateway: tuple[GatewayAuth, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, browser = gateway
    session = _login_public(browser, monkeypatch)
    assert browser.get("/kb/api/ops/users").status_code == 401
    assert auth.users is not None
    with TestClient(browser.app, base_url=_ORIGIN) as owner_browser:
        owner_session = _login_public(
            owner_browser, monkeypatch, username="taoyifei", user_id="325"
        )
        owner_headers = {
            "Origin": _ORIGIN,
            "X-CSRF-Token": owner_session["csrf_token"],
        }
        assert (
            owner_browser.patch(
                "/kb/api/ops/users/1001",
                json={"role": "admin"},
                headers=owner_headers,
            ).status_code
            == 200
        )
        assert browser.get("/kb/api/v1/console/session").status_code == 200
        listing = browser.get("/kb/api/ops/users")
        assert listing.status_code == 200
        listed = {item["user_id"]: item for item in listing.json()["items"]}
        assert listed["325"]["is_super_admin"] is True
        assert listed["325"]["editable_roles"] == []
        assert listed["1001"]["editable_roles"] == []

        headers = {"Origin": _ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        assert (
            browser.patch(
                "/kb/api/ops/users/1001", json={"role": "hr"}, headers=headers
            ).status_code
            == 403
        )
        assert (
            browser.patch(
                "/kb/api/ops/users/325", json={"role": "user"}, headers=headers
            ).status_code
            == 403
        )
        changed = owner_browser.patch(
            "/kb/api/ops/users/1001", json={"role": "hr"}, headers=owner_headers
        )
        assert changed.status_code == 200
        assert changed.json()["role"] == "hr"
    assert browser.get("/kb/api/v1/console/session").status_code == 401
    assert (
        browser.patch(
            "/kb/api/ops/users/1001", json={"role": "admin"}, headers=headers
        ).status_code
        == 401
    )
    resumed = browser.post(
        "/kb/api/public/session", headers={"Origin": _ORIGIN}
    )
    assert resumed.status_code == 200
    assert resumed.json()["user"]["role"] == "hr"


@pytest.mark.parametrize("sync_fails", [False, True])
def test_role_change_revokes_background_before_saving(
    gateway: tuple[GatewayAuth, TestClient],
    monkeypatch: pytest.MonkeyPatch,
    sync_fails: bool,
) -> None:
    auth, existing_browser = gateway
    owner = _login_public(
        existing_browser, monkeypatch, username="taoyifei", user_id="325"
    )
    assert auth.users is not None
    auth.users.record_identity(
        SsoIdentity(
            user_id="138",
            username="luoyang",
            nick_name="普通用户",
            dept_id="208",
            roles=(),
            permissions=(),
        )
    )
    auth.users.set_role("138", "hr", actor_user_id="325")
    observed: list[tuple[str, str, str]] = []

    async def revoke(target: UserProfile, new_role: Role) -> None:
        assert auth.users is not None
        current = auth.users.get(target.user_id)
        assert current is not None
        observed.append((target.user_id, current.role, new_role))
        if sync_fails:
            raise NativeHttpError(502, "synthetic unavailable")

    app = FastAPI(root_path="/kb")
    register_auth_routes(app, auth, before_role_change=revoke)
    with TestClient(app, base_url=_ORIGIN) as browser:
        browser.cookies.update(existing_browser.cookies)
        result = browser.patch(
            "/kb/api/ops/users/138",
            json={"role": "user"},
            headers={"Origin": _ORIGIN, "X-CSRF-Token": owner["csrf_token"]},
        )
    assert observed == [("138", "hr", "user")]
    assert result.status_code == (503 if sync_fails else 200)
    current = auth.users.get("138")
    assert current is not None
    assert current.role == ("hr" if sync_fails else "user")


def test_denied_role_change_cannot_stop_other_users_workbench(
    gateway: tuple[GatewayAuth, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, existing_browser = gateway
    assert auth.users is not None
    auth.users.record_identity(
        SsoIdentity(
            user_id="325",
            username="taoyifei",
            nick_name="超管",
            dept_id="208",
            roles=(),
            permissions=(),
        )
    )
    actor = _login_public(existing_browser, monkeypatch, user_id="138")
    auth.users.set_role("138", "admin", actor_user_id="325")
    called: list[str] = []

    async def revoke(target: UserProfile, _new_role: Role) -> None:
        called.append(target.user_id)

    app = FastAPI(root_path="/kb")
    register_auth_routes(app, auth, before_role_change=revoke)
    with TestClient(app, base_url=_ORIGIN) as browser:
        browser.cookies.update(existing_browser.cookies)
        result = browser.patch(
            "/kb/api/ops/users/325",
            json={"role": "user"},
            headers={"Origin": _ORIGIN, "X-CSRF-Token": actor["csrf_token"]},
        )
    assert result.status_code == 403
    assert called == []
    assert auth.users.get("325").is_super_admin
