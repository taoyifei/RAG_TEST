"""RDMS validate HTTP v1.3 合同回归。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from rag_app.wanshitong.sso_client import (
    SsoClient,
    SsoValidationError,
    load_client_secret,
)

_TICKET = "a" * 32
_SERVICE = "http://kb.test:8289/kb/sso/callback"
_STATE = "b" * 64


def _success(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    assert payload == {
        "ticket": _TICKET,
        "service": _SERVICE,
        "clientId": "kb",
        "clientSecret": "synthetic-secret",
    }
    return httpx.Response(
        200,
        json={
            "code": 200,
            "msg": "操作成功",
            "data": {
                "userId": 1,
                "username": "tester",
                "nickName": "测试用户",
                "deptId": 42,
                "roles": ["employee"],
                "permissions": ["kb:read"],
                "service": _SERVICE,
                "state": _STATE,
            },
        },
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> SsoClient:
    return SsoClient(
        validate_url="http://idp.test:21000/sso/validate",
        client_id="kb",
        client_secret="synthetic-secret",  # noqa: S106 - 合成测试凭据。
        transport=httpx.MockTransport(handler),
    )


def test_validate_sends_exact_body_and_normalizes_identity() -> None:
    client = _client(_success)

    identity = client.validate(
        ticket=_TICKET, service=_SERVICE, state=_STATE
    )

    assert identity.user_id == "1"
    assert identity.dept_id == "42"
    assert identity.nick_name == "测试用户"
    assert identity.roles == ("employee",)
    assert "synthetic-secret" not in repr(client)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "AUTH_REQUEST_INVALID"),
        (401, "AUTH_LOGIN_EXPIRED"),
        (403, "AUTH_LOGIN_EXPIRED"),
        (429, "AUTH_LOGIN_EXPIRED"),
        (503, "AUTH_SERVICE_UNAVAILABLE"),
    ],
)
def test_validate_branches_on_real_http_status_without_retry(
    status: int, code: str
) -> None:
    calls = 0

    def _failure(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"code": status, "msg": "x"})

    with pytest.raises(SsoValidationError) as captured:
        _client(_failure).validate(
            ticket=_TICKET, service=_SERVICE, state=_STATE
        )

    assert captured.value.code == code
    assert calls == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"code": 500},
        {"data": None},
        {"data": {"service": "http://attacker.test", "state": _STATE}},
        {"data": {"service": _SERVICE, "state": "wrong"}},
    ],
)
def test_validate_rejects_incomplete_or_mismatched_success(
    mutation: dict[str, object],
) -> None:
    base: dict[str, object] = {
        "code": 200,
        "data": {
            "userId": 1,
            "service": _SERVICE,
            "state": _STATE,
            "roles": [],
            "permissions": [],
        },
    }
    base.update(mutation)

    with pytest.raises(SsoValidationError, match="认证响应无效"):
        _client(lambda _request: httpx.Response(200, json=base)).validate(
            ticket=_TICKET, service=_SERVICE, state=_STATE
        )


def test_validate_does_not_follow_redirect() -> None:
    calls = 0

    def _redirect(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://attacker.test"})

    with pytest.raises(SsoValidationError):
        _client(_redirect).validate(
            ticket=_TICKET, service=_SERVICE, state=_STATE
        )
    assert calls == 1


def test_client_secret_requires_private_regular_file(tmp_path: Path) -> None:
    secret = tmp_path / "sso-client-secret"
    secret.write_text("secret-value\n", encoding="utf-8")
    secret.chmod(0o600)

    assert load_client_secret(secret) == "secret-value"
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_client_secret(secret)
