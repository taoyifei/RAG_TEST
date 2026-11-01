"""固定角色与登录资料同步的必要行为。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from wanshitong_gateway.compat.wanshitong.sso_session import SsoIdentity
from wanshitong_gateway.roles import Role, RoleChangeDeniedError, UserStore
from wanshitong_gateway.store import GatewayStore


def _identity(user_id: str, username: str) -> SsoIdentity:
    return SsoIdentity(
        user_id=user_id,
        username=username,
        nick_name=username,
        dept_id="42",
        roles=(),
        permissions=(),
    )


def _seed_usage_turns(
    database: Path,
    rows: tuple[tuple[str, str, str, str | None, str], ...],
) -> None:
    """用真实历史表保存合成旧记录，不经过身份登记。"""
    GatewayStore(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO gateway_turns "
            "(trace_id,deployment_id,owner_id,asker_name,created_at,"
            "conversation_id,native_session_id,question,status) "
            "VALUES (?,?,?,?,?,'history','native','合成旧问题','completed')",
            rows,
        )


def test_history_backfill_is_scoped_idempotent_and_preserves_profiles(
    tmp_path: Path,
) -> None:
    database = tmp_path / "gateway.sqlite3"
    users = UserStore(database)
    owner = users.record_identity(_identity("325", "taoyifei"))
    original = users.record_identity(_identity("1001", "verified_staff"))
    original = users.set_role("1001", "hr", actor_user_id="325")
    _seed_usage_turns(
        database,
        (
            ("old", "prod", "rdms:prod:1002", "旧显示名", "2026-10-01"),
            ("new", "prod", "rdms:prod:1002", " 新显示名 ", "2026-10-02"),
            ("blank", "prod", "rdms:prod:1002", " \t ", "2026-10-03"),
            ("kept", "prod", "rdms:prod:1001", "不能覆盖实名", "2026-10-04"),
            ("owner", "prod", "rdms:prod:325", "不能覆盖超管", "2026-10-04"),
            ("named", "prod", "rdms:prod:1", "超级管理员", "2026-10-04"),
            ("cross", "test", "rdms:prod:1004", "其他环境", "2026-10-04"),
            ("prefix", "prod", "rdms:test:1005", "其他主体", "2026-10-04"),
            ("anonymous", "prod", "anonymous:1006", "匿名用户", "2026-10-04"),
        ),
    )
    history = GatewayStore(database)
    history.bind_session(
        deployment_id="prod",
        owner_id="rdms:prod:1003",
        conversation_id="without-turn",
        native_session_id="native-without-turn",
    )
    with sqlite3.connect(database) as connection:
        turns_before = connection.execute(
            "SELECT * FROM gateway_turns"
        ).fetchall()
        conversations_before = connection.execute(
            "SELECT * FROM gateway_conversations"
        ).fetchall()
        role_changes = connection.execute(
            "SELECT * FROM user_role_changes"
        ).fetchall()
    assert users.backfill_usage_users(deployment_id="prod") == 3
    assert users.backfill_usage_users(deployment_id="prod") == 0
    assert users.get("325") == owner
    assert users.get("1001") == original
    new = users.get("1002")
    assert new is not None
    assert (new.username, new.display_name, new.dept_id, new.role) == (
        "1002",
        "新显示名",
        None,
        "user",
    )
    no_turn = users.get("1003")
    assert no_turn is not None and no_turn.display_name == "1003"
    named_admin = users.get("1")
    assert named_admin is not None
    assert named_admin.display_name == "超级管理员"
    assert named_admin.role == "user" and named_admin.is_super_admin is False
    assert {user.user_id for user in users.list_users()} == {
        "325",
        "1001",
        "1002",
        "1003",
        "1",
    }
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM gateway_turns").fetchall()
            == turns_before
        )
        assert (
            connection.execute("SELECT * FROM gateway_conversations").fetchall()
            == conversations_before
        )
        assert (
            connection.execute("SELECT * FROM user_role_changes").fetchall()
            == role_changes
        )


@pytest.mark.parametrize("protected_ids", [("325",), ()])
def test_history_super_admin_comes_only_from_config(
    tmp_path: Path, protected_ids: tuple[str, ...]
) -> None:
    database = tmp_path / "gateway.sqlite3"
    _seed_usage_turns(
        database,
        (("one", "prod", "rdms:prod:325", "超级管理员", "2026-10-01"),),
    )
    users = UserStore(
        database, admin_usernames=(), super_admin_user_ids=protected_ids
    )
    assert users.backfill_usage_users(deployment_id="prod") == 1
    profile = users.get("325")
    assert profile is not None
    assert profile.is_super_admin == bool(protected_ids)
    assert profile.role == ("admin" if protected_ids else "user")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT role FROM user_profiles WHERE user_id='325'"
        ).fetchone() == ("user",)


@pytest.mark.parametrize(
    "owner_id",
    [
        "rdms:prod:",
        "rdms:prod:u:extra",
        "rdms:prod: user",
        "rdms:prod:user name",
        "rdms:prod:user\tname",
        "rdms:prod:user\x00name",
    ],
)
def test_history_backfill_ignores_malformed_owner_ids(
    tmp_path: Path, owner_id: str
) -> None:
    database = tmp_path / "gateway.sqlite3"
    _seed_usage_turns(
        database, (("one", "prod", owner_id, "旧姓名", "2026-10-01"),)
    )
    users = UserStore(database)
    assert users.backfill_usage_users(deployment_id="prod") == 0
    assert users.list_users() == ()


def test_history_backfill_handles_missing_tables_and_legacy_name_column(
    tmp_path: Path,
) -> None:
    database = tmp_path / "gateway.sqlite3"
    users = UserStore(database)
    assert users.backfill_usage_users(deployment_id="prod") == 0
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE gateway_turns (deployment_id TEXT,owner_id TEXT)"
        )
        connection.execute(
            "INSERT INTO gateway_turns VALUES ('prod','rdms:prod:1001')"
        )
    assert users.backfill_usage_users(deployment_id="prod") == 1
    profile = users.get("1001")
    assert profile is not None
    assert (profile.username, profile.display_name) == ("1001", "1001")


def test_sso_enriches_backfilled_identity_without_resetting_assigned_role(
    tmp_path: Path,
) -> None:
    database = tmp_path / "gateway.sqlite3"
    users = UserStore(database)
    users.record_identity(_identity("325", "taoyifei"))
    _seed_usage_turns(
        database,
        (("one", "prod", "rdms:prod:1001", "历史姓名", "2026-10-01"),),
    )
    users.backfill_usage_users(deployment_id="prod")
    users.set_role("1001", "hr", actor_user_id="325")
    identity = replace(
        _identity("1001", "verified_login"), nick_name="正式姓名"
    )
    profile = users.record_identity(identity)
    assert profile.role == "hr"
    assert (profile.username, profile.display_name, profile.dept_id) == (
        "verified_login",
        "正式姓名",
        "42",
    )
    assert users.backfill_usage_users(deployment_id="prod") == 0
    assert users.get("1001") == profile


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
            assert (
                connection.execute(
                    "SELECT count(*) FROM user_role_changes"
                ).fetchone()[0]
                == 3
            )
