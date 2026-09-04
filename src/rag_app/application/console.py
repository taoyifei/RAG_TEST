"""P10 控制台只读查询编排。"""

from __future__ import annotations

from rag_app.core.models.console import (
    ChunkPage,
    JobPage,
    RevisionDocumentReport,
    RevisionInspection,
)
from rag_app.core.ports.console_inspection import (
    ConsoleJobStorePort,
    ConsoleRevisionStorePort,
)


class ConsoleInspectionService:
    """把控制台查询限制在 Core Port 和稳定模型内。"""

    def __init__(
        self,
        *,
        revisions: ConsoleRevisionStorePort,
        jobs: ConsoleJobStorePort,
        serving_fingerprint: str,
    ) -> None:
        """保存只读 Store 与当前 serving 指纹。

        Args:
            revisions: Revision 与 Chunk 只读 Port。
            jobs: Job 列表只读 Port。
            serving_fingerprint: 当前组合根的 serving 指纹。

        Returns:
            无返回值。

        """
        self._revisions = revisions
        self._jobs = jobs
        self._serving_fingerprint = serving_fingerprint

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        states: tuple[str, ...] = (),
        page_size: int = 50,
        offset: int = 0,
    ) -> JobPage:
        """读取 scope 绑定的 Job 分页。

        Args:
            project_id: 可选项目过滤。
            knowledge_base_id: 可选知识库过滤。
            states: 可选公开 Job 状态过滤。
            page_size: 单页上限。
            offset: 稳定偏移量。

        Returns:
            安全 Job 分页。

        """
        items, total = self._jobs.list_jobs(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            states=states,
            limit=page_size,
            offset=offset,
        )
        return JobPage(
            items=items,
            total=total,
            page_size=page_size,
            offset=offset,
            next_offset=(
                offset + len(items) if offset + len(items) < total else None
            ),
        )

    def inspect_revision(
        self, project_id: str, knowledge_base_id: str, revision_id: str
    ) -> RevisionInspection:
        """读取 scope 绑定的 Revision 视图。

        Args:
            project_id: 项目 ID。
            knowledge_base_id: 知识库 ID。
            revision_id: Revision ID。

        Returns:
            不含 Secret 或向量的检查视图。

        """
        return self._revisions.inspect_revision(
            project_id,
            knowledge_base_id,
            revision_id,
            serving_fingerprint=self._serving_fingerprint,
        )

    def list_chunks(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        revision_id: str,
        *,
        document_id: str | None = None,
        role: str | None = None,
        section_id: str | None = None,
        neighbor_group_id: str | None = None,
        page_size: int = 50,
        offset: int = 0,
    ) -> ChunkPage:
        """分页读取 canonical Chunk 三视图与来源跨度。

        Args:
            project_id: 项目 ID。
            knowledge_base_id: 知识库 ID。
            revision_id: Revision ID。
            document_id: 可选逻辑文档过滤。
            role: 可选 Chunk role 过滤。
            section_id: 可选 Section 过滤。
            neighbor_group_id: 可选相邻组过滤。
            page_size: 单页上限。
            offset: 稳定偏移量。

        Returns:
            canonical Chunk 分页。

        """
        items, total = self._revisions.list_revision_chunks(
            project_id,
            knowledge_base_id,
            revision_id,
            document_id=document_id,
            role=role,
            section_id=section_id,
            neighbor_group_id=neighbor_group_id,
            limit=page_size,
            offset=offset,
        )
        return ChunkPage(
            items=items,
            total=total,
            page_size=page_size,
            offset=offset,
            next_offset=(
                offset + len(items) if offset + len(items) < total else None
            ),
        )

    def document_reports(
        self, project_id: str, knowledge_base_id: str, revision_id: str
    ) -> tuple[RevisionDocumentReport, ...]:
        """读取 Revision 内全部文档质量报告。

        Args:
            project_id: 项目 ID。
            knowledge_base_id: 知识库 ID。
            revision_id: Revision ID。

        Returns:
            按逻辑文档 ID 排序的报告。

        """
        return self._revisions.revision_document_reports(
            project_id, knowledge_base_id, revision_id
        )


__all__ = ["ConsoleInspectionService"]
