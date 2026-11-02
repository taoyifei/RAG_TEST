"""固定内网后台入口的真实本地角色、单次交接和原会话TTL合同。"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from wanshitong_gateway import admin_entry
from wanshitong_gateway.admin_entry import register_admin_entry_routes
from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)
from wanshitong_gateway.compat.wanshitong.public_session import (
    PublicSessionIssue,
)
from wanshitong_gateway.compat.wanshitong.sso_session import (
    SsoIdentity,
    SsoSessionService,
)
from wanshitong_gateway.roles import Role
from wanshitong_gateway.settings import GatewayAuthSettings

_PUBLIC = "https://public.test"
_INTERNAL = "http://10.242.180.54:18288"
_BOOTSTRAP = "synthetic-admin-bootstrap"
_ENTRIES = (("public", _PUBLIC), ("internal", _INTERNAL))


@dataclass
class _Gateway:
    auth: GatewayAuth
    app: FastAPI
    clock: list[float]


def _gateway(  # noqa: PLR0913 - 仅测试构造器显式覆盖入口、部署及数据隔离。
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: tuple[tuple[str, str], ...] = _ENTRIES,
    deployment: str = "entry_test",
    database_dir: Path | None = None,
    sso_enabled: bool = True,
) -> _Gateway:
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret_paths = {}
    for name, value in (
        ("master", b"m" * 32),
        ("bootstrap", _BOOTSTRAP.encode()),
        ("sso", b"synthetic-sso-client"),
    ):
        path = tmp_path / name
        path.write_bytes(value)
        path.chmod(0o600)
        secret_paths[name] = str(path)
    environment = {
        "WANSHITONG_GATEWAY_DATA_DIR": str(database_dir or tmp_path / "data"),
        "WANSHITONG_GATEWAY_DEPLOYMENT_ID": deployment,
        "RAG_MASTER_KEY_FILE": secret_paths["master"],
        "RAG_ADMIN_BOOTSTRAP_TOKEN_FILE": secret_paths["bootstrap"],
        "RAG_ROOT_PATH": "/kb",
        "RAG_TRUSTED_ORIGINS": ",".join(origin for _, origin in entries),
        "RAG_TRUSTED_PROXIES": "203.0.113.8",
    }
    if sso_enabled:
        environment.update(
            RAG_WANSHITONG_AUTH_MODE="sso",
            RAG_WANSHITONG_SSO_DEPLOYMENT_ID=deployment,
            RAG_WANSHITONG_SSO_ENTRIES=json.dumps(
                [
                    {
                        "id": name,
                        "origin": origin,
                        "authorize_url": "https://rdms.test/sso/authorize",
                    }
                    for name, origin in entries
                ]
            ),
            RAG_WANSHITONG_SSO_VALIDATE_URL="https://rdms.test/sso/validate",
            RAG_WANSHITONG_SSO_CLIENT_ID="kb",
            RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE=secret_paths["sso"],
        )
    auth = GatewayAuth.from_settings(
        GatewayAuthSettings.from_environment(environment)
    )
    clock = [1_700_000_000.0]
    monkeypatch.setattr(admin_entry.time, "time", lambda: clock[0])
    if isinstance(auth.public_sessions, SsoSessionService):
        monkeypatch.setattr(auth.public_sessions, "_clock", lambda: clock[0])
    assert auth.users is not None
    auth.users.record_identity(_identity("325"))
    app = FastAPI(root_path="/kb")
    register_admin_entry_routes(app, auth)

    @app.get("/api/admin/identity")
    def identity(request: Request) -> dict[str, Any]:
        principal = auth.require_public(request, csrf=False)
        administrator = auth.require_admin(request, csrf=False)
        return {
            "user_id": principal.user_id,
            "session_id": principal.session_id,
            "expires_at": principal.expires_at,
            "admin_session_id": administrator.session_id,
        }

    return _Gateway(auth, app, clock)


def _identity(user_id: str) -> SsoIdentity:
    return SsoIdentity(
        user_id=user_id,
        username="taoyifei" if user_id == "325" else "staff_" + user_id,
        nick_name="测试员工" + user_id,
        dept_id="42",
        roles=(),
        permissions=(),
    )


def _issue(gateway: _Gateway, user_id: str, role: Role) -> PublicSessionIssue:
    auth = gateway.auth
    assert auth.users is not None
    identity = _identity(user_id)
    auth.users.record_identity(identity)
    if user_id != "325":
        auth.users.set_role(user_id, role, actor_user_id="325")
    sessions = auth.public_sessions
    assert isinstance(sessions, SsoSessionService)
    return sessions.issue_user(identity)


def _set_user(
    browser: TestClient,
    gateway: _Gateway,
    issue: PublicSessionIssue,
    origin: str,
) -> None:
    browser.cookies.set(
        gateway.auth.public_sessions.cookie_name,
        issue.cookie_value,
        domain=urlsplit(origin).hostname or "",
        path="/",
    )


def _rows(gateway: _Gateway) -> list[dict[str, Any]]:
    factory = SqliteConnectionFactory(gateway.auth.settings.database_path)
    with factory.transaction() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM gateway_admin_handoffs"
            )
        ]


def _ticket(response: httpx.Response) -> str:
    assert response.status_code == 302
    parameters = parse_qs(urlsplit(response.headers["location"]).query)
    assert set(parameters) == {"ticket"}
    return parameters["ticket"][0]


def _private_response(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("user_id", ["325", "201"])
def test_admin_handoff_preserves_identity_cookie_session_and_remaining_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_id: str
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, user_id, "admin")
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        gateway.clock[0] += 10
        start = browser.get("/kb/api/admin/entry", follow_redirects=False)
        ticket = _ticket(start)
        assert start.headers["location"].startswith(
            _INTERNAL + "/kb/api/admin/entry/complete?ticket="
        )
        assert issue.cookie_value not in start.headers["location"]
        row = _rows(gateway)[0]
        assert row["ticket_hash"] == hashlib.sha256(ticket.encode()).hexdigest()
        assert row["cookie_value"] == issue.cookie_value
        assert row["expires_at"] == int(gateway.clock[0]) + 60
        assert row["deployment_id"] == gateway.auth.settings.deployment_id
        assert row["target_origin"] == _INTERNAL
        _private_response(start)
        gateway.clock[0] += 7
        complete = browser.get(
            start.headers["location"], follow_redirects=False
        )
        assert complete.status_code == 303
        assert complete.headers["location"] == _INTERNAL + "/kb/admin/overview"
        _private_response(complete)
        cookie = SimpleCookie()
        for value in complete.headers.get_list("set-cookie"):
            cookie.load(value)
        restored = cookie[gateway.auth.public_sessions.cookie_name]
        assert restored.value == issue.cookie_value
        assert restored["max-age"] == str(issue.expires_in - 17)
        assert restored["path"] == "/"
        assert restored["httponly"]
        assert restored["samesite"] == "lax"
        assert not restored["secure"]
        assert cookie[gateway.auth.settings.admin_cookie_name]["max-age"] == "0"
        assert cookie[gateway.auth.settings.admin_cookie_name]["path"] == "/kb"
        current = browser.get(_INTERNAL + "/kb/api/admin/identity").json()
        assert current == {
            "user_id": user_id,
            "session_id": issue.principal.session_id,
            "expires_at": issue.principal.expires_at,
            "admin_session_id": issue.principal.session_id,
        }
        assert (
            gateway.auth.public_sessions.bootstrap(restored.value).csrf_token
            == issue.csrf_token
        )
        assert not _rows(gateway)


@pytest.mark.parametrize(
    "origin", [_INTERNAL, "http://10.242.180.54:8289", _PUBLIC]
)
def test_unique_internal_entry_redirects_directly_without_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
):
    gateway = _gateway(tmp_path, monkeypatch, entries=(("candidate", origin),))
    issue = _issue(gateway, "325", "admin")
    with TestClient(gateway.app, base_url=origin) as browser:
        _set_user(browser, gateway, issue, origin)
        response = browser.get("/kb/api/admin/entry", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == origin + "/kb/admin/overview"
        _private_response(response)
        cookie = SimpleCookie()
        for value in response.headers.get_list("set-cookie"):
            cookie.load(value)
        assert bool(
            cookie[gateway.auth.public_sessions.cookie_name]["secure"]
        ) == origin.startswith("https://")
        assert not _rows(gateway)


@pytest.mark.parametrize("role", ["user", "hr"])
def test_non_admin_cannot_sign_even_with_valid_independent_admin_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: Role
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "138", role)
    _, emergency_cookie, _ = gateway.auth.admin_sessions.login(
        _BOOTSTRAP, "test"
    )
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        browser.cookies.set(
            gateway.auth.settings.admin_cookie_name,
            emergency_cookie,
            path="/kb",
        )
        response = browser.get("/kb/api/admin/entry", follow_redirects=False)
        assert response.status_code == 403
        _private_response(response)
        assert not _rows(gateway)


@pytest.mark.parametrize("bootstrap_only", [False, True])
def test_employee_identity_required_for_anonymous_or_bootstrap_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bootstrap_only: bool
):
    gateway = _gateway(tmp_path, monkeypatch)
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        if bootstrap_only:
            _, cookie, _ = gateway.auth.admin_sessions.login(_BOOTSTRAP, "test")
            browser.cookies.set(
                gateway.auth.settings.admin_cookie_name, cookie, path="/kb"
            )
        response = browser.get("/kb/api/admin/entry", follow_redirects=False)
        assert response.status_code == 401
        _private_response(response)
        assert not _rows(gateway)


def test_role_demotion_between_issue_and_consume_denies_and_burns_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "201", "admin")
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        response = browser.get("/kb/api/admin/entry", follow_redirects=False)
        assert gateway.auth.users is not None
        gateway.auth.users.set_role("201", "user", actor_user_id="325")
        denied = browser.get(
            response.headers["location"], follow_redirects=False
        )
        assert denied.status_code == 403
        _private_response(denied)
        assert "set-cookie" not in denied.headers
        assert not _rows(gateway)
        gateway.auth.users.set_role("201", "admin", actor_user_id="325")
        assert (
            browser.get(
                response.headers["location"], follow_redirects=False
            ).status_code
            == 401
        )


def test_expired_and_replayed_tickets_never_set_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "325", "admin")
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        first = browser.get("/kb/api/admin/entry", follow_redirects=False)
        ticket = _ticket(first)
        gateway.clock[0] += 60
        expired = browser.get(first.headers["location"], follow_redirects=False)
        assert expired.status_code == 401
        assert ticket not in expired.text
        assert "set-cookie" not in expired.headers
        _private_response(expired)
        next_start = browser.get(
            _PUBLIC + "/kb/api/admin/entry", follow_redirects=False
        )
        assert (
            browser.get(
                next_start.headers["location"], follow_redirects=False
            ).status_code
            == 303
        )
        replay = browser.get(
            next_start.headers["location"], follow_redirects=False
        )
        assert replay.status_code == 401
        assert "set-cookie" not in replay.headers


def test_ticket_and_target_cookie_do_not_extend_nearly_expired_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "325", "admin")
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        gateway.clock[0] = issue.principal.expires_at - 10
        start = browser.get("/kb/api/admin/entry", follow_redirects=False)
        assert _rows(gateway)[0]["expires_at"] == issue.principal.expires_at
        gateway.clock[0] += 5
        completed = browser.get(
            start.headers["location"], follow_redirects=False
        )
        assert completed.status_code == 303
        cookie = SimpleCookie()
        for value in completed.headers.get_list("set-cookie"):
            cookie.load(value)
        assert (
            cookie[gateway.auth.public_sessions.cookie_name]["max-age"] == "5"
        )
        assert (
            cookie[gateway.auth.public_sessions.cookie_name].value
            == issue.cookie_value
        )


def test_handoff_requires_target_origin_and_keeps_ticket_for_correct_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "325", "admin")
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        start = browser.get("/kb/api/admin/entry", follow_redirects=False)
        wrong_url = start.headers["location"].replace(_INTERNAL, _PUBLIC)
        denied = browser.get(wrong_url, follow_redirects=False)
        assert denied.status_code == 403
        _private_response(denied)
        assert len(_rows(gateway)) == 1
        assert (
            browser.get(
                start.headers["location"], follow_redirects=False
            ).status_code
            == 303
        )


@pytest.mark.parametrize("extra", ["&ticket=another", "&extra=1", "&ticket="])
def test_complete_accepts_exactly_one_ticket_parameter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "325", "admin")
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        start = browser.get("/kb/api/admin/entry", follow_redirects=False)
        invalid = browser.get(
            start.headers["location"] + extra, follow_redirects=False
        )
        assert invalid.status_code == 400
        _private_response(invalid)
        assert len(_rows(gateway)) == 1
        assert (
            browser.get(
                start.headers["location"], follow_redirects=False
            ).status_code
            == 303
        )


def test_cross_deployment_cannot_consume_another_deployments_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    shared = tmp_path / "shared-data"
    gateway = _gateway(tmp_path / "first", monkeypatch, database_dir=shared)
    other = _gateway(
        tmp_path / "other", monkeypatch, database_dir=shared, deployment="other"
    )
    issue = _issue(gateway, "325", "admin")
    with (
        TestClient(gateway.app, base_url=_PUBLIC) as browser,
        TestClient(other.app) as outsider,
    ):
        _set_user(browser, gateway, issue, _PUBLIC)
        start = browser.get("/kb/api/admin/entry", follow_redirects=False)
        denied = outsider.get(start.headers["location"], follow_redirects=False)
        assert denied.status_code == 401
        assert len(_rows(gateway)) == 1
        assert (
            browser.get(
                start.headers["location"], follow_redirects=False
            ).status_code
            == 303
        )


def test_simultaneous_consumers_only_one_can_restore_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "325", "admin")
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        _set_user(browser, gateway, issue, _PUBLIC)
        start = browser.get("/kb/api/admin/entry", follow_redirects=False)

    def consume() -> int:
        with TestClient(gateway.app) as browser:
            return browser.get(
                start.headers["location"], follow_redirects=False
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume(), range(2)))
    assert sorted(results) == [303, 401]
    assert not _rows(gateway)


@pytest.mark.parametrize(
    "peer,expected", [("203.0.113.8", 302), ("203.0.113.9", 403)]
)
def test_only_trusted_proxy_can_restore_registered_public_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: str, expected: int
):
    gateway = _gateway(tmp_path, monkeypatch)
    issue = _issue(gateway, "325", "admin")
    with TestClient(
        gateway.app, base_url="http://gateway.private", client=(peer, 50000)
    ) as browser:
        response = browser.get(
            "/kb/api/admin/entry",
            headers={
                "Cookie": gateway.auth.public_sessions.cookie_name
                + "="
                + issue.cookie_value,
                "X-Forwarded-Host": "public.test",
                "X-Forwarded-Proto": "https",
            },
            follow_redirects=False,
        )
        assert response.status_code == expected
        _private_response(response)


def test_multiple_entries_without_internal_target_fail_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with pytest.raises(ValueError, match="internal"):
        _gateway(
            tmp_path,
            monkeypatch,
            entries=(("first", _PUBLIC), ("second", _INTERNAL)),
        )


def test_anonymous_mode_does_not_register_handoffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gateway = _gateway(tmp_path, monkeypatch, sso_enabled=False)
    with TestClient(gateway.app, base_url=_PUBLIC) as browser:
        assert browser.get("/kb/api/admin/entry").status_code == 404
    factory = SqliteConnectionFactory(gateway.auth.settings.database_path)
    with factory.transaction() as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name='gateway_admin_handoffs'"
            ).fetchone()
            is None
        )
