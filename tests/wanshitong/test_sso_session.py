"""KB 签名 pending 与加密用户会话安全回归。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rag_app.core.errors import PolicyDenied
from rag_app.product.crypto import MasterKey
from rag_app.wanshitong import sso_session
from rag_app.wanshitong.sso_session import SsoIdentity, SsoSessionService


def _service(now: list[float]) -> SsoSessionService:
    return SsoSessionService(
        MasterKey(value=b"s" * 32, key_id="synthetic"),
        deployment_id="candidate_8289",
        base_path="/kb",
        clock=lambda: now[0],
    )


def _identity(*, permissions: tuple[str, ...] = ("kb:read",)) -> SsoIdentity:
    return SsoIdentity(
        user_id="1001",
        username="tester",
        nick_name="测试用户",
        dept_id="42",
        roles=("employee",),
        permissions=permissions,
    )


def test_pending_is_encrypted_bound_to_flow_and_expires() -> None:
    now = [1_000.0]
    service = _service(now)
    issue = service.issue_pending(
        service="http://kb.test:8289/kb/sso/callback",
        return_to="/kb/admin/history",
        origin_id="internal",
    )

    assert issue.pending.state not in issue.cookie_value
    assert issue.pending.service not in issue.cookie_value
    assert len(issue.pending.state) == 64
    assert service.read_pending(issue.cookie_value) == issue.pending

    with pytest.raises(PolicyDenied):
        service.read_pending(issue.cookie_value + "tampered")
    now[0] += 300
    with pytest.raises(PolicyDenied, match="已过期"):
        service.read_pending(issue.cookie_value)


def test_user_session_is_encrypted_stable_and_csrf_bound() -> None:
    now = [2_000.0]
    service = _service(now)
    issue = service.issue_user(_identity())

    assert "tester" not in issue.cookie_value
    assert "kb:read" not in issue.cookie_value
    assert len(issue.cookie_value) <= 3500
    assert service.cookie_name == "kb_user_session_candidate_8289"
    assert service.cookie_path == "/"
    assert service.legacy_cookie_path == "/kb"
    assert service.pending_cookie_path == "/kb/sso"
    assert service.bootstrap(issue.cookie_value).principal == issue.principal
    assert (
        service.authenticate(issue.cookie_value, issue.csrf_token)
        == issue.principal
    )
    assert issue.principal.owner_id == "rdms:1001"
    assert issue.principal.expires_at == 9_200

    with pytest.raises(PolicyDenied):
        service.authenticate(issue.cookie_value, "wrong")
    with pytest.raises(PolicyDenied):
        service.bootstrap(None)
    now[0] += 7_200
    with pytest.raises(PolicyDenied, match="已过期"):
        service.validate_cookie(issue.cookie_value)


def test_large_unused_claims_do_not_block_user_session() -> None:
    service = _service([3_000.0])
    permissions = tuple(f"permission:{index}:{'x' * 80}" for index in range(80))

    issue = service.issue_user(_identity(permissions=permissions))

    assert len(issue.cookie_value) <= 3500
    assert issue.principal.nick_name == "测试用户"
    assert issue.principal.dept_id is None
    assert issue.principal.roles == ()
    assert issue.principal.permissions == ()
    assert service.bootstrap(issue.cookie_value).principal == issue.principal


def test_large_display_name_falls_back_to_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service([3_000.0])
    monkeypatch.setattr(sso_session, "_MAX_SESSION_COOKIE_CHARS", 700)
    identity = replace(_identity(), nick_name="😀" * 256)

    issue = service.issue_user(identity)

    assert len(issue.cookie_value) <= 700
    assert issue.principal.nick_name is None
    assert issue.principal.user_id == identity.user_id
    assert service.bootstrap(issue.cookie_value).principal == issue.principal


def test_existing_cookie_with_claims_remains_readable() -> None:
    service = _service([3_000.0])
    old_cookie = service._encode(
        "user",
        {
            "aud": "candidate_8289",
            "dept_id": "42",
            "exp": 10_200,
            "iat": 3_000,
            "iss": "rdms",
            "nick_name": "测试用户",
            "permissions": ["kb:read"],
            "roles": ["employee"],
            "sid": "wstsid_" + "a" * 32,
            "user_id": "1001",
            "username": "tester",
            "v": 1,
        },
    )

    principal = service.bootstrap(old_cookie).principal

    assert principal.owner_id == "rdms:1001"
    assert principal.dept_id == "42"
    assert principal.roles == ("employee",)
    assert principal.permissions == ("kb:read",)
