"""P09 同步 Python SDK facade。"""

from __future__ import annotations

from collections.abc import Callable

from rag_app.application.console import ConsoleInspectionService
from rag_app.application.lifecycle import LifecycleService
from rag_app.application.retrieval import RetrievalService
from rag_app.core.errors import CapabilityUnavailable, NotFound
from rag_app.core.events import TraceEvent
from rag_app.core.models import (
    ArtifactDescriptor,
    ChunkPage,
    JobPage,
    KnowledgeBaseScope,
    KnowledgeBaseStatus,
    ProjectStatus,
    RevisionDocumentReport,
    RevisionInspection,
    SearchRequest,
)
from rag_app.core.models.management import (
    Document,
    DocumentVersion,
    Job,
    KnowledgeBase,
    Project,
    SystemStatus,
)
from rag_app.core.models.search import RetrievalDiagnostics, SearchAnswerResult
from rag_app.core.ports import BlobReadResult


class RagSdk:
    """SDK 与 HTTP 共享的同步稳定 facade。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        lifecycle: LifecycleService,
        retrieval: RetrievalService,
        get_job: Callable[[str], Job],
        cancel_job: Callable[[str], Job],
        retry_job: Callable[[str], Job],
        submit_job: Callable[[str], None],
        trace_events: Callable[[str], tuple[TraceEvent, ...]],
        system_status: Callable[[], SystemStatus],
        close: Callable[[], None],
        console: ConsoleInspectionService | None = None,
        retrieval_resolver: Callable[[str, RetrievalService], RetrievalService]
        | None = None,
        revision_builder_resolver: Callable[
            [str, LifecycleService], LifecycleService
        ]
        | None = None,
    ) -> None:
        """保存 Application Services，不持有具体 Store 类型。

        Args:
            lifecycle: 生命周期应用服务。
            retrieval: P07 检索应用服务。
            get_job: 持久化 Job 查询函数。
            cancel_job: 持久化取消函数。
            retry_job: 持久化重试函数。
            submit_job: 有界 Worker 调度函数。
            trace_events: 安全 Trace 事件读取函数。
            system_status: 只读系统状态函数。
            close: 组合根关闭函数。
            console: 可选 P10 只读控制台服务。
            retrieval_resolver: 可选按知识库选择检索服务的解析器。
            revision_builder_resolver: 可选按知识库选择 Revision
                构建服务的解析器。

        Returns:
            无返回值。

        """
        self._lifecycle = lifecycle
        self._retrieval = retrieval
        self._get_job = get_job
        self._cancel_job = cancel_job
        self._retry_job = retry_job
        self._submit_job = submit_job
        self._trace_events = trace_events
        self._system_status = system_status
        self._close = close
        self._console = console
        self._retrieval_resolver = retrieval_resolver
        self._revision_builder_resolver = revision_builder_resolver
        self._closed = False
        self._diagnostics: dict[str, RetrievalDiagnostics] = {}

    def create_project(
        self, name: str, *, idempotency_key: str | None = None
    ) -> Project:
        """创建项目。

        Args:
            name: 项目显示名。
            idempotency_key: 可选持久化幂等键。

        Returns:
            新项目。

        """
        self._require_open()
        return self._lifecycle.create_project(
            name, idempotency_key=idempotency_key
        )

    def get_project(self, project_id: str) -> Project:
        """读取项目。

        Args:
            project_id: 目标项目 ID。

        Returns:
            项目视图。

        """
        self._require_open()
        return self._lifecycle.get_project(project_id)

    def list_projects(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[Project, ...]:
        """分页读取项目。

        Args:
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页项目。

        """
        self._require_open()
        return self._lifecycle.list_projects(limit=limit, offset=offset)

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        status: ProjectStatus | None = None,
    ) -> Project:
        """更新项目显示名或状态。

        Args:
            project_id: 目标项目 ID。
            name: 可选显示名。
            status: 可选生命周期状态。

        Returns:
            更新后的项目。

        """
        self._require_open()
        return self._lifecycle.update_project(
            project_id, name=name, status=status
        )

    def create_knowledge_base(
        self,
        project_id: str,
        name: str,
        *,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> KnowledgeBase:
        """创建知识库。

        Args:
            project_id: 所属项目 ID。
            name: 知识库显示名。
            description: 可选说明。
            idempotency_key: 可选持久化幂等键。

        Returns:
            新知识库。

        """
        self._require_open()
        return self._lifecycle.create_knowledge_base(
            project_id,
            name,
            description=description,
            idempotency_key=idempotency_key,
        )

    def get_knowledge_base(
        self, project_id: str, knowledge_base_id: str
    ) -> KnowledgeBase:
        """读取知识库。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。

        Returns:
            知识库视图。

        """
        self._require_open()
        return self._lifecycle.get_knowledge_base(project_id, knowledge_base_id)

    def delete_knowledge_base(
        self, project_id: str, knowledge_base_id: str
    ) -> KnowledgeBase:
        """创建受控知识库删除操作。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。

        Returns:
            deleting 状态知识库。

        """
        self._require_open()
        return self._lifecycle.delete_knowledge_base(
            project_id, knowledge_base_id
        )

    def list_knowledge_bases(
        self,
        project_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[KnowledgeBase, ...]:
        """分页读取项目内知识库。

        Args:
            project_id: 所属项目 ID。
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页知识库。

        """
        self._require_open()
        return self._lifecycle.list_knowledge_bases(
            project_id, limit=limit, offset=offset
        )

    def update_knowledge_base(
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: KnowledgeBaseStatus | None = None,
    ) -> KnowledgeBase:
        """更新知识库显示字段或状态。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。
            name: 可选新名称。
            description: 可选新说明。
            status: 可选生命周期状态。

        Returns:
            更新后的知识库。

        """
        self._require_open()
        return self._lifecycle.update_knowledge_base(
            project_id,
            knowledge_base_id,
            name=name,
            description=description,
            status=status,
        )

    def create_document(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        display_name: str,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> Job:
        """创建新逻辑文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            display_name: 文档显示名。
            content: DOCX 字节。
            media_type: DOCX 媒体类型。
            idempotency_key: 写请求幂等键。

        Returns:
            持久化构建 Job。

        """
        self._require_open()
        lifecycle = self._revision_lifecycle(knowledge_base_id)
        job = lifecycle.create_document(
            project_id,
            knowledge_base_id,
            display_name=display_name,
            content=content,
            media_type=media_type,
            idempotency_key=idempotency_key,
        )
        self._submit_job(job.job_id)
        return job

    def create_document_version(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        *,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> Job:
        """为既有文档创建版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 保持不变的逻辑文档 ID。
            content: DOCX 字节。
            media_type: DOCX 媒体类型。
            idempotency_key: 写请求幂等键。

        Returns:
            持久化构建 Job。

        """
        self._require_open()
        lifecycle = self._revision_lifecycle(knowledge_base_id)
        job = lifecycle.create_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            content=content,
            media_type=media_type,
            idempotency_key=idempotency_key,
        )
        self._submit_job(job.job_id)
        return job

    def rename_document(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        *,
        display_name: str,
    ) -> Document:
        """只修改文档显示名。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            display_name: 新显示名。

        Returns:
            更新后的文档。

        """
        self._require_open()
        return self._lifecycle.rename_document(
            project_id,
            knowledge_base_id,
            document_id,
            display_name=display_name,
        )

    def get_document(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """读取逻辑文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            文档视图。

        """
        self._require_open()
        return self._lifecycle.get_document(
            project_id, knowledge_base_id, document_id
        )

    def list_documents(
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Document, ...]:
        """分页读取知识库文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页文档。

        """
        self._require_open()
        return self._lifecycle.list_documents(
            project_id, knowledge_base_id, limit=limit, offset=offset
        )

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
        self._require_open()
        return self._lifecycle.list_document_versions(
            project_id, knowledge_base_id, document_id
        )

    def get_document_version(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
    ) -> DocumentVersion:
        """读取单个不可变版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            document_version_id: 目标版本 ID。

        Returns:
            版本视图。

        """
        self._require_open()
        return self._lifecycle.get_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            document_version_id,
        )

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
        self._require_open()
        return self._lifecycle.list_artifacts(
            project_id,
            knowledge_base_id,
            document_id,
            document_version_id,
        )

    def read_artifact(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        artifact_id: str,
    ) -> BlobReadResult:
        """授权后读取 Artifact。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 所属文档 ID。
            document_version_id: 所属版本 ID。
            artifact_id: 目标 Artifact ID。

        Returns:
            Artifact 字节与摘要。

        """
        self._require_open()
        return self._lifecycle.read_artifact(
            project_id,
            knowledge_base_id,
            document_id,
            document_version_id,
            artifact_id,
        )

    def delete_document(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """创建受控删除生命周期操作。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            deleting 状态文档。

        """
        self._require_open()
        return self._lifecycle.delete_document(
            project_id, knowledge_base_id, document_id
        )

    def get_job(self, job_id: str) -> Job:
        """读取 Job。

        Args:
            job_id: 目标 Job ID。

        Returns:
            持久化 Job。

        """
        self._require_open()
        return self._get_job(job_id)

    def cancel_job(self, job_id: str) -> Job:
        """请求取消非终态 Job。

        Args:
            job_id: 目标 Job ID。

        Returns:
            更新后的 Job。

        """
        self._require_open()
        return self._cancel_job(job_id)

    def retry_job(self, job_id: str) -> Job:
        """将可重试 Job 放回队列。

        Args:
            job_id: 目标 Job ID。

        Returns:
            更新后的 Job。

        """
        self._require_open()
        job = self._retry_job(job_id)
        self._submit_job(job.job_id)
        return job

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        states: tuple[str, ...] = (),
        page_size: int = 50,
        offset: int = 0,
    ) -> JobPage:
        """分页读取 P10 控制台安全 Job。

        Args:
            project_id: 可选项目过滤。
            knowledge_base_id: 可选知识库过滤。
            states: 可选公开状态过滤。
            page_size: 单页上限。
            offset: 稳定偏移量。

        Returns:
            Job 分页。

        """
        self._require_open()
        return self._console_service().list_jobs(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            states=states,
            page_size=page_size,
            offset=offset,
        )

    def inspect_revision(
        self, project_id: str, knowledge_base_id: str, revision_id: str
    ) -> RevisionInspection:
        """读取 scope 绑定的 Revision 检查视图。

        Args:
            project_id: 项目 ID。
            knowledge_base_id: 知识库 ID。
            revision_id: Revision ID。

        Returns:
            Revision 检查视图。

        """
        self._require_open()
        return self._console_service().inspect_revision(
            project_id, knowledge_base_id, revision_id
        )

    def list_revision_chunks(  # noqa: PLR0913
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
        """分页读取 canonical Chunk 三视图。

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
        self._require_open()
        return self._console_service().list_chunks(
            project_id,
            knowledge_base_id,
            revision_id,
            document_id=document_id,
            role=role,
            section_id=section_id,
            neighbor_group_id=neighbor_group_id,
            page_size=page_size,
            offset=offset,
        )

    def revision_document_reports(
        self, project_id: str, knowledge_base_id: str, revision_id: str
    ) -> tuple[RevisionDocumentReport, ...]:
        """读取 Revision 内文档质量报告。

        Args:
            project_id: 项目 ID。
            knowledge_base_id: 知识库 ID。
            revision_id: Revision ID。

        Returns:
            ParseReport 与 ChunkingReport 列表。

        """
        self._require_open()
        return self._console_service().document_reports(
            project_id, knowledge_base_id, revision_id
        )

    def search(
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        limit: int = 10,
    ) -> SearchAnswerResult:
        """执行 revision-sticky 检索并保存安全诊断。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。
            text: 查询文本。
            limit: 最大结果数。

        Returns:
            P08.5 实际路由与最小证据结果。

        """
        self._require_open()
        retrieval = self._retrieval
        if self._retrieval_resolver is not None:
            retrieval = self._retrieval_resolver(
                knowledge_base_id,
                self._retrieval,
            )
        result = retrieval.search_and_answer(
            SearchRequest(
                scope=KnowledgeBaseScope(
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                ),
                text=text,
                limit=limit,
            )
        )
        if result.diagnostics is not None:
            self._diagnostics[result.trace_id] = result.diagnostics
        return result

    def _revision_lifecycle(self, knowledge_base_id: str) -> LifecycleService:
        if self._revision_builder_resolver is None:
            return self._lifecycle
        return self._revision_builder_resolver(
            knowledge_base_id,
            self._lifecycle,
        )

    def answer(
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        limit: int = 10,
    ) -> SearchAnswerResult:
        """执行与 Search 共用的检索和受控回答链。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。
            text: 用户问题。
            limit: 最大候选数。

        Returns:
            含回答或明确拒答的结果。

        """
        return self.search(project_id, knowledge_base_id, text, limit=limit)

    def retrieval_diagnostics(self, trace_id: str) -> RetrievalDiagnostics:
        """读取进程内完整安全诊断。

        Args:
            trace_id: 查询返回的 Trace ID。

        Returns:
            不含正文、向量、Prompt 或 Secret 的诊断。

        """
        self._require_open()
        try:
            return self._diagnostics[trace_id]
        except KeyError as error:
            raise NotFound(
                "检索诊断不存在。", stage="retrieval.diagnostics"
            ) from error

    def trace_events(self, trace_id: str) -> tuple[TraceEvent, ...]:
        """读取不含正文和 Secret 的结构化 Trace 事件。

        Args:
            trace_id: 查询或作业派生的安全 Trace ID。

        Returns:
            按记录顺序排列的脱敏事件。

        """
        self._require_open()
        return self._trace_events(trace_id)

    def health(self) -> SystemStatus:
        """读取不产生 Provider 调用的系统状态。

        Args:
            无参数；读取当前组合根。

        Returns:
            系统状态。

        """
        self._require_open()
        return self._system_status()

    def close(self) -> None:
        """幂等关闭 SDK 持有的运行时。

        Args:
            无参数；关闭当前 facade。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._diagnostics.clear()
        self._close()

    def _require_open(self) -> None:
        if self._closed:
            raise CapabilityUnavailable("SDK 已关闭。", stage="sdk.lifecycle")

    def _console_service(self) -> ConsoleInspectionService:
        if self._console is None:
            raise CapabilityUnavailable(
                "当前组合根未启用控制台检查能力。", stage="console.inspect"
            )
        return self._console


__all__ = ["RagSdk"]
