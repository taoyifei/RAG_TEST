"""SQLite 持久化 Embedding cache adapter。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import Conflict, ValidationFailed
from rag_app.core.models import EmbeddingCacheIdentity, EmbeddingCacheRecord


class SqliteEmbeddingCache:
    """按 project/KB/global scope 隔离且验证向量编码的 cache。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        """保存共享数据库连接工厂。

        Args:
            connections: 已完成 migration 的 P06 数据库。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._closed = False

    def get_many(
        self,
        identities: Sequence[EmbeddingCacheIdentity],
    ) -> tuple[EmbeddingCacheRecord | None, ...]:
        """按输入顺序读取并 fail closed 校验每个命中。

        Args:
            identities: 完整 scope/slot/policy/text 身份。

        Returns:
            与输入等长的命中或 None。

        """
        self._ensure_open()
        now = datetime.now(UTC).isoformat()
        results: list[EmbeddingCacheRecord | None] = []
        with self._connections.transaction(write=True) as connection:
            for identity in identities:
                row = connection.execute(
                    "SELECT * FROM embedding_cache WHERE cache_key=?",
                    (identity.persistent_key,),
                ).fetchone()
                if row is None:
                    results.append(None)
                    continue
                self._validate_row(identity, row)
                record = EmbeddingCacheRecord.from_bytes(
                    identity,
                    bytes(row["vector_bytes"]),
                )
                connection.execute(
                    "UPDATE embedding_cache SET last_used_at=? "
                    "WHERE cache_key=?",
                    (now, identity.persistent_key),
                )
                results.append(record)
        return tuple(results)

    def put_many(self, records: Sequence[EmbeddingCacheRecord]) -> None:
        """逐条幂等写入，不覆盖同 key 的不同向量。

        Args:
            records: 已验证 cache 记录。

        Returns:
            无返回值。

        Raises:
            Conflict: 同一 cache key 已保存不同字节或身份。

        """
        self._ensure_open()
        now = datetime.now(UTC).isoformat()
        with self._connections.transaction(write=True) as connection:
            for record in records:
                identity = record.identity
                payload = record.to_bytes()
                existing = connection.execute(
                    "SELECT * FROM embedding_cache WHERE cache_key=?",
                    (identity.persistent_key,),
                ).fetchone()
                if existing is not None:
                    self._validate_row(identity, existing)
                    if bytes(existing["vector_bytes"]) != payload:
                        raise Conflict(
                            "同一 cache key 已存在不同向量。",
                            stage="embedding_cache.put",
                        )
                    connection.execute(
                        "UPDATE embedding_cache SET last_used_at=? "
                        "WHERE cache_key=?",
                        (now, identity.persistent_key),
                    )
                    continue
                slot = identity.slot
                connection.execute(
                    "INSERT INTO embedding_cache("
                    "cache_key, scope_kind, scope_id, slot_id, provider_id, "
                    "model, dimension, normalization, role_policy_identity, "
                    "adapter_revision, text_sha256, vector_encoding_version, "
                    "vector_bytes, created_at, last_used_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity.persistent_key,
                        identity.scope_kind.value,
                        identity.scope_id,
                        slot.slot_id,
                        slot.provider_id,
                        slot.model,
                        slot.dimension,
                        slot.normalization,
                        identity.role_policy_identity,
                        slot.adapter_revision,
                        identity.text_sha256,
                        record.vector_encoding_version,
                        payload,
                        now,
                        now,
                    ),
                )

    def close(self) -> None:
        """幂等关闭 cache。

        Args:
            无参数；不拥有共享连接工厂。

        Returns:
            无返回值。

        """
        self._closed = True

    def _validate_row(
        self, identity: EmbeddingCacheIdentity, row: sqlite3.Row
    ) -> None:
        slot = identity.slot
        expected = {
            "scope_kind": identity.scope_kind.value,
            "scope_id": identity.scope_id,
            "slot_id": slot.slot_id,
            "provider_id": slot.provider_id,
            "model": slot.model,
            "dimension": slot.dimension,
            "normalization": slot.normalization,
            "role_policy_identity": identity.role_policy_identity,
            "adapter_revision": slot.adapter_revision,
            "text_sha256": identity.text_sha256,
            "vector_encoding_version": "float32-le-v1",
        }
        if any(row[name] != value for name, value in expected.items()):
            raise ValidationFailed(
                "Embedding cache 行与请求身份不一致。",
                stage="embedding_cache.read",
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SqliteEmbeddingCache 已关闭。")


__all__ = ["SqliteEmbeddingCache"]
