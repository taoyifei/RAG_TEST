"""P06 持久化组件的严格配置和可信工厂。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from qdrant_client import QdrantClient

from rag_app.adapters.stores import (
    FilesystemBlobStore,
    MemoryRevisionVectorStore,
    MigrationRunner,
    QdrantRevisionVectorStore,
    SqliteConnectionFactory,
    SqliteControlStore,
    SqliteFtsStore,
)
from rag_app.core.models.common import JsonObject


class LocalPersistenceConfig(BaseModel):
    """拒绝未知字段的 P06 本地 Store 配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_root: str = Field(default=".data", min_length=1)
    sqlite_filename: str = Field(
        default="universal-rag.sqlite3",
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    journal_mode: str = Field(default="WAL", pattern=r"^(WAL|DELETE|MEMORY)$")
    busy_timeout_ms: StrictInt = Field(default=5000, ge=1, le=60000)
    qdrant_mode: str = Field(default="memory", pattern=r"^(memory|path|url)$")
    qdrant_url: str | None = Field(
        default=None,
        pattern=r"^https?://[^\s]+$",
    )


def sqlite_control_factory(config: JsonObject) -> SqliteControlStore:
    """创建已迁移的 SQLite 控制面。

    Args:
        config: 已通过 Registry 校验的本地配置。

    Returns:
        SQLite 控制面实例。

    """
    return SqliteControlStore(_migrated_connections(config))


def sqlite_fts_factory(config: JsonObject) -> SqliteFtsStore:
    """创建共享同一 schema 的 SQLite FTS5 Store。

    Args:
        config: 已通过 Registry 校验的本地配置。

    Returns:
        SQLite FTS5 Store 实例。

    """
    return SqliteFtsStore(_migrated_connections(config))


def filesystem_blob_factory(config: JsonObject) -> FilesystemBlobStore:
    """创建 content-addressed Filesystem Blob Store。

    Args:
        config: 已通过 Registry 校验的本地配置。

    Returns:
        Filesystem Blob Store 实例。

    """
    resolved = LocalPersistenceConfig.model_validate(dict(config))
    return FilesystemBlobStore(resolved.data_root)


def qdrant_local_factory(config: JsonObject) -> QdrantRevisionVectorStore:
    """按 Profile 显式选择 Qdrant local-memory 或 local-path。

    Args:
        config: 含实际 Qdrant 模式的本地配置。

    Returns:
        Qdrant Revision Store 实例。

    """
    resolved = LocalPersistenceConfig.model_validate(dict(config))
    location: str | Path = ":memory:"
    if resolved.qdrant_mode == "url":
        if resolved.qdrant_url is None:
            raise ValueError("Qdrant url 模式缺少 RAG_QDRANT_URL。")
        return QdrantRevisionVectorStore(
            client=QdrantClient(
                url=resolved.qdrant_url,
                check_compatibility=False,
            )
        )
    if resolved.qdrant_mode == "path":
        location = Path(resolved.data_root) / "qdrant"
    return QdrantRevisionVectorStore(location)


def memory_revision_vector_factory() -> MemoryRevisionVectorStore:
    """创建支持完整 named-vector Point 的离线 Memory Store。

    Args:
        无参数；创建空 Store。

    Returns:
        Memory Revision Vector Store 实例。

    """
    return MemoryRevisionVectorStore()


def _migrated_connections(config: JsonObject) -> SqliteConnectionFactory:
    resolved = LocalPersistenceConfig.model_validate(dict(config))
    connections = SqliteConnectionFactory(
        Path(resolved.data_root) / resolved.sqlite_filename,
        busy_timeout_ms=resolved.busy_timeout_ms,
        journal_mode=resolved.journal_mode,
    )
    MigrationRunner(
        connections,
        _migrations_directory(),
    ).migrate()
    return connections


def _migrations_directory() -> Path:
    configured = os.environ.get("RAG_MIGRATIONS_DIR")
    if configured is not None:
        return Path(configured)
    image_migrations = Path("/app/migrations/universal_rag")
    if image_migrations.is_dir():
        return image_migrations
    return Path(__file__).resolve().parents[3] / "migrations" / "universal_rag"


__all__ = [
    "LocalPersistenceConfig",
    "filesystem_blob_factory",
    "memory_revision_vector_factory",
    "qdrant_local_factory",
    "sqlite_control_factory",
    "sqlite_fts_factory",
]
