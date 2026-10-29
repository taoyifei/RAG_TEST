"""固定角色与登录资料同步的必要行为。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wanshitong_gateway.compat.wanshitong.sso_session import SsoIdentity
from wanshitong_gateway.roles import UserStore


def test_login_keeps_manual_role_and_syncs_department(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "gateway.sqlite3")
    identity = SsoIdentity(
        user_id="325",
        username="taoyifei",
        nick_name="陶逸非",
        dept_id="42",
        roles=(),
        permissions=(),
    )
    assert store.record_identity(identity).role == "admin"
    store.set_role("325", "hr")
    updated = store.record_identity(replace(identity, dept_id="43"))
    assert updated.role == "hr"
    assert updated.dept_id == "43"
    assert store.get("325") == updated


def test_idp_admin_claims_do_not_grant_local_admin(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "gateway.sqlite3")
    profile = store.record_identity(
        SsoIdentity(
            user_id="1001",
            username="ordinary",
            nick_name="普通用户",
            dept_id=None,
            roles=("admin",),
            permissions=("*:*:*",),
        )
    )
    assert profile.role == "user"
    assert store.list_users() == (profile,)
