"""研发管理系统身份、固定角色及受保护的超级管理员。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)
from wanshitong_gateway.compat.wanshitong.sso_session import SsoIdentity

Role = Literal["admin", "hr", "user"]
_ROLES = frozenset({"admin", "hr", "user"})


class RoleChangeDeniedError(PermissionError):
    """操作者无权修改目标用户或授予指定角色。"""


@dataclass(frozen=True, slots=True)
class UserProfile:
    """从网关数据库实时读取的用户身份与本地角色。"""

    user_id: str
    username: str
    display_name: str
    dept_id: str | None
    role: Role
    is_super_admin: bool = False

    def as_dict(self) -> dict[str, str | bool | None]:
        """返回前端可见的用户信息。"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "dept_id": self.dept_id,
            "role": self.role,
            "is_super_admin": self.is_super_admin,
        }


def can_access_hr_internal(profile: UserProfile) -> bool:
    """人力内部资料只向人力角色和受保护的超级管理员开放。"""
    return profile.role == "hr" or profile.is_super_admin


def role_edit_note(actor: UserProfile, target: UserProfile) -> str | None:
    """返回目标角色不能修改的原因，供界面与保存检查共用。"""
    if target.is_super_admin:
        return "超级管理员身份由服务端配置，不能在此修改。"
    if actor.user_id == target.user_id:
        return "不能修改自己的角色。"
    if actor.role != "admin":
        return "仅管理员可以调整用户角色。"
    if target.role == "admin" and not actor.is_super_admin:
        return "仅超级管理员可以调整其他管理员的角色。"
    return None


def editable_roles(actor: UserProfile, target: UserProfile) -> tuple[Role, ...]:
    """管理员只能管理普通、人力身份，管理员任免归超级管理员。"""
    if role_edit_note(actor, target) is not None:
        return ()
    if actor.is_super_admin:
        return ("user", "hr", "admin")
    return ("user", "hr")


def _history_user_id(owner_id: object, deployment_id: str) -> str | None:
    """仅接受当前部署的完整RDMS主体，不把额外分隔符或空白当用户ID。"""
    prefix = f"rdms:{deployment_id}:"
    if not isinstance(owner_id, str) or not owner_id.startswith(prefix):
        return None
    user_id = owner_id[len(prefix) :]
    if (
        not user_id
        or ":" in user_id
        or not user_id.isprintable()
        or any(character.isspace() for character in user_id)
    ):
        return None
    return user_id


class UserStore:
    """按可信 RDMS userId 保存角色，不采用 IdP 的角色或权限列表。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        admin_usernames: tuple[str, ...] | None = None,
        super_admin_user_ids: tuple[str, ...] | None = None,
    ) -> None:
        """在当前测试网关数据库初始化用户表。"""
        self._connections = SqliteConnectionFactory(database_path)
        names = (
            tuple(
                os.environ.get("WANSHITONG_ADMIN_USERNAMES", "taoyifei").split(
                    ","
                )
            )
            if admin_usernames is None
            else admin_usernames
        )
        self._admin_usernames = frozenset(
            name.strip().casefold() for name in names if name.strip()
        )
        protected_ids = (
            os.environ.get("WANSHITONG_SUPER_ADMIN_USER_IDS", "325").split(",")
            if super_admin_user_ids is None
            else super_admin_user_ids
        )
        self._super_admin_user_ids = frozenset(
            user_id.strip() for user_id in protected_ids if user_id.strip()
        )
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_profiles ("
                "user_id TEXT PRIMARY KEY, "
                "username TEXT NOT NULL, "
                "display_name TEXT NOT NULL, "
                "dept_id TEXT, "
                "role TEXT NOT NULL CHECK(role IN ('admin', 'hr', 'user')))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_role_changes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "actor_user_id TEXT NOT NULL, target_user_id TEXT NOT NULL, "
                "previous_role TEXT NOT NULL, new_role TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
            )

    def record_identity(self, identity: SsoIdentity) -> UserProfile:
        """同步已核验的登录信息，保留管理员手工设置的角色。"""
        if not identity.user_id:
            raise ValueError("研发管理系统身份缺少 userId。")
        username = identity.username or identity.user_id
        display_name = identity.nick_name or username
        initial_role: Role = (
            "admin"
            if identity.user_id in self._super_admin_user_ids
            or (
                identity.username is not None
                and identity.username.casefold() in self._admin_usernames
            )
            else "user"
        )
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO user_profiles (user_id, username, display_name, "
                "dept_id, role) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "username=excluded.username, "
                "display_name=excluded.display_name, dept_id=excluded.dept_id",
                (
                    identity.user_id,
                    username,
                    display_name,
                    identity.dept_id,
                    initial_role,
                ),
            )
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=?",
                (identity.user_id,),
            ).fetchone()
        if row is None:
            raise AssertionError("用户信息写入后不存在。")
        return self._profile(row)

    def backfill_usage_users(self, *, deployment_id: str) -> int:
        """补登记当前部署历史使用者，不覆盖已有身份字段或管理员分配的角色。

        Args:
            deployment_id: 当前网关已验证的部署标识，不能混用其他环境的历史。

        Returns:
            本次新增的用户数。历史没有真实登录名或部门时，分别使用用户ID和空值。

        """
        if not deployment_id:
            raise ValueError("补登记历史用户必须指定当前部署。")
        with self._connections.transaction(write=True) as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            users: dict[str, str] = {}
            for table in ("gateway_conversations", "gateway_turns"):
                if table not in tables:
                    continue
                rows = connection.execute(
                    f"SELECT DISTINCT owner_id FROM {table} "  # noqa: S608 - 表名仅取上面的两个固定值。
                    "WHERE deployment_id=?",
                    (deployment_id,),
                )
                for row in rows:
                    user_id = _history_user_id(row["owner_id"], deployment_id)
                    if user_id is not None:
                        users[user_id] = user_id
            if "gateway_turns" in tables:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(gateway_turns)"
                    )
                }
                if "asker_name" in columns:
                    named: set[str] = set()
                    rows = connection.execute(
                        "SELECT owner_id,asker_name FROM gateway_turns "
                        "WHERE deployment_id=? "
                        "ORDER BY created_at DESC,trace_id DESC",
                        (deployment_id,),
                    )
                    for row in rows:
                        user_id = _history_user_id(
                            row["owner_id"], deployment_id
                        )
                        name = row["asker_name"]
                        if (
                            user_id is not None
                            and user_id not in named
                            and isinstance(name, str)
                            and name.strip()
                        ):
                            users[user_id] = name.strip()
                            named.add(user_id)
            inserted = 0
            for user_id, display_name in users.items():
                # 历史昵称不授予角色；受保护的超管仍只由_profile读取服务端配置。
                result = connection.execute(
                    "INSERT INTO user_profiles "
                    "(user_id,username,display_name,dept_id,role) "
                    "VALUES (?,?,?,NULL,'user') "
                    "ON CONFLICT(user_id) DO NOTHING",
                    (user_id, user_id, display_name),
                )
                inserted += result.rowcount
        return inserted

    def get(self, user_id: str) -> UserProfile | None:
        """读取当前角色，角色变更无需重新登录。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        return None if row is None else self._profile(row)

    def list_users(self) -> tuple[UserProfile, ...]:
        """列出已登录过湾小度的用户。"""
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM user_profiles ORDER BY display_name, user_id"
            ).fetchall()
        return tuple(self._profile(row) for row in rows)

    def set_role(
        self, user_id: str, role: Role, *, actor_user_id: str
    ) -> UserProfile:
        """在同一事务中核对双方当前身份、保存角色并记录变更。

        Args:
            user_id: 已登录过湾小度的目标用户 ID。
            role: 本次准备授予的业务角色。
            actor_user_id: 从真实登录会话取得的操作者 ID。

        Raises:
            KeyError: 目标用户不存在。
            RoleChangeDeniedError: 操作者无权修改目标或授予该角色。
            ValueError: 角色不属于预定义三种角色。

        """
        if role not in _ROLES:
            raise ValueError("角色必须为 admin、hr 或 user。")
        with self._connections.transaction(write=True) as connection:
            actor_row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (actor_user_id,)
            ).fetchone()
            target_row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
            if target_row is None:
                raise KeyError(user_id)
            if actor_row is None:
                raise RoleChangeDeniedError("操作者身份不存在。")
            actor = self._profile(actor_row)
            target = self._profile(target_row)
            if role not in editable_roles(actor, target):
                raise RoleChangeDeniedError(
                    role_edit_note(actor, target)
                    or "仅超级管理员可以授予管理员角色。"
                )
            connection.execute(
                "UPDATE user_profiles SET role=? WHERE user_id=?",
                (role, user_id),
            )
            if target.role != role:
                connection.execute(
                    "INSERT INTO user_role_changes "
                    "(actor_user_id,target_user_id,previous_role,new_role) "
                    "VALUES (?,?,?,?)",
                    (actor_user_id, user_id, target.role, role),
                )
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            raise AssertionError("角色更新后用户信息不存在。")
        return self._profile(row)

    def _profile(self, row: sqlite3.Row) -> UserProfile:
        """超级管理员绑定可信 userId，其有效角色不受历史角色值影响。"""
        user_id = str(row["user_id"])
        is_super_admin = user_id in self._super_admin_user_ids
        return UserProfile(
            user_id=user_id,
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            dept_id=None if row["dept_id"] is None else str(row["dept_id"]),
            role="admin" if is_super_admin else cast(Role, str(row["role"])),
            is_super_admin=is_super_admin,
        )
