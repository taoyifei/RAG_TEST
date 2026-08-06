"""语义路由 prototype 向量的独立 SQLite 缓存。"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rag_app.generation.question_profile import PrimaryOperation

__all__ = [
    "CachedPrototype",
    "IntentRouterCache",
    "PrototypeNamespace",
]


@dataclass(frozen=True, slots=True)
class PrototypeNamespace:
    """绑定配置和 embedding 身份的缓存命名空间。"""

    config_sha256: str
    embedding_model: str
    embedding_revision: str
    tokenizer_sha256: str
    dimension: int
    expected_example_count: int

    def __post_init__(self) -> None:
        """拒绝不完整的模型身份或无效维度。"""
        if (
            not self.config_sha256
            or not self.embedding_model.strip()
            or not self.embedding_revision.strip()
            or not self.tokenizer_sha256
            or self.dimension <= 0
            or self.expected_example_count <= 0
        ):
            raise ValueError("prototype namespace 身份不完整。")

    @property
    def digest(self) -> str:
        """返回不包含向量或问题正文的稳定命名空间摘要。

        Args:
            无参数；组合当前冻结身份。

        Returns:
            小写 SHA256 十六进制摘要。

        """
        payload = "\n".join(
            (
                self.config_sha256,
                self.embedding_model,
                self.embedding_revision,
                self.tokenizer_sha256,
                str(self.dimension),
                str(self.expected_example_count),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedPrototype:
    """一条仅用于内存路由的已缓存 prototype。"""

    example_id: str
    operation: PrimaryOperation
    text_sha256: str
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        """拒绝空 identity、非有限数值和空向量。"""
        if (
            not self.example_id.strip()
            or not self.text_sha256
            or not self.vector
            or not all(math.isfinite(value) for value in self.vector)
        ):
            raise ValueError("缓存 prototype 无效。")


class IntentRouterCache:
    """以完整 namespace 为原子发布单元的 prototype 缓存。"""

    def __init__(self, path: Path) -> None:
        """保存独立 SQLite 文件位置。

        Args:
            path: 仅存储 prototype vector 的 SQLite 文件。

        Returns:
            无返回值。

        """
        self._path = path

    def initialize(self) -> None:
        """创建不含 query vector 的缓存表。

        Args:
            无参数；在首次使用前初始化数据库。

        Returns:
            无返回值。

        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intent_router_namespace (
                    namespace_digest TEXT PRIMARY KEY,
                    config_sha256 TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding_revision TEXT NOT NULL,
                    tokenizer_sha256 TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    expected_example_count INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_router_prototype (
                    namespace_digest TEXT NOT NULL,
                    example_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    vector BLOB NOT NULL,
                    dimension INTEGER NOT NULL,
                    PRIMARY KEY (namespace_digest, example_id),
                    FOREIGN KEY (namespace_digest)
                        REFERENCES intent_router_namespace(namespace_digest)
                        ON DELETE CASCADE
                );
                """
            )

    def load_complete(
        self,
        namespace: PrototypeNamespace,
    ) -> tuple[CachedPrototype, ...]:
        """读取完整且身份精确匹配的 namespace。

        Args:
            namespace: 当前配置与 embedding 身份。

        Returns:
            完整缓存；不存在、半写或校验失败时返回空元组。

        """
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT config_sha256, embedding_model, embedding_revision,
                           tokenizer_sha256, dimension, expected_example_count,
                           complete
                    FROM intent_router_namespace
                    WHERE namespace_digest = ?
                    """,
                    (namespace.digest,),
                ).fetchone()
                if row is None or not _namespace_matches(row, namespace):
                    return ()
                rows = connection.execute(
                    """
                    SELECT example_id, operation, text_sha256, vector, dimension
                    FROM intent_router_prototype
                    WHERE namespace_digest = ?
                    ORDER BY example_id
                    """,
                    (namespace.digest,),
                ).fetchall()
        except (OSError, sqlite3.DatabaseError, ValueError, struct.error):
            return ()
        if len(rows) != namespace.expected_example_count:
            return ()
        try:
            return tuple(
                CachedPrototype(
                    example_id=str(row["example_id"]),
                    operation=PrimaryOperation(str(row["operation"])),
                    text_sha256=str(row["text_sha256"]),
                    vector=_decode_vector(
                        bytes(row["vector"]),
                        dimension=int(row["dimension"]),
                    ),
                )
                for row in rows
            )
        except (TypeError, ValueError, struct.error):
            return ()

    def publish(
        self,
        namespace: PrototypeNamespace,
        prototypes: tuple[CachedPrototype, ...],
    ) -> None:
        """单事务发布完整 namespace，并仅保留最近两版。

        Args:
            namespace: 当前配置与 embedding 身份。
            prototypes: 全量且已校验的 prototype vectors。

        Returns:
            无返回值。

        Raises:
            ValueError: prototype 数量、维度或 ID 不符合 namespace。

        """
        _validate_publish_inputs(namespace, prototypes)
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM intent_router_namespace "
                    "WHERE namespace_digest = ?",
                    (namespace.digest,),
                )
                connection.execute(
                    """
                    INSERT INTO intent_router_namespace (
                        namespace_digest, config_sha256, embedding_model,
                        embedding_revision, tokenizer_sha256, dimension,
                        expected_example_count, complete, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        namespace.digest,
                        namespace.config_sha256,
                        namespace.embedding_model,
                        namespace.embedding_revision,
                        namespace.tokenizer_sha256,
                        namespace.dimension,
                        namespace.expected_example_count,
                        created_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO intent_router_prototype (
                        namespace_digest, example_id, operation, text_sha256,
                        vector, dimension
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    tuple(
                        (
                            namespace.digest,
                            prototype.example_id,
                            prototype.operation.value,
                            prototype.text_sha256,
                            _encode_vector(prototype.vector),
                            namespace.dimension,
                        )
                        for prototype in prototypes
                    ),
                )
                connection.execute(
                    "UPDATE intent_router_namespace SET complete = 1 "
                    "WHERE namespace_digest = ?",
                    (namespace.digest,),
                )
                connection.execute(
                    """
                    DELETE FROM intent_router_namespace
                    WHERE namespace_digest IN (
                        SELECT namespace_digest
                        FROM intent_router_namespace
                        ORDER BY created_at DESC, namespace_digest DESC
                        LIMIT -1 OFFSET 2
                    )
                    """
                )
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _namespace_matches(
    row: sqlite3.Row,
    namespace: PrototypeNamespace,
) -> bool:
    return (
        int(row["complete"]) == 1
        and str(row["config_sha256"]) == namespace.config_sha256
        and str(row["embedding_model"]) == namespace.embedding_model
        and str(row["embedding_revision"]) == namespace.embedding_revision
        and str(row["tokenizer_sha256"]) == namespace.tokenizer_sha256
        and int(row["dimension"]) == namespace.dimension
        and int(row["expected_example_count"])
        == namespace.expected_example_count
    )


def _validate_publish_inputs(
    namespace: PrototypeNamespace,
    prototypes: tuple[CachedPrototype, ...],
) -> None:
    if len(prototypes) != namespace.expected_example_count:
        raise ValueError("prototype 数量与 namespace 不一致。")
    if len({prototype.example_id for prototype in prototypes}) != len(
        prototypes
    ):
        raise ValueError("prototype example_id 不能重复。")
    if any(
        len(prototype.vector) != namespace.dimension
        for prototype in prototypes
    ):
        raise ValueError("prototype vector 维度与 namespace 不一致。")


def _encode_vector(vector: tuple[float, ...]) -> bytes:
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("prototype vector 必须是有限数值。")
    return struct.pack(f"<{len(vector)}f", *vector)


def _decode_vector(raw: bytes, *, dimension: int) -> tuple[float, ...]:
    if dimension <= 0 or len(raw) != dimension * 4:
        raise ValueError("prototype vector BLOB 长度无效。")
    vector = struct.unpack(f"<{dimension}f", raw)
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("prototype vector 包含非有限数值。")
    return tuple(vector)
