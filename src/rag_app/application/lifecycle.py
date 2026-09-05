"""P09 文档生命周期与 P06 Revision Builder 编排。"""

from __future__ import annotations

import hashlib

from rag_app.application.revision_builder import (
    IngestionDocument,
    RevisionBuilder,
)
from rag_app.core.errors import (
    InvalidDocument,
    NotFound,
    RagError,
    RevisionStateError,
)
from rag_app.core.identifiers import (
    canonical_sha256,
    deterministic_id,
    document_version_id,
    new_id,
)
from rag_app.core.models import DocumentEmbeddingBudget, DocumentRef
from rag_app.core.models.management import (
    ArtifactDescriptor,
    Document,
    DocumentStatus,
    DocumentVersion,
    Job,
    KnowledgeBase,
    KnowledgeBaseStatus,
    Project,
    ProjectStatus,
    QueuedIngestion,
    QueuedIngestionDocument,
)
from rag_app.core.ports import (
    ActiveDocumentStorePort,
    BlobReadResult,
    BlobStorePort,
    BlobWriteRequest,
    LifecycleStorePort,
)

_DOC_MEDIA_TYPES_BY_EXTENSION = {
    ".doc": frozenset({"application/msword", "application/octet-stream"}),
    ".docx": frozenset(
        {
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        }
    ),
}
_OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_DOCX_ZIP_MAGIC = b"PK\x03\x04"


class LifecycleService:
    """复用 P06 Builder 的同步生命周期 Application Service。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        store: LifecycleStorePort,
        control: ActiveDocumentStorePort,
        builder: RevisionBuilder,
        blob_store: BlobStorePort,
        profile_id: str,
        index_fingerprint: str,
        budgets: dict[str, DocumentEmbeddingBudget],
        egress_allowed_slots: frozenset[str] = frozenset(),
        retrieval_profile_revision_id: str | None = None,
    ) -> None:
        """保存全部显式依赖和离线预算。

        Args:
            store: P09 生命周期 Store。
            control: P06 Active Revision 控制面。
            builder: P06 Revision Builder。
            blob_store: 来源 Artifact Store。
            profile_id: 当前 resolved Profile ID。
            index_fingerprint: 目标 Revision 身份使用的索引指纹。
            budgets: 每个 embedding slot 的硬预算。
            egress_allowed_slots: 当前 Profile 明确授权的远程索引 slot。
            retrieval_profile_revision_id: 队列需要冻结的产品 Profile Revision。

        Returns:
            无返回值。

        """
        self._store = store
        self._control = control
        self._builder = builder
        self._blob_store = blob_store
        self._profile_id = profile_id
        self._index_fingerprint = index_fingerprint
        self._budgets = dict(budgets)
        self._egress_allowed_slots = egress_allowed_slots
        self._retrieval_profile_revision_id = retrieval_profile_revision_id

    def create_project(
        self, name: str, *, idempotency_key: str | None = None
    ) -> Project:
        """创建新的逻辑项目。

        Args:
            name: 项目显示名。
            idempotency_key: 可选持久化幂等键。

        Returns:
            新项目。

        """
        _require_text(name, "项目名称")
        project_id = new_id("prj")
        if idempotency_key is not None:
            _require_text(idempotency_key, "Idempotency-Key")
            project_id = self._store.claim_idempotency(
                scope_id="projects",
                operation="project.create",
                idempotency_key=idempotency_key,
                request_hash=canonical_sha256({"name": name.strip()}),
                result_id=deterministic_id("prj", idempotency_key),
            )
        return self._store.create_project(project_id, name.strip())

    def get_project(self, project_id: str) -> Project:
        """读取项目。

        Args:
            project_id: 目标项目 ID。

        Returns:
            项目视图。

        """
        return self._store.get_project(project_id)

    def list_projects(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[Project, ...]:
        """稳定分页读取项目。

        Args:
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页项目。

        """
        return self._store.list_projects(limit=limit, offset=offset)

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        status: ProjectStatus | None = None,
    ) -> Project:
        """更新项目允许修改的字段。

        Args:
            project_id: 目标项目 ID。
            name: 可选显示名。
            status: 可选状态。

        Returns:
            更新后的项目。

        """
        if name is not None:
            _require_text(name, "项目名称")
        return self._store.update_project(project_id, name=name, status=status)

    def create_knowledge_base(
        self,
        project_id: str,
        name: str,
        *,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> KnowledgeBase:
        """在项目内创建知识库。

        Args:
            project_id: 所属项目 ID。
            name: 知识库显示名。
            description: 可选说明。
            idempotency_key: 可选持久化幂等键。

        Returns:
            新知识库。

        """
        project = self._store.get_project(project_id)
        if project.status is not ProjectStatus.ACTIVE:
            raise RevisionStateError(
                "只有 active 项目可以创建知识库。",
                stage="knowledge_base.create",
            )
        _require_text(name, "知识库名称")
        knowledge_base_id = new_id("kb")
        if idempotency_key is not None:
            _require_text(idempotency_key, "Idempotency-Key")
            knowledge_base_id = self._store.claim_idempotency(
                scope_id=project_id,
                operation="knowledge_base.create",
                idempotency_key=idempotency_key,
                request_hash=canonical_sha256(
                    {"name": name.strip(), "description": description}
                ),
                result_id=deterministic_id("kb", project_id, idempotency_key),
            )
        return self._store.create_knowledge_base(
            knowledge_base_id,
            project_id,
            name.strip(),
            profile_id=self._profile_id,
            description=description,
        )

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
        return self._store.get_knowledge_base(project_id, knowledge_base_id)

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
        knowledge_base = self._store.get_knowledge_base(
            project_id, knowledge_base_id
        )
        if knowledge_base.status is KnowledgeBaseStatus.DELETING:
            return knowledge_base
        return self._store.mark_knowledge_base_deleting(
            project_id, knowledge_base_id
        )

    def list_knowledge_bases(
        self, project_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[KnowledgeBase, ...]:
        """稳定分页读取项目内知识库。

        Args:
            project_id: 所属项目 ID。
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页知识库。

        """
        return self._store.list_knowledge_bases(
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
        """更新知识库允许修改的字段。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。
            name: 可选名称。
            description: 可选说明。
            status: 可选状态。

        Returns:
            更新后的知识库。

        """
        if name is not None:
            _require_text(name, "知识库名称")
        current = self._store.get_knowledge_base(project_id, knowledge_base_id)
        if current.status is KnowledgeBaseStatus.DELETING:
            raise RevisionStateError(
                "deleting 知识库不能再更新。",
                stage="knowledge_base.update",
            )
        if status is KnowledgeBaseStatus.DELETING:
            raise RevisionStateError(
                "请使用受控知识库删除操作。",
                stage="knowledge_base.update",
            )
        return self._store.update_knowledge_base(
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
        """创建全新逻辑文档并构建完整 Revision。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            display_name: 仅作显示的文件名。
            content: 受大小上限保护的 DOC 或 DOCX 字节。
            media_type: 已允许的媒体类型。
            idempotency_key: 调用方写请求幂等键。

        Returns:
            已完成或恢复的持久化 Job。

        """
        knowledge_base = self._store.get_knowledge_base(
            project_id, knowledge_base_id
        )
        if knowledge_base.status is not KnowledgeBaseStatus.ACTIVE:
            raise RevisionStateError(
                "只有 active 知识库可以创建文档。",
                stage="document.create",
            )
        _validate_document_input(
            display_name, content, media_type, idempotency_key
        )
        request_hash = _document_request_hash(display_name, content, media_type)
        proposed_id = deterministic_id(
            "doc", project_id, knowledge_base_id, idempotency_key
        )
        document_id = self._store.claim_idempotency(
            scope_id=knowledge_base_id,
            operation="document.create",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            result_id=proposed_id,
        )
        return self._queue_document_version(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            display_name=display_name,
            content=content,
            media_type=media_type,
            idempotency_key=idempotency_key,
        )

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
        """为既有逻辑文档提交不可变新版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 保持不变的逻辑文档 ID。
            content: 新版本 DOC 或 DOCX 字节。
            media_type: 已允许的媒体类型。
            idempotency_key: 调用方写请求幂等键。

        Returns:
            已完成或恢复的持久化 Job。

        """
        current = self._store.get_document(
            project_id, knowledge_base_id, document_id
        )
        if current.status is not DocumentStatus.ACTIVE:
            raise RevisionStateError(
                "只有 active 文档可以创建新版本。",
                stage="document_version.create",
            )
        _validate_document_input(
            current.display_name,
            content,
            media_type,
            idempotency_key,
            require_display_extension=False,
        )
        digest = hashlib.sha256(content).hexdigest()
        version_id = document_version_id(document_id, digest)
        result_id = self._store.claim_idempotency(
            scope_id=document_id,
            operation="document.version.create",
            idempotency_key=idempotency_key,
            request_hash=_document_request_hash(
                current.display_name, content, media_type
            ),
            result_id=version_id,
        )
        if result_id != version_id:
            raise AssertionError("幂等结果必须绑定确定性版本 ID。")
        return self._queue_document_version(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            display_name=current.display_name,
            content=content,
            media_type=media_type,
            idempotency_key=idempotency_key,
        )

    def get_document(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """按 scope 读取逻辑文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            文档视图。

        """
        return self._store.get_document(
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
        """稳定分页读取知识库文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            limit: 页大小。
            offset: 页偏移。

        Returns:
            当前页文档。

        """
        return self._store.list_documents(
            project_id, knowledge_base_id, limit=limit, offset=offset
        )

    def rename_document(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        *,
        display_name: str,
    ) -> Document:
        """只修改显示名，不触发版本或索引构建。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            display_name: 新显示名。

        Returns:
            更新后的文档。

        """
        _require_text(display_name, "文档显示名")
        current = self._store.get_document(
            project_id, knowledge_base_id, document_id
        )
        if current.status is not DocumentStatus.ACTIVE:
            raise RevisionStateError(
                "只有 active 文档可以重命名。", stage="document.rename"
            )
        return self._store.rename_document(
            project_id, knowledge_base_id, document_id, display_name
        )

    def delete_document(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """只生成 deleting 生命周期状态，不物理删除。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            状态为 deleting 的文档。

        """
        current = self._store.get_document(
            project_id, knowledge_base_id, document_id
        )
        if current.status is DocumentStatus.DELETING:
            return current
        if current.status is DocumentStatus.DELETED:
            raise RevisionStateError(
                "deleted 文档不能重复删除。", stage="document.delete"
            )
        return self._store.mark_document_deleting(
            project_id, knowledge_base_id, document_id
        )

    def list_document_versions(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> tuple[DocumentVersion, ...]:
        """读取文档全部版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            不可变版本序列。

        """
        return self._store.list_document_versions(
            project_id, knowledge_base_id, document_id
        )

    def get_document_version(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
    ) -> DocumentVersion:
        """读取指定不可变版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            document_version_id: 目标版本 ID。

        Returns:
            不可变版本视图。

        """
        return self._store.get_document_version(
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
        """按完整逻辑引用 scope 列出 Artifact。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 所属文档 ID。
            document_version_id: 所属版本 ID。

        Returns:
            授权范围内的 Artifact 摘要。

        """
        return self._store.list_artifacts(
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
        """授权后读取 Artifact 字节。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 所属文档 ID。
            document_version_id: 所属版本 ID。
            artifact_id: 目标 Artifact ID。

        Returns:
            带摘要与媒体类型的 Artifact。

        """
        self._store.authorize_artifact(
            project_id,
            knowledge_base_id,
            document_id,
            document_version_id,
            artifact_id,
        )
        result = self._blob_store.read(artifact_id)
        if result is None:
            raise NotFound("Artifact 物理对象不存在。", stage="artifact.read")
        return result

    def _queue_document_version(  # noqa: PLR0913
        self,
        *,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        display_name: str,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> Job:
        document = DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            display_name=display_name,
        )
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"sha256:{digest}"
        self._blob_store.put_if_absent(
            BlobWriteRequest(
                blob_id=artifact_id,
                content_sha256=digest,
                media_type=media_type,
                content=content,
            )
        )
        documents = [
            _queued_active_document(
                self._blob_store,
                item,
                item_artifact_id,
                item_media_type,
            )
            for item, item_artifact_id, item_media_type in (
                self._control.active_documents(knowledge_base_id)
            )
            if item.document_id != document_id
        ]
        documents.append(
            QueuedIngestionDocument(
                document=document,
                artifact_id=artifact_id,
                content_sha256=digest,
                size_bytes=len(content),
                media_type=media_type,
                extension=_detect_document_extension(content, media_type),
            )
        )
        version_ids = tuple(
            document_version_id(item.document.document_id, item.content_sha256)
            for item in documents
        )
        revision_id = deterministic_id(
            "irev",
            knowledge_base_id,
            tuple(sorted(version_ids)),
            self._index_fingerprint,
        )
        request = QueuedIngestion(
            job_id=deterministic_id("job", knowledge_base_id, revision_id),
            revision_id=revision_id,
            target_document_id=document_id,
            target_document_version_id=document_version_id(document_id, digest),
            retrieval_profile_revision_id=self._retrieval_profile_revision_id,
            documents=tuple(
                sorted(documents, key=lambda item: item.document.document_id)
            ),
        )
        return self._store.enqueue_ingestion(
            request, idempotency_key=idempotency_key
        )

    def queue_profile_rebuild(
        self,
        knowledge_base_id: str,
        expected_profile_revision_id: str | None,
        expected_index_revision_id: str | None,
        activation_validation_ids: tuple[str, ...],
    ) -> Job:
        """冻结现有文档快照并提交候选 Profile 的持久构建。

        Args:
            knowledge_base_id: 候选方案所属知识库。
            expected_profile_revision_id: 确认时的 Active Profile。
            expected_index_revision_id: 确认时的 Active Revision。
            activation_validation_ids: 准入时通过的独立 Provider 验证记录。

        Returns:
            已持久化但尚未领取的作业。

        """
        documents = tuple(
            _queued_active_document(self._blob_store, item, artifact, media)
            for item, artifact, media in self._control.active_documents(
                knowledge_base_id
            )
        )
        if not documents:
            raise RevisionStateError(
                "没有可构建的文档快照。", stage="profile.queue"
            )
        versions = tuple(
            sorted(
                document_version_id(
                    item.document.document_id, item.content_sha256
                )
                for item in documents
            )
        )
        revision_id = deterministic_id(
            "irev", knowledge_base_id, versions, self._index_fingerprint
        )
        first = documents[0]
        request = QueuedIngestion(
            job_id=deterministic_id("job", knowledge_base_id, revision_id),
            revision_id=revision_id,
            target_document_id=first.document.document_id,
            target_document_version_id=document_version_id(
                first.document.document_id, first.content_sha256
            ),
            retrieval_profile_revision_id=self._retrieval_profile_revision_id,
            documents=documents,
            activate_profile=True,
            expected_profile_revision_id=expected_profile_revision_id,
            expected_index_revision_id=expected_index_revision_id,
            activation_validation_ids=activation_validation_ids,
        )
        return self._store.enqueue_ingestion(
            request, idempotency_key=request.job_id
        )

    def run_ingestion(self, job_id: str) -> None:
        """领取并执行一个持久 Revision 构建请求。

        Args:
            job_id: 目标持久 Job ID。

        Returns:
            无返回值；终态写入 Store。

        """
        request = self._store.claim_ingestion(job_id)
        if request is None:
            return
        try:
            if (
                request.retrieval_profile_revision_id
                != self._retrieval_profile_revision_id
            ):
                raise RevisionStateError(
                    "持久作业绑定的 Retrieval Profile 已漂移。",
                    stage="document.worker",
                )
            documents = tuple(
                _ingestion_document(self._blob_store, item)
                for item in request.documents
            )
            first = documents[0].document
            result = self._builder.build_and_activate(
                project_id=first.project_id,
                knowledge_base_id=first.knowledge_base_id,
                documents=documents,
                idempotency_key=job_id,
                budgets=self._budgets,
                egress_allowed_slots=self._egress_allowed_slots,
                attempt=max(1, self._store.get_job(job_id).attempt),
            )
            if result.revision_id != request.revision_id:
                raise AssertionError("持久请求与 Builder Revision 身份不一致。")
            self._store.bind_job_document(
                result.job_id,
                request.target_document_id,
                request.target_document_version_id,
            )
            self._store.mark_version_ready(
                request.target_document_id,
                request.target_document_version_id,
            )
        except Exception as error:
            retryable = isinstance(error, RagError) and error.retryable
            self._store.finish_ingestion(
                job_id,
                succeeded=False,
                error_code=(
                    error.code
                    if isinstance(error, RagError)
                    else type(error).__name__
                ),
                safe_message=(
                    error.safe_message
                    if isinstance(error, RagError)
                    else "文档构建失败。"
                ),
                retryable=retryable,
            )
            return
        self._store.finish_ingestion(job_id, succeeded=True)


def _validate_document_input(
    display_name: str,
    content: bytes,
    media_type: str,
    idempotency_key: str,
    *,
    require_display_extension: bool = True,
) -> None:
    _require_text(display_name, "文档显示名")
    _require_text(idempotency_key, "Idempotency-Key")
    extension = _detect_document_extension(content, media_type)
    if require_display_extension and not display_name.casefold().endswith(
        extension
    ):
        raise InvalidDocument(
            "显示名扩展名必须与 DOC 或 DOCX 文件签名一致。",
            stage="document.upload",
        )


def _detect_document_extension(content: bytes, media_type: str) -> str:
    if not content:
        raise InvalidDocument("上传内容不能为空。", stage="document.upload")
    if content.startswith(_OLE_COMPOUND_FILE_MAGIC):
        extension = ".doc"
    elif content.startswith(_DOCX_ZIP_MAGIC):
        extension = ".docx"
    else:
        raise InvalidDocument(
            "上传内容不是有效的 DOC 或 DOCX 文件签名。",
            stage="document.upload",
        )
    if media_type.casefold() not in _DOC_MEDIA_TYPES_BY_EXTENSION[extension]:
        raise InvalidDocument(
            "上传 Content-Type 与文件格式不匹配。",
            stage="document.upload",
        )
    return extension


def _queued_active_document(
    blob_store: BlobStorePort,
    document: DocumentRef,
    artifact_id: str,
    media_type: str,
) -> QueuedIngestionDocument:
    blob = blob_store.read(artifact_id)
    expected_hash = artifact_id.removeprefix("sha256:")
    if blob is None or blob.content_sha256 != expected_hash:
        raise InvalidDocument(
            "Active DocumentVersion 的来源 Artifact 不存在或摘要漂移。",
            stage="document.snapshot",
        )
    return QueuedIngestionDocument(
        document=document,
        artifact_id=artifact_id,
        content_sha256=expected_hash,
        size_bytes=len(blob.content),
        media_type=media_type,
        extension=_detect_document_extension(blob.content, media_type),
    )


def _ingestion_document(
    blob_store: BlobStorePort, request: QueuedIngestionDocument
) -> IngestionDocument:
    blob = blob_store.read(request.artifact_id)
    if blob is None or blob.content_sha256 != request.content_sha256:
        raise InvalidDocument(
            "持久构建请求的来源 Artifact 不存在或摘要漂移。",
            stage="document.worker",
        )
    return IngestionDocument(
        document=request.document,
        content=blob.content,
        media_type=request.media_type,
        extension=request.extension,
    )


def _document_request_hash(
    display_name: str, content: bytes, media_type: str
) -> str:
    return canonical_sha256(
        {
            "display_name": display_name,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "media_type": media_type,
        }
    )


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label}不能为空。")


__all__ = ["LifecycleService"]
