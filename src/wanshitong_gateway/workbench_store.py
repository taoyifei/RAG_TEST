"""保存工作台身份和按用户、资料范围隔离的原生工作空间。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)


@dataclass(frozen=True, slots=True)
class WorkbenchIdentity:
    """稳定的研发用户到原生普通账号映射，不保存口令和 JWT。"""

    user_id: str
    native_user_id: str
    username: str
    email: str


@dataclass(frozen=True, slots=True)
class WorkbenchWorkspace:
    """某一代工作空间的授权快照及撤权进度。"""

    workspace_id: str
    owner_id: str
    search_scopes: tuple[str, ...]
    kb_ids: tuple[str, ...]
    generation: int
    state: str
    native_tenant_id: int
    organization_id: str
    model_id: str
    revoked: bool


class WorkbenchStore:
    """用独立表记录原生资源，禁用状态先于任何撤权网络操作提交。"""

    def __init__(self, database_path: str | Path) -> None:
        self._connections = SqliteConnectionFactory(database_path)
        with self._connections.transaction(write=True) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS workbench_identities ("
                "user_id TEXT PRIMARY KEY, native_user_id TEXT NOT NULL "
                "UNIQUE, username TEXT NOT NULL, email TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS workbench_workspaces ("
                "workspace_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
                "scopes_json TEXT NOT NULL, kb_ids_json TEXT NOT NULL, "
                "generation INTEGER NOT NULL, state TEXT NOT NULL "
                "CHECK(state IN ('provisioning','active','disabled')), "
                "native_tenant_id INTEGER NOT NULL DEFAULT 0, "
                "organization_id TEXT NOT NULL DEFAULT '', "
                "model_id TEXT NOT NULL DEFAULT '', "
                "revoked INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "UNIQUE(owner_id,scopes_json,generation));"
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "workbench_one_active_scope ON workbench_workspaces"
                "(owner_id,scopes_json) "
                "WHERE state IN ('provisioning','active');"
            )

    def identity(self, user_id: str) -> WorkbenchIdentity | None:
        """读取稳定原生身份。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_identities WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return (
            None
            if row is None
            else WorkbenchIdentity(
                user_id=row["user_id"],
                native_user_id=row["native_user_id"],
                username=row["username"],
                email=row["email"],
            )
        )

    def remember_identity(self, identity: WorkbenchIdentity) -> None:
        """幂等登记身份，拒绝把已登记用户换绑到另一原生账号。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO workbench_identities "
                "(user_id,native_user_id,username,email) VALUES (?,?,?,?)",
                (
                    identity.user_id,
                    identity.native_user_id,
                    identity.username,
                    identity.email,
                ),
            )
            row = connection.execute(
                "SELECT native_user_id,email FROM workbench_identities "
                "WHERE user_id=?",
                (identity.user_id,),
            ).fetchone()
            if row is None or (row["native_user_id"], row["email"]) != (
                identity.native_user_id,
                identity.email,
            ):
                raise ValueError("工作台原生身份与已登记账号不一致。")

    def get(self, workspace_id: str) -> WorkbenchWorkspace | None:
        """按不可猜测的网关 ID 读取工作空间。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_workspaces WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        return None if row is None else _workspace(row)

    def list_workspaces(
        self, owner_id: str | None = None
    ) -> tuple[WorkbenchWorkspace, ...]:
        """列出工作空间，包含需要继续撤权的禁用记录。"""
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM workbench_workspaces "
                "WHERE (? IS NULL OR owner_id=?) ORDER BY generation",
                (owner_id, owner_id),
            ).fetchall()
        return tuple(_workspace(row) for row in rows)

    def create(
        self,
        owner_id: str,
        scopes: tuple[str, ...],
        kb_ids: tuple[str, ...],
    ) -> WorkbenchWorkspace:
        """登记新一代后再创建原生资源，失败时仍有可回收的记录。"""
        workspace_id = str(uuid.uuid4())
        scopes_json = json.dumps(scopes)
        with self._connections.transaction(write=True) as connection:
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation),0)+1 "
                    "FROM workbench_workspaces "
                    "WHERE owner_id=? AND scopes_json=?",
                    (owner_id, scopes_json),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO workbench_workspaces "
                "(workspace_id,owner_id,scopes_json,kb_ids_json,generation,"
                "state) VALUES (?,?,?,?,?,'provisioning')",
                (
                    workspace_id,
                    owner_id,
                    scopes_json,
                    json.dumps(kb_ids),
                    generation,
                ),
            )
        result = self.get(workspace_id)
        if result is None:
            raise AssertionError("新工作空间登记失败。")
        return result

    def save_native(
        self,
        workspace_id: str,
        *,
        tenant_id: int,
        organization_id: str = "",
        model_id: str = "",
    ) -> None:
        """逐步记录原生资源，支持创建中断后的撤权。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE workbench_workspaces SET native_tenant_id=?, "
                "organization_id=?,model_id=? WHERE workspace_id=?",
                (tenant_id, organization_id, model_id, workspace_id),
            )

    def activate(self, workspace_id: str) -> None:
        """仅把仍处于创建中的工作空间标记为可访问。"""
        with self._connections.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE workbench_workspaces SET state='active' "
                "WHERE workspace_id=? AND state='provisioning'",
                (workspace_id,),
            ).rowcount
        if changed != 1:
            raise ValueError("工作空间已经禁用，不能重新激活旧一代。")

    def disable(self, workspace_ids: tuple[str, ...]) -> None:
        """先阻止所有代理和流读取，再开始原生暂停及撤销 membership。"""
        with self._connections.transaction(write=True) as connection:
            connection.executemany(
                "UPDATE workbench_workspaces SET state='disabled' "
                "WHERE workspace_id=?",
                ((workspace_id,) for workspace_id in workspace_ids),
            )

    def mark_revoked(self, workspace_id: str) -> None:
        """记录原生 Viewer membership 已撤销，供失败重试使用。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE workbench_workspaces SET revoked=1 "
                "WHERE workspace_id=? AND state='disabled'",
                (workspace_id,),
            )


def _workspace(row: sqlite3.Row) -> WorkbenchWorkspace:
    return WorkbenchWorkspace(
        workspace_id=row["workspace_id"],
        owner_id=row["owner_id"],
        search_scopes=tuple(json.loads(row["scopes_json"])),
        kb_ids=tuple(json.loads(row["kb_ids_json"])),
        generation=row["generation"],
        state=row["state"],
        native_tenant_id=row["native_tenant_id"],
        organization_id=row["organization_id"],
        model_id=row["model_id"],
        revoked=bool(row["revoked"]),
    )
