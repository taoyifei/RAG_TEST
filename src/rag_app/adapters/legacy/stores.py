"""权威离线 Memory/SQLite Store adapters。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass

from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import Conflict, IndexCompatibilityError
from rag_app.core.events import TraceEvent
from rag_app.core.identifiers import canonical_json
from rag_app.core.models import (
    Chunk,
    LexicalSearchRequest,
    SearchHit,
    VectorSearchRequest,
    VectorWriteRequest,
)
from rag_app.core.ports import (
    BlobReadResult,
    BlobWriteRequest,
    MetadataRecord,
)


@dataclass(frozen=True, slots=True)
class _VectorRow:
    chunk: Chunk
    vector: tuple[float, ...]


class InMemoryVectorStore:
    """按 revision/slot/vector name 三元组隔离的离线 Store。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.VECTOR_STORE,
        name="memory",
        version="1",
        mode=ProviderMode.DETERMINISTIC,
    )

    def __init__(self) -> None:
        """创建空的进程内向量空间。

        Args:
            无参数；不创建网络或磁盘资源。

        Returns:
            无返回值。

        """
        self._rows: dict[tuple[str, str, str], tuple[_VectorRow, ...]] = {}
        self._closed = False
        self.close_count = 0

    def write(self, request: VectorWriteRequest) -> None:
        """幂等写入一个显式向量空间。

        Args:
            request: revision、slot、vector name、chunks 和向量。

        Returns:
            无返回值。

        Raises:
            ValueError: 数量或向量维度不一致。
            Conflict: 同一空间已经存在不同内容。

        """
        if len(request.chunks) != len(request.vectors):
            raise ValueError("vector write 的 chunk/vector 数量必须一致。")
        dimensions = {len(vector) for vector in request.vectors}
        if len(dimensions) > 1 or 0 in dimensions:
            raise ValueError("vector write 的维度必须一致且非零。")
        key = (
            request.revision.index_revision_id,
            request.slot_id,
            request.vector_name,
        )
        rows = tuple(
            _VectorRow(chunk=chunk, vector=tuple(vector))
            for chunk, vector in zip(
                request.chunks,
                request.vectors,
                strict=True,
            )
        )
        existing = self._rows.get(key)
        if existing is not None and existing != rows:
            raise Conflict(
                "同一 vector 空间已经存在不同内容。",
                stage="vector_store.write",
            )
        self._rows[key] = rows

    def search(self, request: VectorSearchRequest) -> tuple[SearchHit, ...]:
        """只查询精确匹配的 revision/slot/vector name。

        Args:
            request: 显式空间、向量和上限。

        Returns:
            余弦分数降序的 Core 命中。

        Raises:
            IndexCompatibilityError: 精确空间不存在或维度不匹配。

        """
        key = (
            request.revision.index_revision_id,
            request.slot_id,
            request.vector_name,
        )
        rows = self._rows.get(key)
        if rows is None:
            raise IndexCompatibilityError(
                "请求的 revision/slot/vector name 不存在。",
                stage="vector_store.search",
            )
        if rows and len(rows[0].vector) != len(request.query_vector):
            raise IndexCompatibilityError(
                "查询向量维度与目标空间不匹配。",
                stage="vector_store.search",
            )
        scored = [
            (row, _cosine(tuple(request.query_vector), row.vector))
            for row in rows
        ]
        scored.sort(key=lambda item: (-item[1], item[0].chunk.chunk_id))
        return tuple(
            SearchHit(
                chunk=row.chunk,
                score=score,
                rank=rank,
                channels=(f"dense:{request.slot_id}",),
            )
            for rank, (row, score) in enumerate(
                scored[: request.limit],
                start=1,
            )
        )

    def validate_revision(self, revision: object) -> None:
        """确认 Store 至少含有该 revision 的空间。

        Args:
            revision: 带 `index_revision_id` 的 Core revision 引用。

        Returns:
            无返回值。

        Raises:
            IndexCompatibilityError: revision 没有任何空间。

        """
        revision_id = getattr(revision, "index_revision_id", None)
        if not isinstance(revision_id, str) or not any(
            key[0] == revision_id for key in self._rows
        ):
            raise IndexCompatibilityError(
                "Vector Store 不包含目标 revision。",
                stage="vector_store.validate",
            )

    def close(self) -> None:
        """幂等关闭进程内 Store。

        Args:
            无参数；释放当前内容引用。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self._rows.clear()


class InMemoryLexicalStore:
    """按简单 token overlap 提供统一排名语义的离线 Store。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.LEXICAL_STORE,
        name="memory",
        version="1",
        mode=ProviderMode.DETERMINISTIC,
    )

    def __init__(self) -> None:
        """创建空 Store。

        Args:
            无参数；不创建外部资源。

        Returns:
            无返回值。

        """
        self._chunks: dict[str, Chunk] = {}
        self._closed = False
        self.close_count = 0

    def write(self, chunks: tuple[Chunk, ...]) -> None:
        """按 chunk ID 幂等写入。

        Args:
            chunks: Core 分块序列。

        Returns:
            无返回值。

        """
        for chunk in chunks:
            existing = self._chunks.get(chunk.chunk_id)
            if existing is not None and existing != chunk:
                raise ValueError("同一 chunk ID 的词法内容不一致。")
            self._chunks[chunk.chunk_id] = chunk

    def search(self, request: LexicalSearchRequest) -> tuple[SearchHit, ...]:
        """按 query 字符集合重叠生成有序命中。

        Args:
            request: 词法查询和上限。

        Returns:
            分数降序的 Core 命中。

        """
        query_tokens = _tokens(request.query)
        scored = [
            (chunk, float(len(query_tokens & _tokens(chunk.citation_text))))
            for chunk in self._chunks.values()
        ]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda item: (-item[1], item[0].chunk_id))
        return tuple(
            SearchHit(
                chunk=chunk,
                score=score,
                rank=rank,
                channels=("lexical",),
            )
            for rank, (chunk, score) in enumerate(
                scored[: request.limit],
                start=1,
            )
        )

    def close(self) -> None:
        """幂等关闭 Store。

        Args:
            无参数；释放当前内容引用。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self._chunks.clear()


class SqliteMetadataStore:
    """使用私有内存 SQLite 的 P01 元数据 Store。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.METADATA_STORE,
        name="sqlite",
        version=sqlite3.sqlite_version,
        mode=ProviderMode.LOCAL,
    )

    def __init__(self) -> None:
        """创建不触碰现有数据库 schema 的内存连接。

        Args:
            无参数；固定使用 `:memory:`。

        Returns:
            无返回值。

        """
        self._connection = sqlite3.connect(":memory:")
        self._connection.execute(
            "CREATE TABLE metadata (namespace TEXT, key TEXT, value TEXT, "
            "PRIMARY KEY(namespace, key))"
        )
        self._closed = False
        self.close_count = 0

    def put(self, record: MetadataRecord) -> None:
        """幂等覆盖一条结构化记录。

        Args:
            record: 命名空间、键和值。

        Returns:
            无返回值。

        """
        self._connection.execute(
            "INSERT INTO metadata(namespace, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value",
            (record.namespace, record.key, canonical_json(record.value)),
        )
        self._connection.commit()

    def get(self, namespace: str, key: str) -> MetadataRecord | None:
        """读取一条记录。

        Args:
            namespace: 受控命名空间。
            key: 记录键。

        Returns:
            找到的记录，否则为 None。

        """
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()
        if row is None:
            return None
        return MetadataRecord(
            namespace=namespace,
            key=key,
            value=json.loads(str(row[0])),
        )

    def close(self) -> None:
        """幂等关闭 SQLite 连接。

        Args:
            无参数；关闭私有内存连接。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self._connection.close()


class InMemoryBlobStore:
    """校验摘要的离线 Blob Store。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.BLOB_STORE,
        name="local",
        version="1",
        mode=ProviderMode.LOCAL,
    )

    def __init__(self) -> None:
        """创建空 Store。

        Args:
            无参数；不触碰宿主文件系统。

        Returns:
            无返回值。

        """
        self._items: dict[str, BlobReadResult] = {}
        self._closed = False
        self.close_count = 0

    def put(self, request: BlobWriteRequest) -> None:
        """校验摘要后幂等写入。

        Args:
            request: blob 身份、摘要、媒体类型和内容。

        Returns:
            无返回值。

        """
        observed = hashlib.sha256(request.content).hexdigest()
        if observed != request.content_sha256:
            raise ValueError("blob 内容摘要不匹配。")
        result = BlobReadResult(**request.model_dump())
        existing = self._items.get(request.blob_id)
        if existing is not None and existing != result:
            raise ValueError("同一 blob ID 的内容不一致。")
        self._items[request.blob_id] = result

    def get(self, blob_id: str) -> BlobReadResult | None:
        """读取一个 blob。

        Args:
            blob_id: blob 身份。

        Returns:
            找到的结果，否则为 None。

        """
        return self._items.get(blob_id)

    def close(self) -> None:
        """幂等关闭 Store。

        Args:
            无参数；释放内容引用。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self._items.clear()


class SqliteTraceSink:
    """使用私有内存 SQLite 保存结构化 Trace 事件。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.TRACE_SINK,
        name="sqlite",
        version=sqlite3.sqlite_version,
        mode=ProviderMode.LOCAL,
    )

    def __init__(self) -> None:
        """创建私有内存事件表。

        Args:
            无参数；不触碰现有数据库 schema。

        Returns:
            无返回值。

        """
        self._connection = sqlite3.connect(":memory:")
        self._connection.execute(
            "CREATE TABLE events (trace_id TEXT, event_name TEXT, payload TEXT)"
        )
        self._closed = False
        self.close_count = 0

    def record(self, event: TraceEvent) -> None:
        """记录一个已脱敏 Core 事件。

        Args:
            event: 结构化安全事件。

        Returns:
            无返回值。

        """
        self._connection.execute(
            "INSERT INTO events(trace_id, event_name, payload) "
            "VALUES (?, ?, ?)",
            (
                event.trace_id,
                event.event_name,
                canonical_json(event.model_dump(mode="json")),
            ),
        )
        self._connection.commit()

    def close(self) -> None:
        """幂等关闭 SQLite 连接。

        Args:
            无参数；关闭私有连接。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self._connection.close()


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("向量 Store 禁止零长度向量。")
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )


def _tokens(value: str) -> set[str]:
    return {
        character.casefold() for character in value if not character.isspace()
    }
