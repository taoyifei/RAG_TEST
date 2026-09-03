"""P09 生命周期、幂等与公开作业同步端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.models import DocumentRef
from rag_app.core.models.management import (
    ArtifactDescriptor,
    Document,
    DocumentVersion,
    Job,
    KnowledgeBase,
    KnowledgeBaseStatus,
    Project,
    ProjectStatus,
    QueuedIngestion,
)


class LifecycleStorePort(Protocol):
    """隔离 Application 与 SQLite 行类型的生命周期端口。"""

    def create_project(self, project_id: str, name: str) -> Project:
        """创建项目。

        Args:
            project_id: 服务端项目 ID。
            name: 项目显示名。

        Returns:
            持久化项目。

        """
        ...

    def get_project(self, project_id: str) -> Project:
        """读取项目。

        Args:
            project_id: 目标项目 ID。

        Returns:
            项目视图。

        """
        ...

    def list_projects(self, *, limit: int, offset: int) -> tuple[Project, ...]:
        """稳定分页读取项目。

        Args:
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页项目。

        """
        ...

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        status: ProjectStatus | None = None,
    ) -> Project:
        """更新项目。

        Args:
            project_id: 目标项目 ID。
            name: 可选显示名。
            status: 可选状态。

        Returns:
            更新后的项目。

        """
        ...

    def create_knowledge_base(
        self,
        knowledge_base_id: str,
        project_id: str,
        name: str,
        *,
        profile_id: str,
        description: str,
    ) -> KnowledgeBase:
        """创建知识库。

        Args:
            knowledge_base_id: 服务端知识库 ID。
            project_id: 所属项目 ID。
            name: 显示名。
            profile_id: 冻结 Profile ID。
            description: 可选说明。

        Returns:
            持久化知识库。

        """
        ...

    def get_knowledge_base(
        self, project_id: str, knowledge_base_id: str
    ) -> KnowledgeBase:
        """按 scope 读取知识库。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。

        Returns:
            知识库视图。

        """
        ...

    def list_knowledge_bases(
        self, project_id: str, *, limit: int, offset: int
    ) -> tuple[KnowledgeBase, ...]:
        """分页读取知识库。

        Args:
            project_id: 所属项目 ID。
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页知识库。

        """
        ...

    def update_knowledge_base(
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: KnowledgeBaseStatus | None = None,
    ) -> KnowledgeBase:
        """更新知识库。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。
            name: 可选名称。
            description: 可选说明。
            status: 可选状态。

        Returns:
            更新后的知识库。

        """
        ...

    def mark_knowledge_base_deleting(
        self, project_id: str, knowledge_base_id: str
    ) -> KnowledgeBase:
        """创建知识库受控删除操作。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。

        Returns:
            deleting 状态知识库。

        """
        ...

    def get_document(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """按 scope 读取文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            文档视图。

        """
        ...

    def list_documents(
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[Document, ...]:
        """分页读取文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页文档。

        """
        ...

    def rename_document(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        display_name: str,
    ) -> Document:
        """只更新文档显示名。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            display_name: 新显示名。

        Returns:
            更新后的文档。

        """
        ...

    def mark_document_deleting(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """把文档置为受控删除状态。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            deleting 状态文档。

        """
        ...

    def list_document_versions(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> tuple[DocumentVersion, ...]:
        """读取文档版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            不可变版本序列。

        """
        ...

    def get_document_version(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
    ) -> DocumentVersion:
        """读取单个文档版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            document_version_id: 目标版本 ID。

        Returns:
            版本视图。

        """
        ...

    def mark_version_ready(
        self, document_id: str, document_version_id: str
    ) -> None:
        """推进新版本并淘汰旧 Ready 版本。

        Args:
            document_id: 目标文档 ID。
            document_version_id: 新 Ready 版本 ID。

        Returns:
            无返回值。

        """
        ...

    def claim_idempotency(
        self,
        *,
        scope_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        result_id: str,
    ) -> str:
        """持久化幂等身份。

        Args:
            scope_id: 操作 scope ID。
            operation: 稳定操作名。
            idempotency_key: 调用方幂等键。
            request_hash: canonical request 指纹。
            result_id: 首次预分配结果 ID。

        Returns:
            首次或既有结果 ID。

        """
        ...

    def bind_job_document(
        self, job_id: str, document_id: str, document_version_id: str
    ) -> None:
        """绑定 Job 与文档版本。

        Args:
            job_id: 目标 Job ID。
            document_id: 逻辑文档 ID。
            document_version_id: 文档版本 ID。

        Returns:
            无返回值。

        """
        ...

    def enqueue_ingestion(
        self, request: QueuedIngestion, *, idempotency_key: str
    ) -> Job:
        """原子保存 Job、目标版本和无正文构建请求。

        Args:
            request: 冻结 Revision 构建请求。
            idempotency_key: 原始调用方幂等键。

        Returns:
            queued 或既有 Job。

        """
        ...

    def claim_ingestion(self, job_id: str) -> QueuedIngestion | None:
        """领取单个持久构建请求。

        Args:
            job_id: 目标 Job ID。

        Returns:
            成功领取的请求；不可领取时返回 None。

        """
        ...

    def pending_ingestion_jobs(self) -> tuple[str, ...]:
        """恢复启动时尚未完成的构建请求。

        Args:
            无参数；读取持久队列。

        Returns:
            稳定 Job ID 序列。

        """
        ...

    def finish_ingestion(
        self,
        job_id: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
        safe_message: str | None = None,
        retryable: bool = False,
    ) -> None:
        """持久化构建请求终态和兜底安全错误。

        Args:
            job_id: 目标 Job ID。
            succeeded: 是否成功。
            error_code: 可选稳定错误码。
            safe_message: 可选安全消息。
            retryable: 是否可安全重试。

        Returns:
            无返回值。

        """
        ...

    def get_job(self, job_id: str) -> Job:
        """读取公开 Job。

        Args:
            job_id: 目标 Job ID。

        Returns:
            安全 Job 视图。

        """
        ...

    def list_artifacts(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
    ) -> tuple[ArtifactDescriptor, ...]:
        """按引用 scope 列出 Artifact。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 所属文档 ID。
            document_version_id: 所属版本 ID。

        Returns:
            Artifact 摘要序列。

        """
        ...

    def authorize_artifact(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        artifact_id: str,
    ) -> ArtifactDescriptor:
        """校验 Artifact 引用授权。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 所属文档 ID。
            document_version_id: 所属版本 ID。
            artifact_id: 目标 Artifact ID。

        Returns:
            已授权 Artifact 摘要。

        """
        ...


class ActiveDocumentStorePort(Protocol):
    """提供 Active Revision 文档快照。"""

    def active_documents(
        self, knowledge_base_id: str
    ) -> tuple[tuple[DocumentRef, str, str], ...]:
        """读取 Active Revision 的来源文档。

        Args:
            knowledge_base_id: 目标知识库 ID。

        Returns:
            文档引用、Artifact ID 与媒体类型序列。

        """
        ...


__all__ = ["ActiveDocumentStorePort", "LifecycleStorePort"]
