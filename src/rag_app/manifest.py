"""索引 manifest 历史、兼容性门禁与原子导出。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from rag_app.contracts import IndexManifest

__all__ = [
    "ManifestRepository",
    "ManifestState",
    "ReadOnlyManifestRepository",
    "StoredManifest",
    "index_manifest_sha256",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_manifests (
    collection_name TEXT PRIMARY KEY,
    pipeline_fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    snapshot_name TEXT NOT NULL,
    snapshot_checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_manifest
ON index_manifests(state)
WHERE state = 'active';

CREATE TABLE IF NOT EXISTS manifest_revisions (
    manifest_sha256 TEXT PRIMARY KEY,
    collection_name TEXT NOT NULL,
    pipeline_fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    snapshot_name TEXT NOT NULL,
    snapshot_checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_manifest_revisions_collection
ON manifest_revisions(collection_name, created_at);
"""
_SHA256_HEX_LENGTH = 64
_MANIFEST_COLUMNS = frozenset(
    {
        "collection_name",
        "pipeline_fingerprint",
        "manifest_json",
        "manifest_sha256",
        "state",
        "snapshot_name",
        "snapshot_checksum",
        "created_at",
        "activated_at",
    }
)


class ManifestState(StrEnum):
    """manifest 生命周期。"""

    STAGING = "staging"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class StoredManifest:
    """持久化 manifest 及其恢复证据。"""

    manifest: IndexManifest
    manifest_sha256: str
    state: ManifestState
    snapshot_name: str
    snapshot_checksum: str


class ManifestRepository:
    """在 SQLite 中保存不可变索引 manifest 历史。"""

    def __init__(self, database_path: Path) -> None:
        """保存与任务表共享的 SQLite 路径。

        Args:
            database_path: SQLite WAL 数据库文件。

        """
        self._database_path = database_path

    def initialize(self) -> None:
        """初始化 manifest schema。

        Args:
            无参数；初始化当前数据库路径。

        Returns:
            无返回值。

        """
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)

    def stage(
        self,
        manifest: IndexManifest,
        *,
        snapshot_name: str,
        snapshot_checksum: str,
    ) -> StoredManifest:
        """幂等保存尚未通过 alias 公布的 manifest。

        Args:
            manifest: 完整 pipeline 与来源清单。
            snapshot_name: alias 切换前创建的 snapshot 文件名。
            snapshot_checksum: Qdrant 返回的 snapshot SHA256。

        Returns:
            新建或内容完全相同的 manifest 记录。

        Raises:
            ValueError: snapshot 标识不安全，或 collection 已绑定其他内容。

        """
        _validate_snapshot(snapshot_name, snapshot_checksum)
        serialized = _serialize_manifest(manifest)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO index_manifests (
                    collection_name, pipeline_fingerprint, manifest_json,
                    manifest_sha256, state, snapshot_name,
                    snapshot_checksum, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.collection_name,
                    manifest.pipeline_fingerprint,
                    serialized,
                    digest,
                    ManifestState.STAGING.value,
                    snapshot_name,
                    snapshot_checksum,
                    manifest.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO manifest_revisions (
                    manifest_sha256, collection_name, pipeline_fingerprint,
                    manifest_json, snapshot_name, snapshot_checksum,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    manifest.collection_name,
                    manifest.pipeline_fingerprint,
                    serialized,
                    snapshot_name,
                    snapshot_checksum,
                    manifest.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM index_manifests
                WHERE collection_name = ?
                """,
                (manifest.collection_name,),
            ).fetchone()
            stored = _stored_from_row(_require_row(row))
            if (
                stored.manifest_sha256 != digest
                or stored.snapshot_name != snapshot_name
                or stored.snapshot_checksum != snapshot_checksum
            ):
                connection.rollback()
                raise ValueError(
                    "collection 已绑定不同 manifest 或 snapshot。"
                )
            connection.commit()
        return stored

    def activate(self, collection_name: str) -> None:
        """原子激活 alias 已指向的 staging manifest。

        Args:
            collection_name: 已完成 Qdrant alias 切换的物理 collection。

        Returns:
            无返回值。

        Raises:
            LookupError: collection 没有 staging 或 active manifest。

        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state FROM index_manifests
                WHERE collection_name = ?
                """,
                (collection_name,),
            ).fetchone()
            if row is None or row["state"] not in (
                ManifestState.STAGING.value,
                ManifestState.ACTIVE.value,
            ):
                connection.rollback()
                raise LookupError("待激活 manifest 不存在。")
            if row["state"] == ManifestState.ACTIVE.value:
                connection.commit()
                return
            connection.execute(
                """
                UPDATE index_manifests
                SET state = ?
                WHERE state = ?
                """,
                (
                    ManifestState.RETIRED.value,
                    ManifestState.ACTIVE.value,
                ),
            )
            connection.execute(
                """
                UPDATE index_manifests
                SET state = ?, activated_at = CURRENT_TIMESTAMP
                WHERE collection_name = ?
                """,
                (ManifestState.ACTIVE.value, collection_name),
            )
            connection.execute(
                """
                UPDATE manifest_revisions
                SET activated_at = CURRENT_TIMESTAMP
                WHERE manifest_sha256 = (
                    SELECT manifest_sha256 FROM index_manifests
                    WHERE collection_name = ?
                )
                """,
                (collection_name,),
            )
            connection.commit()

    def record_active_revision(
        self,
        manifest: IndexManifest,
        *,
        snapshot_name: str,
        snapshot_checksum: str,
    ) -> StoredManifest:
        """为同一活动 collection 原子追加增量 manifest 修订。

        Args:
            manifest: 增量完成后的完整来源清单。
            snapshot_name: 更新后创建的 Qdrant snapshot。
            snapshot_checksum: snapshot 的 SHA256。

        Returns:
            更新后的活动 manifest。

        Raises:
            LookupError: collection 当前不是活动索引。
            ValueError: pipeline 或 snapshot 不兼容。

        """
        _validate_snapshot(snapshot_name, snapshot_checksum)
        serialized = _serialize_manifest(manifest)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT pipeline_fingerprint FROM index_manifests
                WHERE collection_name = ? AND state = ?
                """,
                (
                    manifest.collection_name,
                    ManifestState.ACTIVE.value,
                ),
            ).fetchone()
            if current is None:
                connection.rollback()
                raise LookupError(
                    "增量 manifest 对应的活动 collection 不存在。"
                )
            if str(current["pipeline_fingerprint"]) != (
                manifest.pipeline_fingerprint
            ):
                connection.rollback()
                raise ValueError("增量 manifest pipeline 不兼容。")
            connection.execute(
                """
                INSERT OR IGNORE INTO manifest_revisions (
                    manifest_sha256, collection_name, pipeline_fingerprint,
                    manifest_json, snapshot_name, snapshot_checksum,
                    created_at, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    digest,
                    manifest.collection_name,
                    manifest.pipeline_fingerprint,
                    serialized,
                    snapshot_name,
                    snapshot_checksum,
                    manifest.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE index_manifests
                SET manifest_json = ?, manifest_sha256 = ?,
                    snapshot_name = ?, snapshot_checksum = ?,
                    created_at = ?, activated_at = CURRENT_TIMESTAMP
                WHERE collection_name = ? AND state = ?
                """,
                (
                    serialized,
                    digest,
                    snapshot_name,
                    snapshot_checksum,
                    manifest.created_at.isoformat(),
                    manifest.collection_name,
                    ManifestState.ACTIVE.value,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM index_manifests
                WHERE collection_name = ? AND state = ?
                """,
                (
                    manifest.collection_name,
                    ManifestState.ACTIVE.value,
                ),
            ).fetchone()
            connection.commit()
        return _stored_from_row(_require_row(row))

    def count_revisions(self, collection_name: str) -> int:
        """返回物理 collection 的不可变 manifest 修订数。

        Args:
            collection_name: 物理 collection 名。

        Returns:
            去重后的历史修订数。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM manifest_revisions
                WHERE collection_name = ?
                """,
                (collection_name,),
            ).fetchone()
        return int(_require_row(row)[0])

    def get_active(self) -> StoredManifest | None:
        """返回当前活动 manifest。

        Args:
            无参数；查询当前 manifest 数据库。

        Returns:
            当前活动 manifest；尚未激活索引时为 ``None``。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM index_manifests
                WHERE state = ?
                """,
                (ManifestState.ACTIVE.value,),
            ).fetchone()
        if row is None:
            return None
        return _stored_from_row(row)

    def get(self, collection_name: str) -> StoredManifest | None:
        """按物理 collection 读取 manifest。

        Args:
            collection_name: 物理 collection 名。

        Returns:
            已持久化记录；不存在时返回 None。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM index_manifests
                WHERE collection_name = ?
                """,
                (collection_name,),
            ).fetchone()
        if row is None:
            return None
        return _stored_from_row(row)

    def list_all(self) -> tuple[StoredManifest, ...]:
        """列出全部物理 collection 的当前 manifest 记录。

        Args:
            无参数。

        Returns:
            按 collection 名称稳定排序的不可变 manifest 元组。

        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM index_manifests
                ORDER BY collection_name
                """
            ).fetchall()
        return tuple(_stored_from_row(row) for row in rows)

    def snapshot_references(self) -> frozenset[tuple[str, str]]:
        """返回当前和历史 manifest 登记的全部 snapshot 引用。

        Args:
            无参数。

        Returns:
            `(collection_name, snapshot_name)` 的不可变集合。

        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT collection_name, snapshot_name
                FROM index_manifests
                UNION
                SELECT collection_name, snapshot_name
                FROM manifest_revisions
                """
            ).fetchall()
        return frozenset(
            (str(row["collection_name"]), str(row["snapshot_name"]))
            for row in rows
        )

    def require_compatible(
        self,
        *,
        collection_name: str,
        pipeline_fingerprint: str,
    ) -> StoredManifest:
        """要求启动配置与活动 manifest 完全兼容。

        Args:
            collection_name: alias 当前指向的物理 collection。
            pipeline_fingerprint: 运行时 pipeline 指纹。

        Returns:
            已验证的活动 manifest。

        Raises:
            ValueError: 没有活动 manifest 或任一契约不一致。

        """
        active = self.get_active()
        if active is None:
            raise ValueError("没有 active manifest，拒绝启动。")
        if active.manifest.collection_name != collection_name:
            raise ValueError("active collection 与运行时配置不一致。")
        if active.manifest.pipeline_fingerprint != pipeline_fingerprint:
            raise ValueError("active pipeline 与运行时配置不一致。")
        return active

    def export_active(self, target: Path) -> str:
        """把活动 manifest 原子导出为规范 JSON。

        Args:
            target: 导出的 JSON 文件。

        Returns:
            导出字节的 SHA256。

        Raises:
            LookupError: 没有活动 manifest。

        """
        active = self.get_active()
        if active is None:
            raise LookupError("没有可导出的 active manifest。")
        payload = (active.manifest.model_dump_json(indent=2) + "\n").encode()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(target)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return hashlib.sha256(payload).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


class ReadOnlyManifestRepository:
    """以 SQLite 只读 URI 查询已存在的索引 manifest。"""

    def __init__(self, database_path: Path) -> None:
        """保存必须已经存在的 SQLite 数据库路径。

        Args:
            database_path: 操作员指定的现有 manifest 数据库。

        Returns:
            无返回值。

        """
        self._database_path = database_path

    def get_active(self) -> StoredManifest | None:
        """只读返回当前活动 manifest。

        Args:
            无参数。

        Returns:
            当前活动 manifest；没有活动行时为 ``None``。

        Raises:
            sqlite3.Error: 数据库不存在、不可读或 SQLite 查询失败。
            ValueError: manifest schema 不完整或活动行无效。

        """
        with self._connect() as connection:
            _require_manifest_schema(connection)
            row = connection.execute(
                """
                SELECT * FROM index_manifests
                WHERE state = ?
                """,
                (ManifestState.ACTIVE.value,),
            ).fetchone()
        if row is None:
            return None
        return _stored_from_row(row)

    def get(self, collection_name: str) -> StoredManifest | None:
        """只读返回指定物理 collection 的 manifest。

        Args:
            collection_name: 物理 collection 名。

        Returns:
            已持久化记录；不存在时返回 ``None``。

        Raises:
            sqlite3.Error: 数据库不存在、不可读或 SQLite 查询失败。
            ValueError: manifest schema 不完整或记录无效。

        """
        with self._connect() as connection:
            _require_manifest_schema(connection)
            row = connection.execute(
                """
                SELECT * FROM index_manifests
                WHERE collection_name = ?
                """,
                (collection_name,),
            ).fetchone()
        if row is None:
            return None
        return _stored_from_row(row)

    def list_all(self) -> tuple[StoredManifest, ...]:
        """只读列出全部物理 collection 的当前 manifest。

        Args:
            无参数。

        Returns:
            按 collection 名称稳定排序的 manifest 元组。

        """
        with self._connect() as connection:
            _require_manifest_schema(connection)
            rows = connection.execute(
                """
                SELECT * FROM index_manifests
                ORDER BY collection_name
                """
            ).fetchall()
        return tuple(_stored_from_row(row) for row in rows)

    def snapshot_references(self) -> frozenset[tuple[str, str]]:
        """只读返回当前及历史 manifest 的 snapshot 引用。

        Args:
            无参数。

        Returns:
            collection 与 snapshot 名称组成的不可变集合。

        """
        with self._connect() as connection:
            _require_manifest_schema(connection)
            rows = connection.execute(
                """
                SELECT collection_name, snapshot_name
                FROM index_manifests
                UNION
                SELECT collection_name, snapshot_name
                FROM manifest_revisions
                """
            ).fetchall()
        return frozenset(
            (str(row["collection_name"]), str(row["snapshot_name"]))
            for row in rows
        )

    def _connect(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]:
        return readonly_sqlite_snapshot(self._database_path)


@contextmanager
def readonly_sqlite_snapshot(
    database_path: Path,
) -> Iterator[sqlite3.Connection]:
    """在临时隔离副本上以 mode=ro 和 query_only 查询 SQLite。

    原主库及 WAL/SHM 只按字节读取。复制前后会冻结源文件集与摘要，因此
    并发变化会失败；SQLite 即使为 WAL 锁创建临时 sidecar，也只会写入
    自动清理的隔离目录。

    Args:
        database_path: 必须已存在的非 symlink SQLite 主库。

    Returns:
        提供只读 SQLite 连接的上下文管理器迭代器。

    Yields:
        读取完整主库与已提交 WAL 的 query-only 连接。

    Raises:
        sqlite3.OperationalError: 主库缺失或不是普通文件。
        ValueError: 任一逻辑 sidecar 不安全。
        RuntimeError: 复制期间源逻辑文件集或内容发生变化。

    """
    before = _sqlite_source_snapshot(database_path)
    with tempfile.TemporaryDirectory(prefix="rag-sqlite-ro-") as temporary:
        copied_main = Path(temporary) / database_path.name
        shutil.copyfile(database_path, copied_main)
        wal_path = Path(f"{database_path}-wal")
        if wal_path.is_file():
            shutil.copyfile(wal_path, Path(f"{copied_main}-wal"))
        if _sqlite_source_snapshot(database_path) != before:
            raise RuntimeError("SQLite 源文件在只读快照期间发生变化。")
        encoded_path = quote(copied_main.as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{encoded_path}?mode=ro",
            timeout=10.0,
            uri=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            query_only = connection.execute(
                "PRAGMA query_only"
            ).fetchone()
            if query_only is None or int(query_only[0]) != 1:
                raise ValueError("SQLite 只读连接未启用 query_only。")
            yield connection
        finally:
            connection.close()
        if _sqlite_source_snapshot(database_path) != before:
            raise RuntimeError("SQLite 源文件在只读查询期间发生变化。")


def _sqlite_source_snapshot(
    database_path: Path,
) -> tuple[tuple[str, str], ...]:
    """冻结 SQLite 主库、WAL 与 SHM 的安全文件集和摘要。"""
    items: list[tuple[str, str]] = []
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{database_path}{suffix}")
        if path.is_symlink():
            raise ValueError("SQLite 逻辑文件不能是 symlink。")
        if not path.exists():
            if not suffix:
                raise sqlite3.OperationalError(
                    "SQLite 主库不存在或不是安全普通文件。"
                )
            continue
        if not path.is_file():
            raise ValueError("SQLite 逻辑路径必须是普通文件。")
        items.append(
            (
                suffix,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return tuple(items)


def _serialize_manifest(manifest: IndexManifest) -> str:
    payload = manifest.model_dump(mode="json")
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def index_manifest_sha256(manifest: IndexManifest) -> str:
    """重算规范化 index manifest 的 SHA256。

    Args:
        manifest: 待摘要的索引 manifest。

    Returns:
        64 位小写十六进制摘要。

    """
    return hashlib.sha256(
        _serialize_manifest(manifest).encode("utf-8")
    ).hexdigest()


def _stored_from_row(row: sqlite3.Row) -> StoredManifest:
    manifest = IndexManifest.model_validate_json(str(row["manifest_json"]))
    stored_digest = str(row["manifest_sha256"])
    actual_digest = index_manifest_sha256(manifest)
    if stored_digest != actual_digest:
        raise ValueError("SQLite index manifest 摘要校验失败。")
    return StoredManifest(
        manifest=manifest,
        manifest_sha256=stored_digest,
        state=ManifestState(str(row["state"])),
        snapshot_name=str(row["snapshot_name"]),
        snapshot_checksum=str(row["snapshot_checksum"]),
    )


def _require_manifest_schema(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = 'index_manifests'
        """
    ).fetchone()
    if table is None:
        raise ValueError("SQLite 缺少 index_manifests schema。")
    columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(index_manifests)"
        ).fetchall()
    }
    if not _MANIFEST_COLUMNS.issubset(columns):
        raise ValueError("SQLite index_manifests schema 不完整。")


def _require_row(row: sqlite3.Row | None) -> sqlite3.Row:
    if row is None:
        raise RuntimeError("SQLite 未返回预期 manifest 行。")
    return row


def _validate_snapshot(name: str, checksum: str) -> None:
    if not name.endswith(".snapshot") or "/" in name or "\\" in name:
        raise ValueError("snapshot_name 必须是安全文件名。")
    if len(checksum) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in checksum
    ):
        raise ValueError("snapshot_checksum 必须是 64 位小写十六进制。")
