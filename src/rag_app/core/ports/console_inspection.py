"""P10 只读控制台检查 Port。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.models.chunk import Chunk
from rag_app.core.models.console import (
    RevisionDocumentReport,
    RevisionInspection,
)
from rag_app.core.models.management import Job


class ConsoleRevisionStorePort(Protocol):
    """读取 Revision、Chunk 与质量报告，不执行写操作。"""

    def inspect_revision(
        self,
        project_id: str,
        knowledge_base_id: str,
        revision_id: str,
        *,
        serving_fingerprint: str,
    ) -> RevisionInspection:
        """读取 scope 绑定的 Revision 检查视图。

        Args:
            project_id: Revision 所属项目。
            knowledge_base_id: Revision 所属知识库。
            revision_id: 待读取的 IndexRevision。
            serving_fingerprint: 当前服务兼容指纹。

        Returns:
            不含 Secret、向量和物理路径的检查视图。

        """

    def list_revision_chunks(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        revision_id: str,
        *,
        document_id: str | None,
        role: str | None,
        section_id: str | None,
        neighbor_group_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[Chunk, ...], int]:
        """按固定字段过滤并分页读取 canonical Chunk。

        Args:
            project_id: Revision 所属项目。
            knowledge_base_id: Revision 所属知识库。
            revision_id: 待读取的 IndexRevision。
            document_id: 可选逻辑文档过滤。
            role: 可选 Chunk role 过滤。
            section_id: 可选 section 过滤。
            neighbor_group_id: 可选结构邻居组过滤。
            limit: 最大返回数。
            offset: 从零开始的分页偏移。

        Returns:
            当前页 canonical Chunk 与过滤后的总数。

        """

    def revision_document_reports(
        self,
        project_id: str,
        knowledge_base_id: str,
        revision_id: str,
    ) -> tuple[RevisionDocumentReport, ...]:
        """读取每个文档的 ParseReport 与 ChunkingReport。

        Args:
            project_id: Revision 所属项目。
            knowledge_base_id: Revision 所属知识库。
            revision_id: 待读取的 IndexRevision。

        Returns:
            按文档身份排序的解析与分块报告。

        """


class ConsoleJobStorePort(Protocol):
    """读取安全 Job 列表。"""

    def list_jobs(
        self,
        *,
        project_id: str | None,
        knowledge_base_id: str | None,
        states: tuple[str, ...],
        limit: int,
        offset: int,
    ) -> tuple[tuple[Job, ...], int]:
        """按 scope 和公开状态分页读取 Job。

        Args:
            project_id: 可选项目过滤。
            knowledge_base_id: 可选知识库过滤。
            states: 可选公开状态集合。
            limit: 最大返回数。
            offset: 从零开始的分页偏移。

        Returns:
            当前页安全 Job 与过滤后的总数。

        """


__all__ = ["ConsoleJobStorePort", "ConsoleRevisionStorePort"]
