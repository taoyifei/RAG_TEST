"""P06 不可变 Revision 的 Build、Validate 与 Activate 编排。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import Field

from rag_app.application.artifact_lifecycle import ArtifactLifecycleService
from rag_app.application.embedding_indexing import DocumentEmbeddingService
from rag_app.application.revision_validator import RevisionValidator
from rag_app.core.errors import RagError
from rag_app.core.identifiers import (
    canonical_sha256,
    deterministic_id,
    document_version_id,
)
from rag_app.core.models import (
    CacheScope,
    Chunk,
    ChunkEmbeddingState,
    ChunkingContext,
    ChunkingPolicy,
    ChunkingReport,
    DocumentEmbeddingBudget,
    DocumentIR,
    DocumentRef,
    EmbeddingSlotIdentity,
    FrozenModel,
    IndexRevisionRef,
    IndexRevisionState,
    NamedVectorPoint,
    ParseContext,
    ParseReport,
    ParseSource,
    RevisionValidationEvidence,
    RevisionVectorSpec,
    VectorPointPayload,
    validate_document_ir,
    vector_point_id,
)
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import (
    ChunkerPort,
    EmbeddingPort,
    ParserPort,
    VectorStorePort,
)


class IngestionDocument(FrozenModel):
    """受控本地输入与稳定逻辑文档身份。"""

    document: DocumentRef
    content: bytes = Field(repr=False)
    media_type: str = Field(min_length=1)
    extension: str = Field(default=".docx", pattern=r"^\.[a-z0-9]{1,16}$")


class RevisionBuildResult(FrozenModel):
    """一次成功构建和激活的安全结果。"""

    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    evidence: RevisionValidationEvidence


class _RevisionBuildControl(Protocol):
    """Builder 所需的持久化控制面。"""

    def is_ready_revision(self, revision_id: str) -> bool:
        """检查已有 READY 状态。

        Args:
            revision_id: 待重试的索引。

        Returns:
            是否仅需重新验证并发布。

        """
        ...

    def upsert_document(self, document: DocumentRef) -> None:
        """保存逻辑文档。

        Args:
            document: 带全局 scope 的文档引用。

        Returns:
            无返回值。

        """
        ...

    def put_document_version(  # noqa: PLR0913, PLR0917
        self,
        document_id: str,
        version_id: str,
        content_sha256: str,
        source_artifact_id: str,
        size_bytes: int,
        media_type: str,
    ) -> None:
        """保存不可变文档版本。

        Args:
            document_id: 逻辑文档 ID。
            version_id: 文档版本 ID。
            content_sha256: 来源字节摘要。
            source_artifact_id: 来源 Artifact ID。
            size_bytes: 来源字节数。
            media_type: 来源媒体类型。

        Returns:
            无返回值。

        """
        ...

    def create_job(
        self,
        job_id: str,
        project_id: str,
        knowledge_base_id: str,
        revision_id: str,
        idempotency_key: str,
    ) -> None:
        """创建幂等构建 Job。

        Args:
            job_id: 稳定 Job ID。
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            revision_id: 目标 Revision ID。
            idempotency_key: 用户幂等键。

        Returns:
            无返回值。

        """
        ...

    def update_job(  # noqa: PLR0913
        self,
        job_id: str,
        *,
        state: str,
        stage: str,
        attempt: int,
        error_code: str | None = None,
        safe_message: str | None = None,
        retryable: bool = False,
    ) -> None:
        """更新 Job 状态和安全错误。

        Args:
            job_id: 目标 Job ID。
            state: 持久化状态。
            stage: 当前阶段。
            attempt: 尝试序号。
            error_code: 可选错误码。
            safe_message: 可选安全消息。
            retryable: 是否允许重试。

        Returns:
            无返回值。

        """
        ...

    def acquire_revision_lease(
        self,
        revision_id: str,
        owner_job_id: str,
        *,
        lease_seconds: int = 300,
    ) -> int:
        """获取同一 Revision 的数据库单 Writer Lease。

        Args:
            revision_id: 确定性 Revision ID。
            owner_job_id: 当前 Job ID。
            lease_seconds: 有界 Lease 生命周期。

        Returns:
            当前 fencing token。

        """
        ...

    def assert_revision_writer(self, revision_id: str) -> None:
        """验证当前 Builder 的 fencing token 仍有效。

        Args:
            revision_id: 即将写入的 Revision ID。

        Returns:
            无返回值。

        """
        ...

    def release_revision_lease(self, revision_id: str) -> bool:
        """释放当前 Writer Lease。

        Args:
            revision_id: 已结束构建的 Revision ID。

        Returns:
            当前所有者成功释放时为 True。

        """
        ...

    def assert_job_active(self, job_id: str) -> None:
        """在阶段边界检查持久取消请求。

        Args:
            job_id: 当前构建 Job ID。

        Returns:
            无返回值。

        """
        ...

    def create_revision(
        self,
        revision: IndexRevisionRef,
        *,
        physical_namespace: str,
        expected_document_count: int,
        slots: Sequence[EmbeddingSlotIdentity],
        resolved_contracts: dict[str, object],
    ) -> None:
        """创建或恢复不可变 Revision。

        Args:
            revision: Revision 身份。
            physical_namespace: 独占向量命名空间。
            expected_document_count: 快照文档数。
            slots: required slot 序列。
            resolved_contracts: 不含 Secret 的实际合同。

        Returns:
            无返回值。

        """
        ...

    def set_revision_state(
        self,
        revision_id: str,
        expected: IndexRevisionState,
        target: IndexRevisionState,
    ) -> None:
        """比较并推进 Revision 状态。

        Args:
            revision_id: 目标 Revision ID。
            expected: 预期当前状态。
            target: 下一状态。

        Returns:
            无返回值。

        """
        ...

    def add_revision_document(  # noqa: PLR0913
        self,
        revision_id: str,
        document_ir: DocumentIR,
        parse_report: ParseReport,
        chunking_report: ChunkingReport,
        *,
        parsing_policy_fingerprint: str,
        part_catalog_identity: str,
        chunk_count: int,
    ) -> None:
        """保存 Revision 与解析结果绑定。

        Args:
            revision_id: 目标 Revision ID。
            document_ir: canonical Document IR。
            parse_report: 解析报告。
            chunking_report: 分块报告。
            parsing_policy_fingerprint: 解析策略指纹。
            part_catalog_identity: OOXML part catalog 身份。
            chunk_count: 文档 Chunk 数。

        Returns:
            无返回值。

        """
        ...

    def write_chunks(self, revision_id: str, chunks: Sequence[Chunk]) -> None:
        """原子写 Chunk、FTS 和 Exact。

        Args:
            revision_id: 目标 Revision ID。
            chunks: canonical Chunk 序列。

        Returns:
            无返回值。

        """
        ...

    def set_embedding_state(  # noqa: PLR0913
        self,
        revision_id: str,
        chunk_id: str,
        slot_id: str,
        state: ChunkEmbeddingState,
        *,
        cache_key: str | None,
        attempt: int,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        """保存单 Chunk/Slot 进度。

        Args:
            revision_id: 目标 Revision ID。
            chunk_id: 目标 Chunk ID。
            slot_id: 目标 slot ID。
            state: 最新状态。
            cache_key: 可选 Cache key。
            attempt: 尝试序号。
            error_code: 可选错误码。
            retryable: 是否允许重试。

        Returns:
            无返回值。

        """
        ...

    def update_embedding_coverage(
        self,
        revision_id: str,
        slot_id: str,
        *,
        valid_vector_count: int,
    ) -> None:
        """保存实际向量 coverage。

        Args:
            revision_id: 目标 Revision ID。
            slot_id: 目标 slot ID。
            valid_vector_count: Store 回读有效数。

        Returns:
            无返回值。

        """
        ...

    def record_validation(self, evidence: RevisionValidationEvidence) -> None:
        """持久化激活证据并标记 READY。

        Args:
            evidence: 实际 Store 验证证据。

        Returns:
            无返回值。

        """
        ...

    def activate(
        self,
        knowledge_base_id: str,
        evidence: RevisionValidationEvidence,
        *,
        reason: str,
        trace_id: str,
    ) -> None:
        """原子切换 Active Revision。

        Args:
            knowledge_base_id: 目标知识库 ID。
            evidence: 激活证据。
            reason: 安全原因码。
            trace_id: 稳定 Trace ID。

        Returns:
            无返回值。

        """
        ...

    def completed_build(
        self, job_id: str, revision_id: str
    ) -> tuple[int, int, RevisionValidationEvidence] | None:
        """读取已完成幂等结果。

        Args:
            job_id: 稳定 Job ID。
            revision_id: 预期 Revision ID。

        Returns:
            文档数、Chunk 数和证据；未完成为 None。

        """
        ...


class RevisionBuilder:
    """按固定阶段串行构建，不在失败时改变旧 active pointer。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        control: _RevisionBuildControl,
        parser: ParserPort,
        parsing_policy: ParsingPolicy,
        chunker: ChunkerPort,
        chunking_policy: ChunkingPolicy,
        artifact_lifecycle: ArtifactLifecycleService,
        embedding_service: DocumentEmbeddingService,
        embedding_providers: Mapping[str, EmbeddingPort],
        vector_store: VectorStorePort,
        validator: RevisionValidator,
        slots: Sequence[EmbeddingSlotIdentity],
        index_fingerprint: str,
        resolved_contracts: Mapping[str, object],
    ) -> None:
        """保存全部显式 resolved 依赖，不重建默认策略。

        Args:
            control: SQLite P06 控制面。
            parser: 无持久化副作用的 Parser。
            parsing_policy: composition 实际解析策略。
            chunker: canonical Chunker。
            chunking_policy: composition 实际分块策略。
            artifact_lifecycle: Blob/catalog 协调器。
            embedding_service: 只补 missing 的持久化服务。
            embedding_providers: slot 到文档 Provider 的映射。
            vector_store: 不可变 revision Vector Store。
            validator: 实际 Store 激活门。
            slots: required slot 顺序。
            index_fingerprint: 当前 composition 指纹。
            resolved_contracts: 可持久化且不含 secret 的 schema 合同。

        Returns:
            无返回值。

        """
        self._control = control
        self._parser = parser
        self._parsing_policy = parsing_policy
        self._chunker = chunker
        self._chunking_policy = chunking_policy
        self._artifact_lifecycle = artifact_lifecycle
        self._embedding_service = embedding_service
        self._embedding_providers = dict(embedding_providers)
        self._vector_store = vector_store
        self._validator = validator
        self._slots = tuple(slots)
        self._index_fingerprint = index_fingerprint
        self._resolved_contracts = dict(resolved_contracts)

    def build_and_activate(  # noqa: PLR0913, PLR0915
        self,
        *,
        project_id: str,
        knowledge_base_id: str,
        documents: Sequence[IngestionDocument],
        idempotency_key: str,
        budgets: Mapping[str, DocumentEmbeddingBudget],
        egress_allowed_slots: frozenset[str] = frozenset(),
        attempt: int = 1,
    ) -> RevisionBuildResult:
        """执行固定 Build、Validate、Activate 流程。

        Args:
            project_id: 目标 project。
            knowledge_base_id: 目标知识库。
            documents: 本 revision 的完整文档快照。
            idempotency_key: KB 内重试身份。
            budgets: 每个 required slot 的文档索引预算。
            egress_allowed_slots: 显式远程出网授权。
            attempt: 当前用户发起的尝试序号。

        Returns:
            成功激活的新 revision 与实际证据。

        Raises:
            Exception: 任一步失败；旧 active pointer 保持不变。

        """
        if not documents:
            raise ValueError("IndexRevision snapshot 至少包含一个文档。")
        _validate_snapshot_scope(documents, project_id, knowledge_base_id)
        version_ids = tuple(
            document_version_id(
                item.document.document_id,
                hashlib.sha256(item.content).hexdigest(),
            )
            for item in documents
        )
        revision_id = deterministic_id(
            "irev",
            knowledge_base_id,
            tuple(sorted(version_ids)),
            self._index_fingerprint,
        )
        job_id = deterministic_id("job", knowledge_base_id, revision_id)
        revision = IndexRevisionRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            index_revision_id=revision_id,
            index_fingerprint=self._index_fingerprint,
            state=IndexRevisionState.CREATED,
        )
        spec = RevisionVectorSpec(
            revision=revision,
            physical_namespace=revision_id,
            slots=self._slots,
        )
        current_state = IndexRevisionState.CREATED
        self._control.create_job(
            job_id,
            project_id,
            knowledge_base_id,
            revision_id,
            idempotency_key,
        )
        completed = self._control.completed_build(job_id, revision_id)
        if completed is not None:
            document_count, chunk_count, evidence = completed
            return RevisionBuildResult(
                job_id=job_id,
                revision_id=revision_id,
                document_count=document_count,
                chunk_count=chunk_count,
                evidence=evidence,
            )
        self._control.acquire_revision_lease(revision_id, job_id)
        try:
            self._control.assert_job_active(job_id)
            if self._control.is_ready_revision(revision_id):
                current_state = IndexRevisionState.READY
                return self._resume_ready(spec, job_id, attempt)
            self._control.create_revision(
                revision,
                physical_namespace=spec.physical_namespace,
                expected_document_count=len(documents),
                slots=self._slots,
                resolved_contracts=self._resolved_contracts,
            )
            self._control.assert_revision_writer(revision_id)
            self._vector_store.create_revision(spec)
            current_state = self._advance(
                revision_id,
                current_state,
                IndexRevisionState.PARSING,
                job_id,
                attempt,
            )
            chunks = self._parse_and_chunk(
                documents,
                revision_id=revision_id,
                job_id=job_id,
            )
            current_state = self._advance(
                revision_id,
                current_state,
                IndexRevisionState.CHUNKING,
                job_id,
                attempt,
            )
            self._control.write_chunks(revision_id, chunks)
            current_state = self._advance(
                revision_id,
                current_state,
                IndexRevisionState.EMBEDDING_PRIMARY,
                job_id,
                attempt,
            )
            embedding_vectors: dict[str, tuple[tuple[float, ...], ...]] = {}
            remaining_budgets = dict(budgets)
            for slot_index, slot in enumerate(self._slots):
                self._control.assert_job_active(job_id)
                if slot_index > 0:
                    current_state = self._advance(
                        revision_id,
                        current_state,
                        IndexRevisionState.EMBEDDING_STANDBY,
                        job_id,
                        attempt,
                    )
                embedding = self._embedding_service.embed_missing(
                    job_id=job_id,
                    revision_id=revision_id,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                    chunks=chunks,
                    slots=(slot,),
                    budgets=remaining_budgets,
                    egress_allowed_slots=egress_allowed_slots,
                    attempt=attempt,
                    cache_scope=CacheScope.PROJECT,
                )
                embedding_vectors.update(embedding.vectors)
                remaining_budgets.update(embedding.budgets)
            current_state = self._advance(
                revision_id,
                current_state,
                IndexRevisionState.LEXICAL_INDEXING,
                job_id,
                attempt,
            )
            current_state = self._advance(
                revision_id,
                current_state,
                IndexRevisionState.VECTOR_INDEXING,
                job_id,
                attempt,
            )
            points = complete_vector_points(
                revision, chunks, self._slots, embedding_vectors
            )
            self._control.assert_revision_writer(revision_id)
            self._vector_store.upsert_complete_points(spec, points)
            for slot in self._slots:
                for chunk in chunks:
                    self._control.set_embedding_state(
                        revision_id,
                        chunk.chunk_id,
                        slot.slot_id,
                        ChunkEmbeddingState.VECTOR_WRITTEN,
                        cache_key=None,
                        attempt=attempt,
                    )
                self._control.update_embedding_coverage(
                    revision_id,
                    slot.slot_id,
                    valid_vector_count=self._vector_store.count_vectors(
                        spec,
                        slot.vector_name,
                    ),
                )
            current_state = self._advance(
                revision_id,
                current_state,
                IndexRevisionState.VALIDATING,
                job_id,
                attempt,
            )
            evidence = self._validator.validate(
                spec,
                current_index_fingerprint=self._index_fingerprint,
            )
            self._control.record_validation(evidence)
            current_state = IndexRevisionState.READY
            self._control.assert_job_active(job_id)
            self._control.update_job(
                job_id,
                state="running",
                stage="activate",
                attempt=attempt,
            )
            trace_id = deterministic_id("trace", job_id, revision_id)
            self._control.activate(
                knowledge_base_id,
                evidence,
                reason="P06_BUILD_VALIDATED",
                trace_id=trace_id,
            )
            self._control.update_job(
                job_id,
                state="completed",
                stage="activated",
                attempt=attempt,
            )
            return RevisionBuildResult(
                job_id=job_id,
                revision_id=revision_id,
                document_count=len(documents),
                chunk_count=len(chunks),
                evidence=evidence,
            )
        except Exception as error:
            self._record_failure(
                revision_id, current_state, job_id, attempt, error
            )
            raise
        finally:
            self._control.release_revision_lease(revision_id)

    def _resume_ready(
        self,
        spec: RevisionVectorSpec,
        job_id: str,
        attempt: int,
    ) -> RevisionBuildResult:
        self._control.update_job(
            job_id, state="running", stage="validating", attempt=attempt
        )
        evidence = self._validator.validate(
            spec,
            current_index_fingerprint=self._index_fingerprint,
        )
        self._control.assert_job_active(job_id)
        self._control.activate(
            spec.revision.knowledge_base_id,
            evidence,
            reason="P11_RESUME_VALIDATED",
            trace_id=deterministic_id("trace", job_id, evidence.revision_id),
        )
        self._control.update_job(
            job_id, state="completed", stage="activated", attempt=attempt
        )
        return RevisionBuildResult(
            job_id=job_id,
            revision_id=evidence.revision_id,
            document_count=evidence.document_count,
            chunk_count=evidence.chunk_count,
            evidence=evidence,
        )

    def _parse_and_chunk(
        self,
        documents: Sequence[IngestionDocument],
        *,
        revision_id: str,
        job_id: str,
    ) -> tuple[Chunk, ...]:
        all_chunks: list[Chunk] = []
        for item in documents:
            self._control.assert_job_active(job_id)
            self._control.upsert_document(item.document)
            result = self._parser.parse(
                ParseSource(
                    media_type=item.media_type,
                    display_name=item.document.display_name,
                    content=item.content,
                    extension=item.extension,
                ),
                self._parsing_policy,
                ParseContext(document=item.document),
            )
            validate_document_ir(result.document_ir)
            version = result.document_ir.version
            created, existing = self._artifact_lifecycle.persist(
                result.artifacts,
                owner_document_version_id=version.document_version_id,
                revision_id=revision_id,
                job_id=job_id,
            )
            del created, existing
            source_artifact_id = result.document_ir.source.blob_ref
            if source_artifact_id is None:
                raise ValueError("Parser source Artifact 引用缺失。")
            self._control.put_document_version(
                item.document.document_id,
                version.document_version_id,
                version.content_sha256,
                source_artifact_id,
                len(item.content),
                item.media_type,
            )
            chunked = self._chunker.chunk(
                result.document_ir,
                ChunkingContext(
                    chunker_fingerprint=self._chunker_fingerprint(),
                    index_revision_id=revision_id,
                ),
            )
            self._control.add_revision_document(
                revision_id,
                result.document_ir,
                result.report,
                chunked.report,
                parsing_policy_fingerprint=canonical_sha256(
                    self._parsing_policy.model_dump(mode="json")
                ),
                part_catalog_identity=canonical_sha256(
                    tuple(artifact.artifact_id for artifact in result.artifacts)
                ),
                chunk_count=len(chunked.chunks),
            )
            all_chunks.extend(chunked.chunks)
        return tuple(all_chunks)

    def _chunker_fingerprint(self) -> str:
        value = getattr(self._chunker, "fingerprint", None)
        if not isinstance(value, str) or not value.startswith("sha256:"):
            raise ValueError("Chunker 未公开有效 fingerprint。")
        return value

    def _advance(
        self,
        revision_id: str,
        current: IndexRevisionState,
        target: IndexRevisionState,
        job_id: str,
        attempt: int,
    ) -> IndexRevisionState:
        self._control.assert_job_active(job_id)
        self._control.set_revision_state(revision_id, current, target)
        self._control.update_job(
            job_id,
            state="running",
            stage=target.value,
            attempt=attempt,
        )
        return target

    def _record_failure(
        self,
        revision_id: str,
        current: IndexRevisionState,
        job_id: str,
        attempt: int,
        error: Exception,
    ) -> None:
        retryable = isinstance(error, RagError) and error.retryable
        target = (
            IndexRevisionState.FAILED_RETRYABLE
            if retryable
            else IndexRevisionState.FAILED_TERMINAL
        )
        if current not in {
            IndexRevisionState.READY,
            IndexRevisionState.ACTIVE,
            IndexRevisionState.RETIRED,
        }:
            try:
                self._control.set_revision_state(revision_id, current, target)
            except Exception as cleanup_error:
                error.add_note(
                    "记录 revision failure state 失败："
                    f"{type(cleanup_error).__name__}。"
                )
        code = (
            error.code if isinstance(error, RagError) else type(error).__name__
        )
        safe_message = (
            error.safe_message
            if isinstance(error, RagError)
            else "P06 构建失败。"
        )
        self._control.update_job(
            job_id,
            state="failed_retryable" if retryable else "failed_terminal",
            stage=current.value,
            attempt=attempt,
            error_code=code,
            safe_message=safe_message,
            retryable=retryable,
        )


def _validate_snapshot_scope(
    documents: Sequence[IngestionDocument],
    project_id: str,
    knowledge_base_id: str,
) -> None:
    ids = set()
    for item in documents:
        document = item.document
        if (
            document.project_id != project_id
            or document.knowledge_base_id != knowledge_base_id
        ):
            raise ValueError("Revision snapshot 文档 scope 不一致。")
        if document.document_id in ids:
            raise ValueError("Revision snapshot document ID 禁止重复。")
        ids.add(document.document_id)


def complete_vector_points(
    revision: IndexRevisionRef,
    chunks: Sequence[Chunk],
    slots: Sequence[EmbeddingSlotIdentity],
    vectors: Mapping[str, tuple[tuple[float, ...], ...]],
) -> tuple[NamedVectorPoint, ...]:
    """把每个 Chunk 的全部 required vectors 组装为完整 Point。

    Args:
        revision: 目标不可变 revision。
        chunks: canonical chunk 顺序。
        slots: required slot schema。
        vectors: slot ID 到与 chunks 同序的向量。

    Returns:
        每个 Point 一次携带全部 named vectors 的不可变序列。

    """
    points = []
    for index, chunk in enumerate(chunks):
        named = {
            slot.vector_name: vectors[slot.slot_id][index] for slot in slots
        }
        points.append(
            NamedVectorPoint(
                point_id=vector_point_id(
                    revision.index_revision_id, chunk.chunk_id
                ),
                payload=VectorPointPayload(
                    project_id=revision.project_id,
                    knowledge_base_id=revision.knowledge_base_id,
                    index_revision_id=revision.index_revision_id,
                    document_id=chunk.version.document_id,
                    document_version_id=chunk.version.document_version_id,
                    chunk_id=chunk.chunk_id,
                    role=chunk.role.value,
                    section_id=chunk.section_id,
                    neighbor_group_id=chunk.neighbor_group_id,
                    content_sha256=chunk.content_sha256,
                ),
                vectors=tuple(sorted(named.items())),
            )
        )
    return tuple(points)


__all__ = [
    "IngestionDocument",
    "RevisionBuildResult",
    "RevisionBuilder",
    "complete_vector_points",
]
