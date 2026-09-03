"""从 SQLite cache 恢复 Memory/Qdrant revision 的完整 Points。"""

from __future__ import annotations

from typing import Protocol

from rag_app.application.revision_builder import complete_vector_points
from rag_app.core.models import Chunk, RevisionVectorSpec
from rag_app.core.ports import VectorStorePort


class _RecoveryControl(Protocol):
    """Recovery 所需的只读 SQLite 面。"""

    def revision_vector_spec(self, revision_id: str) -> RevisionVectorSpec:
        """读取完整向量 schema。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            完整向量 Revision 规格。

        """
        ...

    def chunk_rows(self, revision_id: str) -> tuple[Chunk, ...]:
        """读取 canonical Chunk。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            持久化 Chunk 序列。

        """
        ...

    def cached_revision_vectors(
        self, revision_id: str
    ) -> dict[str, dict[str, tuple[float, ...]]]:
        """读取可恢复向量。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            slot 和 Chunk 到向量的映射。

        """
        ...


class RevisionRecoveryService:
    """只从可验证持久化状态回填完整 named-vector Points。"""

    def __init__(
        self,
        control: _RecoveryControl,
        vector_store: VectorStorePort,
    ) -> None:
        """保存权威控制面和目标 Vector Store。"""
        self._control = control
        self._vector_store = vector_store

    def backfill(self, revision_id: str, *, slot_id: str | None = None) -> int:
        """幂等恢复完整 Point；slot 参数只用于校验显式请求。

        Args:
            revision_id: 目标 Revision ID。
            slot_id: 用户显式要求校验的向量槽。

        Returns:
            写入的完整 Point 数量。

        """
        spec = self._control.revision_vector_spec(revision_id)
        if slot_id is not None:
            spec.slot(slot_id)
        chunks = self._control.chunk_rows(revision_id)
        cached = self._control.cached_revision_vectors(revision_id)
        vectors = {
            slot.slot_id: tuple(
                cached[slot.slot_id][chunk.chunk_id] for chunk in chunks
            )
            for slot in spec.slots
        }
        self._vector_store.create_revision(spec)
        points = complete_vector_points(
            spec.revision,
            chunks,
            spec.slots,
            vectors,
        )
        self._vector_store.upsert_complete_points(spec, points)
        return len(points)


__all__ = ["RevisionRecoveryService"]
