"""在 Universal SQLite metadata 中持久化唯一湾事通 Scope。"""

from __future__ import annotations

from datetime import UTC, datetime

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.wanshitong.errors import ScopeBindingError
from rag_app.wanshitong.mode import WANSHITONG_SCOPE_KEY
from rag_app.wanshitong.models import ScopeBinding

_METADATA_NAMESPACE = "wanshitong.fixed-scope"


class ScopeBindingStore:
    """复用 Universal 控制库并以固定 metadata 主键保证唯一性。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        """保存已完成 Universal migrations 的连接工厂。

        Args:
            connections: Product Runtime 当前 SQLite 连接工厂。

        """
        self._connections = connections

    def read(self) -> ScopeBinding | None:
        """读取固定绑定，不存在时返回 None。

        Returns:
            已校验的绑定，或尚未创建时的 None。

        Raises:
            ScopeBindingError: 持久值不符合固定绑定 schema。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE namespace=? AND key=?",
                (_METADATA_NAMESPACE, WANSHITONG_SCOPE_KEY),
            ).fetchone()
        if row is None:
            return None
        return _decode_binding(str(row["value"]))

    def bind_unique(
        self, project_id: str, knowledge_base_id: str
    ) -> ScopeBinding:
        """原子写入唯一绑定，并拒绝把固定 key 改绑到其他对象。

        Args:
            project_id: 由 Universal SDK 返回的项目 ID。
            knowledge_base_id: 由 Universal SDK 返回的知识库 ID。

        Returns:
            首次写入或已存在且身份相同的绑定。

        Raises:
            ScopeBindingError: 固定 key 已绑定不同作用域或值已损坏。

        """
        candidate = ScopeBinding(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE namespace=? AND key=?",
                (_METADATA_NAMESPACE, WANSHITONG_SCOPE_KEY),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO metadata(namespace, key, value) "
                    "VALUES (?, ?, ?)",
                    (
                        _METADATA_NAMESPACE,
                        WANSHITONG_SCOPE_KEY,
                        candidate.model_dump_json(),
                    ),
                )
                return candidate
            existing = _decode_binding(str(row["value"]))
            if (
                existing.project_id != project_id
                or existing.knowledge_base_id != knowledge_base_id
            ):
                raise ScopeBindingError(
                    "湾事通固定 Scope 已绑定其他对象，启动已阻断。",
                    stage="wanshitong.scope.bind",
                    details={"system_key": WANSHITONG_SCOPE_KEY},
                )
            return existing


def _decode_binding(value: str) -> ScopeBinding:
    """把持久 JSON 解码为严格绑定模型。"""
    try:
        return ScopeBinding.model_validate_json(value)
    except ValueError:
        raise ScopeBindingError(
            "湾事通固定 Scope 持久绑定已损坏，启动已阻断。",
            stage="wanshitong.scope.read",
            details={"system_key": WANSHITONG_SCOPE_KEY},
        ) from None


__all__ = ["ScopeBindingStore"]
