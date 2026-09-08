"""P06 不可变 Revision 的 Build、Validate 与 Activate 编排。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from types import TracebackType
from typing import Protocol

from pydantic import Field, ValidationError

from rag_app.application.artifact_lifecycle import ArtifactLifecycleService
from rag_app.application.embedding_indexing import DocumentEmbeddingService
from rag_app.application.revision_validator import RevisionValidator
from rag_app.core.errors import JobCancelled, RagError, RevisionStateError
from rag_app.core.events import TraceEvent
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
    ParseResult,
    ParseSource,
    RevisionValidationEvidence,
    RevisionVectorSpec,
    VectorPointPayload,
    validate_document_ir,
    vector_point_id,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import (
    ChunkerPort,
    EmbeddingPort,
    ParserPort,
    TracePort,
    VectorStorePort,
)

_MAX_VALIDATION_ERRORS = 8
_MAX_VALIDATION_PATH_SEGMENTS = 8
_MAX_CAUSE_TYPES = 4
_SAFE_CODE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,79}$")
_SAFE_PATH_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SAFE_PUBLIC_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_SAFE_FAILURE_IDS = {
    "node_id": re.compile(r"^node_[0-9a-f]{32}$"),
    "chunk_id": re.compile(r"^chunk_[0-9a-f]{32}$"),
}
_PYDANTIC_SAFE_REASONS = {
    "missing": "必填字段缺失。",
    "int_parsing": "字段必须是有效整数。",
    "int_type": "字段必须是整数。",
    "string_pattern_mismatch": "字段格式不符合模型合同。",
    "string_too_short": "字段长度低于模型下限。",
    "string_too_long": "字段长度超过模型上限。",
    "greater_than": "字段值未超过模型下限。",
    "greater_than_equal": "字段值低于模型下限。",
    "less_than": "字段值未低于模型上限。",
    "less_than_equal": "字段值超过模型上限。",
}


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


@dataclass(frozen=True, slots=True)
class _DocumentStage:
    """不含正文的单文档阶段身份。"""

    job_id: str
    revision_id: str
    document_id: str | None
    input_sha256: str | None
    attempt: int


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
        trace: TracePort | None = None,
        document_enricher: Callable[[ParseResult], ParseResult] | None = None,
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
            trace: 可选同步安全事件端口。
            document_enricher: 原生解析后、分块前的受控增补钩子。

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
        self._trace = trace
        self._document_enricher = document_enricher

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
        persistent_job_id: str | None = None,
        content_identity: str | None = None,
        content_identity_current: Callable[[], str | None] | None = None,
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
            persistent_job_id: 队列重新冻结快照时保留的公开 Job 身份。
            content_identity: 与向量契约独立的 OCR 内容修订身份。
            content_identity_current: 激活前复核内容配置未漂移的读取函数。

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
            self._index_fingerprint
            if content_identity is None
            else canonical_sha256(
                {"index": self._index_fingerprint, "content": content_identity}
            ),
        )
        job_id = persistent_job_id or deterministic_id(
            "job", knowledge_base_id, revision_id
        )
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
        self._record_event(
            job_id,
            revision_id,
            "started",
            {
                "attempt": attempt,
                "project_id": project_id,
                "knowledge_base_id": knowledge_base_id,
            },
        )
        try:
            self._control.assert_job_active(job_id)
            if self._control.is_ready_revision(revision_id):
                current_state = IndexRevisionState.READY
                resumed = self._resume_ready(spec, job_id, attempt)
                self._record_event(
                    job_id,
                    revision_id,
                    "completed",
                    {
                        "attempt": attempt,
                        "document_count": resumed.document_count,
                        "chunk_count": resumed.chunk_count,
                        "resumed": True,
                    },
                )
                return resumed
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
                attempt=attempt,
            )
            current_state = self._advance(
                revision_id,
                current_state,
                IndexRevisionState.CHUNKING,
                job_id,
                attempt,
            )
            with self._document_stage(
                _DocumentStage(job_id, revision_id, None, None, attempt),
                "chunk_persistence",
            ):
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
            trace_id = _ingestion_trace_id(job_id, revision_id, attempt)
            if (
                content_identity_current is not None
                and content_identity_current() != content_identity
            ):
                raise RevisionStateError(
                    "内容加工配置在构建期间改变，原索引继续可用。",
                    stage="revision.activate.content_identity",
                )
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
            self._record_event(
                job_id,
                revision_id,
                "completed",
                {
                    "attempt": attempt,
                    "document_count": len(documents),
                    "chunk_count": len(chunks),
                    "resumed": False,
                },
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
            trace_id=_ingestion_trace_id(
                job_id,
                evidence.revision_id,
                attempt,
            ),
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
        attempt: int,
    ) -> tuple[Chunk, ...]:
        all_chunks: list[Chunk] = []
        for item in documents:
            self._control.assert_job_active(job_id)
            self._control.upsert_document(item.document)
            context = _DocumentStage(
                job_id,
                revision_id,
                item.document.document_id,
                hashlib.sha256(item.content).hexdigest(),
                attempt,
            )
            with self._document_stage(context, "parsing"):
                result = self._parser.parse(
                    ParseSource(
                        media_type=item.media_type,
                        display_name=item.document.display_name,
                        content=item.content,
                        extension=item.extension,
                    ),
                    self._parsing_policy,
                    ParseContext(
                        document=item.document,
                        cancel_check=lambda: self._control.assert_job_active(
                            job_id
                        ),
                    ),
                )
            with self._document_stage(context, "ir_validation"):
                validate_document_ir(result.document_ir)
            if self._document_enricher is not None:
                with self._document_stage(context, "image_enrichment"):
                    result = self._document_enricher(result)
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
            with self._document_stage(context, "chunking"):
                chunked = self._chunker.chunk(
                    result.document_ir,
                    ChunkingContext(
                        chunker_fingerprint=self._chunker_fingerprint(),
                        index_revision_id=revision_id,
                    ),
                )
            with self._document_stage(context, "document_persistence"):
                self._control.add_revision_document(
                    revision_id,
                    result.document_ir,
                    result.report,
                    chunked.report,
                    parsing_policy_fingerprint=canonical_sha256(
                        self._parsing_policy.model_dump(mode="json")
                    ),
                    part_catalog_identity=canonical_sha256(
                        tuple(
                            artifact.artifact_id
                            for artifact in result.artifacts
                        )
                    ),
                    chunk_count=len(chunked.chunks),
                )
            all_chunks.extend(chunked.chunks)
        return tuple(all_chunks)

    @contextmanager
    def _document_stage(
        self, context: _DocumentStage, stage: str
    ) -> Iterator[None]:
        self._control.update_job(
            context.job_id,
            state="running",
            stage=stage,
            attempt=context.attempt,
        )
        started = perf_counter()
        attributes: dict[str, object] = {
            "document_id": context.document_id,
            "input_sha256": context.input_sha256,
            "attempt": context.attempt,
            "parsing_policy": self._parsing_policy.model_dump(mode="json"),
            "chunking_policy": self._chunking_policy.model_dump(mode="json"),
            "parser": self._parser.descriptor.model_dump(mode="json"),
            "chunker": self._chunker.descriptor.model_dump(mode="json"),
            "tokenizer": _safe_tokenizer_contract(self._chunker),
            "embedding_slot_limits": _safe_embedding_slot_limits(
                self._slots,
                self._chunking_policy,
            ),
        }
        self._record_event(
            context.job_id, context.revision_id, f"{stage}.started", attributes
        )
        status = "success"
        error_code: str | None = None
        primary_error: Exception | None = None
        try:
            yield
        except Exception as error:
            status = (
                "cancelled" if isinstance(error, JobCancelled) else "failed"
            )
            if isinstance(error, RagError):
                error_code = error.code
                primary_error = error
                raise
            details = _safe_exception_details(error)
            details["document_id"] = context.document_id
            stage_label = {
                "chunk_persistence": "分块与检索索引保存",
                "document_persistence": "文档解析结果保存",
            }.get(stage, stage)
            error_code = f"{stage.upper()}_FAILED"
            primary_error = RagError(
                f"{stage_label}阶段处理失败（{_safe_type_name(error)}），"
                "请查看任务检索过程中的安全定位信息。",
                code=error_code,
                stage=stage,
                details=details,
            )
            raise primary_error from error
        finally:
            terminal_attributes: dict[str, object] = {
                "elapsed_ms": (perf_counter() - started) * 1000,
                "document_id": context.document_id,
                "status": status,
                "attempt": context.attempt,
            }
            if error_code is not None:
                terminal_attributes["error_code"] = error_code
            try:
                self._record_event(
                    context.job_id,
                    context.revision_id,
                    f"{stage}.finished",
                    terminal_attributes,
                )
            except Exception as terminal_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    f"记录阶段终态失败：{_safe_type_name(terminal_error)}。"
                )

    def _record_event(
        self,
        job_id: str,
        revision_id: str,
        name: str,
        attributes: dict[str, object],
    ) -> None:
        if self._trace is not None:
            safe_attributes = {
                **attributes,
                "job_id": job_id,
                "revision_id": revision_id,
            }
            self._trace.record(
                TraceEvent(
                    trace_id=_ingestion_trace_id(
                        job_id,
                        revision_id,
                        _positive_attempt(safe_attributes.get("attempt")),
                    ),
                    event_name="ingestion." + name,
                    occurred_at=datetime.now(UTC),
                    attributes=freeze_json_object(safe_attributes),
                )
            )

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
        self._record_event(
            job_id, revision_id, target.value, {"attempt": attempt}
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
            stage=error.stage if isinstance(error, RagError) else current.value,
            attempt=attempt,
            error_code=code,
            safe_message=safe_message,
            retryable=retryable,
        )
        self._record_event(
            job_id,
            revision_id,
            "cancelled" if isinstance(error, JobCancelled) else "failed",
            {
                "error_code": code,
                "safe_message": safe_message,
                "stage": error.stage
                if isinstance(error, RagError)
                else current.value,
                "attempt": attempt,
                "details": dict(error.details)
                if isinstance(error, RagError)
                else _safe_exception_location(error),
            },
        )


def _positive_attempt(value: object) -> int:
    """把事件中的 retry attempt 收窄为正整数。"""
    if type(value) is not int or value <= 0:
        raise ValueError("Ingestion Trace 事件必须包含正整数 attempt。")
    return value


def _ingestion_trace_id(
    job_id: str,
    revision_id: str,
    attempt: int,
) -> str:
    """为 retry 生成新根，同时保留首次尝试的既有公开 ID。"""
    if attempt == 1:
        return deterministic_id("trace", job_id, revision_id)
    return deterministic_id("trace", job_id, revision_id, attempt)


def _safe_exception_details(error: Exception) -> dict[str, object]:
    """提取不含输入、消息和宿主绝对路径的有界异常诊断。"""
    validation_error = _validation_error_in_chain(error)
    diagnostic_error = validation_error or error
    details = _safe_exception_location(diagnostic_error)
    if validation_error is not None and "application_frame" not in details:
        outer_location = _safe_exception_location(error)
        application_frame = outer_location.get("application_frame")
        if application_frame is not None:
            details["application_frame"] = application_frame
    details["error_type"] = (
        "pydantic_validation"
        if validation_error is not None
        else "unexpected_exception"
    )
    details["invariant_code"] = (
        "PYDANTIC_VALIDATION_FAILED"
        if validation_error is not None
        else "UNEXPECTED_EXCEPTION"
    )
    details["cause_types"] = _safe_cause_types(error)
    details.update(_safe_failure_identifiers(error))
    if validation_error is not None:
        details.update(_safe_validation_details(validation_error))
    return details


def _validation_error_in_chain(error: Exception) -> ValidationError | None:
    current: BaseException | None = error
    for _ in range(_MAX_CAUSE_TYPES):
        if current is None:
            return None
        if isinstance(current, ValidationError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _safe_exception_location(error: Exception) -> dict[str, object]:
    frames = _traceback_frames(error.__traceback__)
    exception_type = _safe_type_name(error)
    if not frames:
        return {"exception_type": exception_type}
    origin = frames[-1]
    origin_frame = _safe_frame(origin, application=False)
    application = next(
        (
            frame
            for frame in reversed(frames)
            if _application_file(frame.tb_frame.f_code.co_filename) is not None
        ),
        None,
    )
    details: dict[str, object] = {
        "exception_type": exception_type,
        "source_file": origin_frame["file"],
        "source_line": origin_frame["line"],
        "origin_frame": origin_frame,
    }
    if application is not None:
        details["application_frame"] = _safe_frame(
            application, application=True
        )
    return details


def _traceback_frames(
    traceback: TracebackType | None,
) -> tuple[TracebackType, ...]:
    frames: list[TracebackType] = []
    while traceback is not None:
        frames.append(traceback)
        traceback = traceback.tb_next
    return tuple(frames)


def _safe_frame(
    traceback: TracebackType,
    *,
    application: bool,
) -> dict[str, object]:
    filename = traceback.tb_frame.f_code.co_filename
    resolved_file = (
        _application_file(filename)
        if application
        else _safe_file_basename(filename)
    )
    return {
        "file": resolved_file or "unknown.py",
        "line": traceback.tb_lineno,
        "function": _safe_code_value(
            traceback.tb_frame.f_code.co_name,
            fallback="unknown",
        ),
    }


def _application_file(filename: str) -> str | None:
    normalized = filename.replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part)
    for index in range(len(parts) - 1):
        if parts[index : index + 2] == ("src", "rag_app"):
            return "/".join(parts[index:])
    if "rag_app" in parts:
        index = parts.index("rag_app")
        return "/".join(parts[index:])
    return None


def _safe_file_basename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return _safe_code_value(basename, fallback="unknown.py")


def _safe_type_name(error: Exception) -> str:
    return _safe_code_value(type(error).__name__, fallback="Exception")


def _safe_code_value(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and _SAFE_CODE_NAME.fullmatch(value):
        return value
    return fallback


def _safe_public_identity(value: object) -> str:
    if isinstance(value, str) and _SAFE_PUBLIC_IDENTITY.fullmatch(value):
        return value
    return "redacted"


def _safe_cause_types(error: Exception) -> list[str]:
    values: list[str] = []
    current: BaseException | None = error
    while current is not None and len(values) < _MAX_CAUSE_TYPES:
        values.append(
            _safe_code_value(type(current).__name__, fallback="Exception")
        )
        current = current.__cause__ or current.__context__
    return values


def _safe_failure_identifiers(error: Exception) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    for name, pattern in _SAFE_FAILURE_IDS.items():
        value = getattr(error, name, None)
        if isinstance(value, str) and pattern.fullmatch(value):
            identifiers[name] = value
    return identifiers


def _safe_validation_details(error: ValidationError) -> dict[str, object]:
    errors = error.errors(
        include_input=False,
        include_context=False,
        include_url=False,
    )
    bounded = errors[:_MAX_VALIDATION_ERRORS]
    entries = [_safe_validation_entry(item) for item in bounded]
    paths = [str(entry["path"]) for entry in entries]
    return {
        "model_name": _safe_code_value(
            error.title,
            fallback="pydantic_model",
        ),
        "invalid_fields": paths,
        "validation_errors": entries,
        "validation_error_count": len(errors),
        "validation_errors_truncated": len(errors) > len(bounded),
    }


def _safe_validation_entry(item: Mapping[str, object]) -> dict[str, object]:
    error_type = item.get("type")
    safe_error_type = (
        error_type
        if isinstance(error_type, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_type)
        else "validation_error"
    )
    location = item.get("loc")
    path = _safe_validation_path(
        location if isinstance(location, (tuple, list)) else ()
    )
    invariant_code = (
        "PYDANTIC_MODEL_INVARIANT_FAILED"
        if path == "$"
        else "PYDANTIC_" + safe_error_type.upper()
    )
    safe_reason = _PYDANTIC_SAFE_REASONS.get(
        safe_error_type,
        ("模型级不变量未通过。" if path == "$" else "字段值不符合模型合同。"),
    )
    return {
        "path": path,
        "error_type": safe_error_type,
        "invariant_code": invariant_code,
        "safe_reason": safe_reason,
    }


def _safe_validation_path(location: Sequence[object]) -> str:
    if not location:
        return "$"
    path = "$"
    for item in location[:_MAX_VALIDATION_PATH_SEGMENTS]:
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            path += f"[{item}]"
        elif isinstance(item, str) and _SAFE_PATH_TOKEN.fullmatch(item):
            path += f".{item}"
        else:
            path += "[?]"
    if len(location) > _MAX_VALIDATION_PATH_SEGMENTS:
        path += "[...]"
    return path


def _safe_tokenizer_contract(chunker: ChunkerPort) -> dict[str, object]:
    token_counter = getattr(chunker, "token_counter", None)
    count = getattr(token_counter, "count", None)
    if not callable(count):
        return {"available": False}
    try:
        probe = count("")
    except Exception:
        return {"available": False}
    tokenizer_id = getattr(probe, "tokenizer_id", None)
    exact = getattr(probe, "exact", None)
    compatibility = getattr(probe, "model_compatibility", ())
    if not isinstance(exact, bool) or not isinstance(compatibility, tuple):
        return {"available": False}
    return {
        "available": True,
        "tokenizer_id": _safe_public_identity(tokenizer_id),
        "exact": exact,
        "model_compatibility": [
            _safe_public_identity(item) for item in compatibility[:8]
        ],
    }


def _safe_embedding_slot_limits(
    slots: Sequence[EmbeddingSlotIdentity],
    policy: ChunkingPolicy,
) -> list[dict[str, object]]:
    policy_limits = dict(policy.max_embedding_tokens_by_slot)
    return [
        {
            "slot_id": _safe_public_identity(slot.slot_id),
            "provider_id": _safe_public_identity(slot.provider_id),
            "model": _safe_public_identity(slot.model),
            "max_input_tokens": slot.max_input_tokens,
            "policy_max_tokens": policy_limits.get(slot.slot_id),
        }
        for slot in slots
    ]


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
