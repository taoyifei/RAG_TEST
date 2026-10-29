"""研发管理系统身份与湾小度三种固定角色。"""

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


@dataclass(frozen=True, slots=True)
class UserProfile:
    """从网关数据库实时读取的用户身份与本地角色。"""

    user_id: str
    username: str
    display_name: str
    dept_id: str | None
    role: Role

    def as_dict(self) -> dict[str, str | None]:
        """返回前端可见的用户信息。"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "dept_id": self.dept_id,
            "role": self.role,
        }


class UserStore:
    """按可信 RDMS userId 保存角色，不采用 IdP 的角色或权限列表。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        admin_usernames: tuple[str, ...] | None = None,
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
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_profiles ("
                "user_id TEXT PRIMARY KEY, "
                "username TEXT NOT NULL, "
                "display_name TEXT NOT NULL, "
                "dept_id TEXT, "
                "role TEXT NOT NULL CHECK(role IN ('admin', 'hr', 'user')))"
            )

    def record_identity(self, identity: SsoIdentity) -> UserProfile:
        """同步已核验的登录信息，保留管理员手工设置的角色。"""
        if not identity.user_id:
            raise ValueError("研发管理系统身份缺少 userId。")
        username = identity.username or identity.user_id
        display_name = identity.nick_name or username
        initial_role: Role = (
            "admin"
            if identity.username is not None
            and identity.username.casefold() in self._admin_usernames
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
        return _profile(row)

    def get(self, user_id: str) -> UserProfile | None:
        """读取当前角色，角色变更无需重新登录。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        return None if row is None else _profile(row)

    def list_users(self) -> tuple[UserProfile, ...]:
        """列出已登录过湾小度的用户。"""
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM user_profiles ORDER BY display_name, user_id"
            ).fetchall()
        return tuple(_profile(row) for row in rows)

    def set_role(self, user_id: str, role: Role) -> UserProfile:
        """修改一个已存在用户的固定角色。"""
        if role not in _ROLES:
            raise ValueError("角色必须为 admin、hr 或 user。")
        with self._connections.transaction(write=True) as connection:
            result = connection.execute(
                "UPDATE user_profiles SET role=? WHERE user_id=?",
                (role, user_id),
            )
            if result.rowcount == 0:
                raise KeyError(user_id)
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            raise AssertionError("角色更新后用户信息不存在。")
        return _profile(row)


def _profile(row: sqlite3.Row) -> UserProfile:
    return UserProfile(
        user_id=str(row["user_id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        dept_id=None if row["dept_id"] is None else str(row["dept_id"]),
        role=cast(Role, str(row["role"])),
    )
