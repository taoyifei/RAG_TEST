"""协调 SQLite 来源状态与 Qdrant 版本切换。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from rag_app.index.qdrant import IndexedChunk, QdrantIndex
from rag_app.state import SourceVersion, StateStore, VersionState

__all__ = ["IndexCoordinator", "IndexResult", "IndexResultState"]

ChunkBuilder = Callable[[SourceVersion], Sequence[IndexedChunk]]


class IndexResultState(StrEnum):
    """单文档索引结果。"""

    ACTIVATED = "activated"
    UNCHANGED = "unchanged"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class IndexResult:
    """一次单文档索引的可审计结果。"""

    source_id: str
    doc_version: str
    chunk_count: int
    state: IndexResultState


class IndexCoordinator:
    """按 Qdrant 先行、SQLite 后确认的顺序切换文档版本。"""

    def __init__(self, state: StateStore, index: QdrantIndex) -> None:
        """保存两个持久存储。

        Args:
            state: SQLite WAL 状态库。
            index: 当前 pipeline 的 Qdrant 物理 collection。

        """
        self._state = state
        self._index = index

    def index_source(
        self,
        *,
        job_id: str,
        source_path: str,
        content_sha256: str,
        build_chunks: ChunkBuilder,
    ) -> IndexResult:
        """幂等构建并激活一个来源版本。

        Args:
            job_id: 已持久化索引任务标识。
            source_path: 当前相对路径。
            content_sha256: DOCX 内容摘要。
            build_chunks: 根据持久 source/version 生成完整编码 chunk。

        Returns:
            激活、无变化或崩溃恢复结果。

        Raises:
            ValueError: chunk 集合为空、重复或身份不匹配。
            Exception: 解析、编码或外部索引操作失败。

        """
        version = self._state.stage_source_version(
            job_id=job_id,
            source_path=source_path,
            content_sha256=content_sha256,
            pipeline_fingerprint=self._index.pipeline_fingerprint,
        )
        if version.state == VersionState.ACTIVE:
            return self._require_active_version(version)

        if version.chunk_count is not None:
            active_count = self._index.count_version(
                version.source_id,
                version.doc_version,
                "active",
            )
            if active_count == version.chunk_count:
                self._state.activate_source_version(
                    version.source_id,
                    version.doc_version,
                )
                return IndexResult(
                    source_id=version.source_id,
                    doc_version=version.doc_version,
                    chunk_count=active_count,
                    state=IndexResultState.RECOVERED,
                )

        try:
            chunks = tuple(build_chunks(version))
            self._validate_chunks(version, chunks)
            self._index.delete_staging(
                version.source_id,
                version.doc_version,
            )
            self._index.stage_chunks(chunks)
            expected_count = len(chunks)
            actual_count = self._index.count_version(
                version.source_id,
                version.doc_version,
                "staging",
            )
            if actual_count != expected_count:
                raise RuntimeError(
                    "Qdrant staging 点数与完整 chunk 集合不一致。"
                )
            self._state.record_staged_chunk_count(
                version.source_id,
                version.doc_version,
                expected_count,
            )
            self._index.activate_source_version(
                version.source_id,
                version.doc_version,
            )
        except Exception as error:
            self._index.delete_staging(
                version.source_id,
                version.doc_version,
            )
            self._state.fail_source_version(
                version.source_id,
                version.doc_version,
                error_code=type(error).__name__,
            )
            raise

        self._state.activate_source_version(
            version.source_id,
            version.doc_version,
        )
        return IndexResult(
            source_id=version.source_id,
            doc_version=version.doc_version,
            chunk_count=len(chunks),
            state=IndexResultState.ACTIVATED,
        )

    def delete_source(self, source_id: str) -> None:
        """先停止 Qdrant 证据，再记录来源已删除。

        Args:
            source_id: 持久来源标识。

        """
        self._index.retire_source(source_id)
        self._state.mark_source_deleted(source_id)

    def _require_active_version(
        self,
        version: SourceVersion,
    ) -> IndexResult:
        if version.chunk_count is None:
            raise RuntimeError("活动来源版本缺少 chunk_count。")
        active_count = self._index.count_version(
            version.source_id,
            version.doc_version,
            "active",
        )
        if active_count != version.chunk_count:
            raise RuntimeError("SQLite 与 Qdrant 活动版本点数不一致。")
        return IndexResult(
            source_id=version.source_id,
            doc_version=version.doc_version,
            chunk_count=active_count,
            state=IndexResultState.UNCHANGED,
        )

    def _validate_chunks(
        self,
        version: SourceVersion,
        chunks: Sequence[IndexedChunk],
    ) -> None:
        if not chunks:
            raise ValueError("一个可激活 DOCX 至少需要一个证据 chunk。")
        chunk_ids = {indexed.chunk.chunk_id for indexed in chunks}
        if len(chunk_ids) != len(chunks):
            raise ValueError("同一来源版本含重复 chunk ID。")
        for indexed in chunks:
            chunk = indexed.chunk
            if (
                chunk.source_id != version.source_id
                or chunk.doc_version != version.doc_version
                or chunk.pipeline_fingerprint
                != self._index.pipeline_fingerprint
            ):
                raise ValueError("chunk 身份与 staging 来源版本不一致。")
