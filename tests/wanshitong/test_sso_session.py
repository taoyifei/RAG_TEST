"""KB 签名 pending 与加密用户会话安全回归。"""

from __future__ import annotations

import pytest

from rag_app.core.errors import PolicyDenied
from rag_app.product.crypto import MasterKey
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
    assert service.cookie_path == "/kb"
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


def test_user_claims_over_cookie_limit_fail_without_truncation() -> None:
    service = _service([3_000.0])
    permissions = tuple(f"permission:{index}:{'x' * 80}" for index in range(80))

    with pytest.raises(ValueError, match="3500 bytes"):
        service.issue_user(_identity(permissions=permissions))
