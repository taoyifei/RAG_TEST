"""Active snapshot 与 canonical Chunk hydration 同步端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    HydratedChunk,
    KnowledgeBaseScope,
    RetrievalPolicy,
)


class EvidenceSourcePort(Protocol):
    """只从权威控制面读取查询快照和可引用 Chunk。"""

    def active_query_snapshot(
        self,
        scope: KnowledgeBaseScope,
        *,
        serving_fingerprint: str,
        retrieval_policy: RetrievalPolicy,
    ) -> ActiveRevisionQuerySnapshot:
        """在一个事务中冻结当前 Active Revision。

        Args:
            scope: 项目和知识库边界。
            serving_fingerprint: 当前实际 serving 语义摘要。
            retrieval_policy: P07 provisional 执行策略。

        Returns:
            请求内不可变的 Active Revision snapshot。

        """
        ...

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        """批量回读并复核 canonical chunks。

        Args:
            snapshot: 请求级 Active Revision snapshot。
            chunk_ids: 有界且需要保序的候选 ID。

        Returns:
            canonical chunks 和显示身份。

        """
        ...

    def section_chunk_ids(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        *,
        document_version_id: str,
        section_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        """返回同 revision/document/section 的有界 Chunk ID。

        Args:
            snapshot: 请求级 Active Revision snapshot。
            document_version_id: immutable 文档版本身份。
            section_id: canonical section 身份。
            limit: 最大返回数。

        Returns:
            稳定排序的 Chunk ID。

        """
        ...


__all__ = ["EvidenceSourcePort"]
