"""固定角色与登录资料同步的必要行为。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from wanshitong_gateway.compat.wanshitong.sso_session import SsoIdentity
from wanshitong_gateway.roles import Role, RoleChangeDeniedError, UserStore


def _identity(user_id: str, username: str) -> SsoIdentity:
    return SsoIdentity(
        user_id=user_id,
        username=username,
        nick_name=username,
        dept_id="42",
        roles=(),
        permissions=(),
    )


def test_login_keeps_manual_role_and_syncs_department(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "gateway.sqlite3")
    store.record_identity(_identity("325", "taoyifei"))
    identity = _identity("1001", "staff")
    assert store.record_identity(identity).role == "user"
    store.set_role("1001", "hr", actor_user_id="325")
    updated = store.record_identity(replace(identity, dept_id="43"))
    assert updated.role == "hr"
    assert updated.dept_id == "43"
    assert store.get("1001") == updated
    with sqlite3.connect(tmp_path / "gateway.sqlite3") as connection:
        row = connection.execute(
            "SELECT actor_user_id,target_user_id,previous_role,new_role,"
            "created_at FROM user_role_changes"
        ).fetchone()
    assert row[:4] == ("325", "1001", "user", "hr")
    assert row[4].endswith("Z")


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
    assert profile.is_super_admin is False
    assert store.list_users() == (profile,)


def test_super_admin_is_bound_to_user_id_and_protects_legacy_role(
    tmp_path: Path,
) -> None:
    database = tmp_path / "gateway.sqlite3"
    store = UserStore(database)
    owner = store.record_identity(_identity("325", "renamed_owner"))
    assert owner.is_super_admin is True
    assert owner.role == "admin"
    same_name = store.record_identity(_identity("1001", "taoyifei"))
    assert same_name.is_super_admin is False
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE user_profiles SET role='user' WHERE user_id='325'"
        )
    assert store.get("325") == owner
    with pytest.raises(RoleChangeDeniedError):
        store.set_role("325", "hr", actor_user_id="1001")


@pytest.mark.parametrize(
    ("actor_id", "target_id", "role", "allowed"),
    [
        ("325", "1001", "admin", True),
        ("325", "201", "user", True),
        ("201", "1001", "hr", True),
        ("201", "1002", "user", True),
        ("201", "202", "user", False),
        ("201", "1001", "admin", False),
        ("201", "201", "hr", False),
        ("201", "325", "user", False),
        ("325", "325", "user", False),
        ("1001", "1002", "user", False),
        ("1002", "1001", "hr", False),
    ],
)
def test_role_change_boundaries_and_denials_do_not_write(
    tmp_path: Path,
    actor_id: str,
    target_id: str,
    role: Role,
    *,
    allowed: bool,
) -> None:
    database = tmp_path / "gateway.sqlite3"
    store = UserStore(database)
    for identifier in ("325", "201", "202", "1001", "1002"):
        store.record_identity(_identity(identifier, "staff_" + identifier))
    store.set_role("201", "admin", actor_user_id="325")
    store.set_role("202", "admin", actor_user_id="325")
    store.set_role("1002", "hr", actor_user_id="325")
    before = store.list_users()
    if allowed:
        assert (
            store.set_role(target_id, role, actor_user_id=actor_id).role == role
        )
    else:
        with pytest.raises(RoleChangeDeniedError):
            store.set_role(target_id, role, actor_user_id=actor_id)
        assert store.list_users() == before
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT count(*) FROM user_role_changes"
            ).fetchone()[0] == 3
