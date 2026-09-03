"""P06 SQLite 控制面、Artifact catalog 与原子激活 adapter。"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from sqlite3 import Row

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.adapters.stores.sqlite_fts5 import write_chunks_transaction
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    Conflict,
    IndexCorrupt,
    IndexNotReady,
    NotFound,
    RevisionStateError,
    ValidationFailed,
)
from rag_app.core.identifiers import (
    canonical_json,
    canonical_sha256,
    document_version_id,
)
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    BlobCatalogEntry,
    BlobReference,
    Chunk,
    ChunkEmbeddingState,
    ChunkingReport,
    DocumentIR,
    DocumentRef,
    EmbeddingCoverage,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
    HydratedChunk,
    IndexRevisionRef,
    IndexRevisionState,
    KnowledgeBaseScope,
    ParseReport,
    RetrievalPolicy,
    RevisionValidationEvidence,
    RevisionVectorSpec,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.ports import MetadataRecord

_TERMINAL_REVISION_STATES = {
    IndexRevisionState.ACTIVE,
    IndexRevisionState.RETIRED,
    IndexRevisionState.FAILED_TERMINAL,
}
_MAX_HYDRATION_CHUNKS = 200
_MAX_SECTION_CHUNKS = 20


def _slot_from_row(row: Row) -> EmbeddingSlotIdentity:
    return EmbeddingSlotIdentity(
        slot_id=str(row["slot_id"]),
        role=EmbeddingSlotRole(str(row["role"])),
        provider_id=str(row["provider_id"]),
        model=str(row["model"]),
        vector_name=str(row["vector_name"]),
        dimension=int(row["dimension"]),
        max_input_tokens=int(row["max_input_tokens"]),
        adapter_revision=str(row["adapter_revision"]),
        document_request_policy=json.loads(
            str(row["document_request_policy_json"])
        ),
        query_request_policy=json.loads(str(row["query_request_policy_json"])),
        normalization=str(row["normalization"]),
    )


class SqliteControlStore:
    """以数据库约束和显式事务执行 P06 身份与生命周期合同。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.METADATA_STORE,
        name="sqlite-control",
        version="5",
        mode=ProviderMode.LOCAL,
    )

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        """保存已迁移数据库连接工厂。

        Args:
            connections: P06 SQLite 连接工厂。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._closed = False

    def put_project(self, project_id: str, name: str) -> None:
        """幂等创建 project。

        Args:
            project_id: 全局 project ID。
            name: 显示名称。

        Returns:
            无返回值。

        """
        now = _now()
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT name FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if existing is not None and str(existing["name"]) != name:
                raise Conflict(
                    "project ID 已绑定不同名称。", stage="control.project"
                )
            connection.execute(
                "INSERT OR IGNORE INTO projects("
                "project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (project_id, name, now, now),
            )

    def create_job(
        self,
        job_id: str,
        project_id: str,
        knowledge_base_id: str,
        revision_id: str,
        idempotency_key: str,
    ) -> None:
        """幂等创建持久化 ingestion job。

        Args:
            job_id: 稳定 Job ID。
            project_id: 目标 project。
            knowledge_base_id: 目标知识库。
            revision_id: 本 Job 构建的 revision。
            idempotency_key: KB 内唯一幂等键。

        Returns:
            无返回值。

        """
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ingestion_jobs("
                "job_id, project_id, knowledge_base_id, revision_id, "
                "idempotency_key, state, stage, attempt, heartbeat_at, "
                "retryable, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 'created', 0, ?, 0, ?, ?)",
                (
                    job_id,
                    project_id,
                    knowledge_base_id,
                    revision_id,
                    idempotency_key,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT project_id, knowledge_base_id, revision_id, "
                "idempotency_key FROM ingestion_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None or tuple(row) != (
                project_id,
                knowledge_base_id,
                revision_id,
                idempotency_key,
            ):
                raise Conflict(
                    "Job ID 或幂等键已绑定不同构建。", stage="job.create"
                )

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
        """更新 Job 心跳、阶段和安全错误。

        Args:
            job_id: 目标 Job。
            state: schema 允许的稳定状态。
            stage: 不含正文的阶段名。
            attempt: 当前非负尝试序号。
            error_code: 可选机器错误码。
            safe_message: 可选安全消息。
            retryable: 是否允许用户显式 retry。

        Returns:
            无返回值。

        """
        now = _now()
        finished = (
            now
            if state in {"completed", "failed_retryable", "failed_terminal"}
            else None
        )
        with self._connections.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE ingestion_jobs SET state=?, stage=?, attempt=?, "
                "heartbeat_at=?, "
                "error_code=?, safe_message=?, retryable=?, updated_at=?, "
                "started_at=COALESCE(started_at, ?), finished_at=? "
                "WHERE job_id=?",
                (
                    state,
                    stage,
                    attempt,
                    now,
                    error_code,
                    safe_message,
                    int(retryable),
                    now,
                    now,
                    finished,
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFound("ingestion job 不存在。", stage="job.update")

    def recover_stale_jobs(self, stale_before: str) -> int:
        """把 stale RUNNING Job 标成可显式恢复的 INTERRUPTED。

        Args:
            stale_before: ISO-8601 心跳截止时间。

        Returns:
            被标记的 Job 数量。

        """
        with self._connections.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE ingestion_jobs SET state='interrupted', retryable=1, "
                "error_code='JOB_INTERRUPTED', "
                "safe_message='进程中断，需要显式重试。', "
                "updated_at=? WHERE state='running' AND heartbeat_at<?",
                (_now(), stale_before),
            )
        return int(cursor.rowcount)

    def record_provider_usage(  # noqa: PLR0913
        self,
        job_id: str,
        slot_id: str,
        provider_id: str,
        *,
        requests: int,
        estimated_tokens: int,
        chunks: int,
        elapsed_ms: int,
        status_category: str,
    ) -> None:
        """持久化 Job/Provider/Slot 累计用量。

        Args:
            job_id: 用量所属 Job。
            slot_id: 用量所属向量槽。
            provider_id: 实际 Provider 身份。
            requests: 累计请求数。
            estimated_tokens: 累计估算 Token 数。
            chunks: 累计 Chunk 数。
            elapsed_ms: 累计耗时毫秒数。
            status_category: 安全状态类别。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO job_provider_usage("
                "job_id, slot_id, provider_id, requests, estimated_tokens, "
                "observed_tokens, chunks, retries, elapsed_ms, "
                "status_category) VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?, ?) "
                "ON CONFLICT(job_id, slot_id, provider_id) DO UPDATE SET "
                "requests=excluded.requests, "
                "estimated_tokens=excluded.estimated_tokens, "
                "chunks=excluded.chunks, elapsed_ms=excluded.elapsed_ms, "
                "status_category=excluded.status_category",
                (
                    job_id,
                    slot_id,
                    provider_id,
                    requests,
                    estimated_tokens,
                    chunks,
                    elapsed_ms,
                    status_category,
                ),
            )

    def completed_build(
        self, job_id: str, revision_id: str
    ) -> tuple[int, int, RevisionValidationEvidence] | None:
        """读取同幂等键已经完成的安全构建结果。

        Args:
            job_id: 稳定 Job ID。
            revision_id: 预期 Revision ID。

        Returns:
            文档数、Chunk 数和验证证据；未完成时为 None。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT j.state, r.expected_document_count, "
                "r.expected_chunk_count, r.validation_evidence_json "
                "FROM ingestion_jobs j JOIN index_revisions r "
                "ON r.index_revision_id=j.revision_id "
                "WHERE j.job_id=? AND j.revision_id=?",
                (job_id, revision_id),
            ).fetchone()
        if row is None or str(row["state"]) != "completed":
            return None
        payload = row["validation_evidence_json"]
        if not isinstance(payload, str):
            raise ValidationFailed(
                "已完成 Job 缺少 validation evidence。",
                stage="job.idempotency",
            )
        return (
            int(row["expected_document_count"]),
            int(row["expected_chunk_count"]),
            RevisionValidationEvidence.model_validate_json(payload),
        )

    def job_summary(self, job_id: str) -> dict[str, object]:
        """返回不含正文、路径和 Secret 的 Job 摘要。

        Args:
            job_id: 待读取的 Job ID。

        Returns:
            安全状态字段映射。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT job_id, project_id, knowledge_base_id, revision_id, "
                "state, stage, attempt, error_code, safe_message, retryable "
                "FROM ingestion_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise NotFound("ingestion job 不存在。", stage="job.read")
        return dict(row)

    def put_knowledge_base(
        self,
        knowledge_base_id: str,
        project_id: str,
        name: str,
        *,
        profile_id: str,
        description: str = "",
    ) -> None:
        """幂等创建知识库并执行 project scope 约束。

        Args:
            knowledge_base_id: 全局知识库 ID。
            project_id: 所属 project。
            name: 显示名称。
            profile_id: resolved Profile ID。
            description: 可选非敏感说明。

        Returns:
            无返回值。

        """
        now = _now()
        normalized_name = name.strip().casefold()
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT project_id, name, profile_id FROM knowledge_bases "
                "WHERE knowledge_base_id=?",
                (knowledge_base_id,),
            ).fetchone()
            if existing is not None:
                observed = (
                    str(existing["project_id"]),
                    str(existing["name"]),
                    str(existing["profile_id"]),
                )
                if observed != (project_id, name, profile_id):
                    raise Conflict(
                        "knowledge base ID 已绑定不同 scope。",
                        stage="control.kb",
                    )
                return
            connection.execute(
                "INSERT INTO knowledge_bases("
                "knowledge_base_id, project_id, name, normalized_name, "
                "description, profile_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    knowledge_base_id,
                    project_id,
                    name,
                    normalized_name,
                    description,
                    profile_id,
                    now,
                    now,
                ),
            )

    def upsert_document(self, document: DocumentRef) -> None:
        """创建逻辑文档或只更新显示名。

        Args:
            document: 全局 ID、scope 与显示名。

        Returns:
            无返回值。

        Raises:
            Conflict: 同一 document ID 已绑定其他 project/KB。

        """
        now = _now()
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT project_id, knowledge_base_id FROM documents "
                "WHERE document_id=?",
                (document.document_id,),
            ).fetchone()
            if existing is not None:
                scope = (
                    str(existing["project_id"]),
                    str(existing["knowledge_base_id"]),
                )
                if scope != (document.project_id, document.knowledge_base_id):
                    raise Conflict(
                        "document ID 已绑定其他 project/knowledge base。",
                        stage="control.document",
                    )
                connection.execute(
                    "UPDATE documents SET display_name=?, updated_at=? "
                    "WHERE document_id=?",
                    (document.display_name, now, document.document_id),
                )
                return
            connection.execute(
                "INSERT INTO documents("
                "document_id, project_id, knowledge_base_id, display_name, "
                "status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (
                    document.document_id,
                    document.project_id,
                    document.knowledge_base_id,
                    document.display_name,
                    now,
                    now,
                ),
            )

    def put_document_version(  # noqa: PLR0913, PLR0917
        self,
        document_id: str,
        version_id: str,
        content_sha256: str,
        source_artifact_id: str,
        size_bytes: int,
        media_type: str,
    ) -> None:
        """幂等保存仅由 document ID 与 bytes 定义的版本。

        Args:
            document_id: 全局逻辑文档 ID。
            version_id: 必须匹配 Core 新公式的 dver。
            content_sha256: 来源字节摘要。
            source_artifact_id: 可共享物理 Artifact ID。
            size_bytes: 来源字节长度。
            media_type: 来源媒体类型。

        Returns:
            无返回值。

        Raises:
            ValidationFailed: dver 不是 P05.5 新公式。
            Conflict: 已存在版本内容不一致。

        """
        if version_id != document_version_id(document_id, content_sha256):
            raise ValidationFailed(
                "document version ID 不符合 P05.5 公式。", stage="control.dver"
            )
        values = (
            document_id,
            content_sha256,
            source_artifact_id,
            size_bytes,
            media_type,
        )
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT document_id, content_sha256, source_artifact_id, "
                "size_bytes, media_type FROM document_versions "
                "WHERE document_version_id=?",
                (version_id,),
            ).fetchone()
            if existing is not None:
                observed = tuple(existing[index] for index in range(5))
                if observed != values:
                    raise Conflict(
                        "document version 已存在不同内容。",
                        stage="control.dver",
                    )
                return
            connection.execute(
                "INSERT INTO document_versions("
                "document_version_id, document_id, content_sha256, "
                "source_artifact_id, size_bytes, media_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version_id, *values, _now()),
            )
            connection.execute(
                "UPDATE documents SET current_version_id=?, updated_at=? "
                "WHERE document_id=?",
                (version_id, _now(), document_id),
            )

    def stage(self, entry: BlobCatalogEntry) -> None:
        """幂等登记物理 Blob 为 staged。

        Args:
            entry: 已由 Filesystem Blob Store 验证的对象。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT artifact_id, content_sha256, size_bytes, media_type, "
                "physical_locator FROM blob_objects WHERE artifact_id=?",
                (entry.artifact_id,),
            ).fetchone()
            expected = (
                entry.artifact_id,
                entry.content_sha256,
                entry.size_bytes,
                entry.media_type,
                entry.physical_locator,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise Conflict(
                        "Artifact catalog 已存在不一致对象。",
                        stage="artifact.stage",
                    )
                return
            connection.execute(
                "INSERT INTO blob_objects("
                "artifact_id, content_sha256, size_bytes, media_type, "
                "physical_state, "
                "physical_locator, created_at, created_by_job_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.artifact_id,
                    entry.content_sha256,
                    entry.size_bytes,
                    entry.media_type,
                    entry.physical_state.value,
                    entry.physical_locator,
                    _now(),
                    entry.created_by_job_id,
                ),
            )

    def commit_reference(self, reference: BlobReference) -> None:
        """新增引用并在同一事务把 staged 对象标记 available。

        Args:
            reference: Artifact 逻辑引用。

        Returns:
            无返回值。

        """
        self.commit_references((reference,))

    def commit_references(self, references: Sequence[BlobReference]) -> None:
        """在一个 SQLite 事务中提交全部引用和 available 状态。

        Args:
            references: 本次解析产生的全部逻辑引用。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            for reference in references:
                cursor = connection.execute(
                    "UPDATE blob_objects SET physical_state='available', "
                    "verified_at=? WHERE artifact_id=? "
                    "AND physical_state IN ('staged', 'available')",
                    (_now(), reference.artifact_id),
                )
                if cursor.rowcount != 1:
                    raise NotFound(
                        "可提交的 Artifact 不存在。",
                        stage="artifact.reference",
                    )
                existing = connection.execute(
                    "SELECT artifact_id, owner_type, owner_id, role, "
                    "revision_id FROM blob_references WHERE reference_id=?",
                    (reference.reference_id,),
                ).fetchone()
                values = (
                    reference.artifact_id,
                    reference.owner_type,
                    reference.owner_id,
                    reference.role,
                    reference.revision_id,
                )
                if existing is not None:
                    if tuple(existing) != values:
                        raise Conflict(
                            "Artifact reference ID 已绑定不同引用。",
                            stage="artifact.reference",
                        )
                    continue
                connection.execute(
                    "INSERT INTO blob_references("
                    "reference_id, artifact_id, owner_type, owner_id, role, "
                    "revision_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (reference.reference_id, *values, _now()),
                )

    def reference_count(self, artifact_id: str) -> int:
        """从引用表重算 Artifact 引用数。

        Args:
            artifact_id: content-addressed Artifact 对象 ID。

        Returns:
            当前引用数。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT count(*) AS value FROM blob_references "
                "WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return int(row["value"])

    def artifact_media_type(self, artifact_id: str) -> str | None:
        """读取 Blob Store 重开后需要的媒体类型。

        Args:
            artifact_id: content-addressed Artifact 对象 ID。

        Returns:
            catalog 媒体类型或 None。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT media_type FROM blob_objects WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return None if row is None else str(row["media_type"])

    def create_revision(
        self,
        revision: IndexRevisionRef,
        *,
        physical_namespace: str,
        expected_document_count: int,
        slots: Sequence[EmbeddingSlotIdentity],
        resolved_contracts: dict[str, object],
    ) -> None:
        """创建不可变 revision 和 required slot schema。

        Args:
            revision: 必须处于 CREATED 的新 revision。
            physical_namespace: 独占 Vector namespace。
            expected_document_count: 固定文档快照数量。
            slots: resolved required slots。
            resolved_contracts: 实际策略和 schema 合同。

        Returns:
            无返回值。

        """
        if revision.state is not IndexRevisionState.CREATED:
            raise RevisionStateError(
                "新 revision 必须从 CREATED 开始。", stage="revision.create"
            )
        required = (
            "parser_identity",
            "parsing_policy",
            "chunker_identity",
            "chunking_policy",
            "embedding_topology",
            "lexical_schema",
            "vector_schema",
            "chunk_payload_schema",
        )
        missing = [name for name in required if name not in resolved_contracts]
        if missing:
            raise ValidationFailed(
                "revision resolved contracts 不完整。",
                stage="revision.create",
                details={"fields": missing},
            )
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT project_id, knowledge_base_id, state, "
                "index_fingerprint, physical_vector_namespace, "
                "expected_document_count FROM index_revisions "
                "WHERE index_revision_id=?",
                (revision.index_revision_id,),
            ).fetchone()
            if existing is not None:
                identity = (
                    str(existing["project_id"]),
                    str(existing["knowledge_base_id"]),
                    str(existing["index_fingerprint"]),
                    str(existing["physical_vector_namespace"]),
                    int(existing["expected_document_count"]),
                )
                expected = (
                    revision.project_id,
                    revision.knowledge_base_id,
                    revision.index_fingerprint,
                    physical_namespace,
                    expected_document_count,
                )
                if identity != expected:
                    raise Conflict(
                        "Revision ID 已绑定不同不可变合同。",
                        stage="revision.create",
                    )
                state = IndexRevisionState(str(existing["state"]))
                if state in {
                    IndexRevisionState.ACTIVE,
                    IndexRevisionState.RETIRED,
                    IndexRevisionState.READY,
                    IndexRevisionState.FAILED_TERMINAL,
                }:
                    raise RevisionStateError(
                        "当前 revision 状态禁止重试。",
                        stage="revision.create",
                    )
                connection.execute(
                    "UPDATE index_revisions SET state='created', "
                    "failure_code=NULL, safe_message=NULL "
                    "WHERE index_revision_id=?",
                    (revision.index_revision_id,),
                )
                return
            connection.execute(
                "INSERT INTO index_revisions("
                "index_revision_id, project_id, knowledge_base_id, "
                "state, index_fingerprint, "
                "serving_compatibility_version, parser_identity_json, "
                "parsing_policy_json, chunker_identity_json, "
                "chunking_policy_json, embedding_topology_json, "
                "lexical_schema_json, "
                "vector_schema_json, chunk_payload_schema_json, "
                "physical_vector_namespace, "
                "expected_document_count, expected_chunk_count, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    revision.index_revision_id,
                    revision.project_id,
                    revision.knowledge_base_id,
                    revision.state.value,
                    revision.index_fingerprint,
                    str(
                        resolved_contracts.get(
                            "serving_compatibility_version",
                            "1",
                        )
                    ),
                    *(
                        canonical_json(resolved_contracts[name])
                        for name in required
                    ),
                    physical_namespace,
                    expected_document_count,
                    _now(),
                ),
            )
            for slot in slots:
                connection.execute(
                    "INSERT INTO embedding_slots("
                    "revision_id, slot_id, role, provider_id, model, "
                    "vector_name, "
                    "dimension, normalization, document_request_policy_json, "
                    "query_request_policy_json, adapter_revision, "
                    "max_input_tokens, required_for_activation, "
                    "document_fingerprint, query_fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        revision.index_revision_id,
                        slot.slot_id,
                        slot.role.value,
                        slot.provider_id,
                        slot.model,
                        slot.vector_name,
                        slot.dimension,
                        slot.normalization,
                        canonical_json(slot.document_request_policy),
                        canonical_json(slot.query_request_policy),
                        slot.adapter_revision,
                        slot.max_input_tokens,
                        canonical_sha256(slot.document_request_policy),
                        canonical_sha256(slot.query_request_policy),
                    ),
                )

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
        """绑定解析结果到 revision，而不改变 DocumentVersion 身份。

        Args:
            revision_id: 目标 revision。
            document_ir: canonical Document IR。
            parse_report: Parser 报告。
            chunking_report: canonical Chunk validator 聚合报告。
            parsing_policy_fingerprint: 实际 ParsingPolicy 指纹。
            part_catalog_identity: OOXML part catalog 身份。
            chunk_count: 当前文档 chunk 数。

        Returns:
            无返回值。

        """
        source = document_ir.source
        values = (
            revision_id,
            source.document_id,
            source.document_version_id,
            parse_report.parser_id,
            parse_report.parser_version,
            parsing_policy_fingerprint,
            document_ir.schema_version,
            document_ir.model_dump_json(),
            parse_report.model_dump_json(),
            chunking_report.model_dump_json(),
            part_catalog_identity,
            chunk_count,
        )
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM revision_documents WHERE revision_id=? "
                "AND document_id=?",
                (revision_id, source.document_id),
            ).fetchone()
            if existing is not None:
                observed_ir = DocumentIR.model_validate_json(
                    str(existing["document_ir_json"])
                )
                observed_parse = ParseReport.model_validate_json(
                    str(existing["parse_report_json"])
                )
                observed_chunking = ChunkingReport.model_validate_json(
                    str(existing["chunking_report_json"])
                )
                stable_identity = (
                    str(existing["revision_id"]),
                    str(existing["document_id"]),
                    str(existing["document_version_id"]),
                    str(existing["parser_id"]),
                    str(existing["parser_version"]),
                    str(existing["parsing_policy_fingerprint"]),
                    str(existing["ir_schema_version"]),
                    str(existing["part_catalog_identity"]),
                    int(existing["chunk_count"]),
                )
                expected_identity = (
                    revision_id,
                    source.document_id,
                    source.document_version_id,
                    parse_report.parser_id,
                    parse_report.parser_version,
                    parsing_policy_fingerprint,
                    document_ir.schema_version,
                    part_catalog_identity,
                    chunk_count,
                )
                normalized_parse = parse_report.model_copy(
                    update={"elapsed_seconds": 0.0}
                )
                normalized_observed_parse = observed_parse.model_copy(
                    update={"elapsed_seconds": 0.0}
                )
                normalized_ir = document_ir.model_copy(
                    update={"parse_report": normalized_parse}
                )
                normalized_observed_ir = observed_ir.model_copy(
                    update={"parse_report": normalized_observed_parse}
                )
                if (
                    stable_identity != expected_identity
                    or normalized_observed_ir != normalized_ir
                    or normalized_observed_parse != normalized_parse
                    or observed_chunking.model_copy(
                        update={"elapsed_seconds": 0.0}
                    )
                    != chunking_report.model_copy(
                        update={"elapsed_seconds": 0.0}
                    )
                ):
                    raise Conflict(
                        "Revision document 已存在不同解析结果。",
                        stage="revision.document",
                    )
                return
            connection.execute(
                "INSERT INTO revision_documents("
                "revision_id, document_id, document_version_id, parser_id, "
                "parser_version, parsing_policy_fingerprint, "
                "ir_schema_version, "
                "document_ir_json, parse_report_json, chunking_report_json, "
                "part_catalog_identity, chunk_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def set_revision_state(
        self,
        revision_id: str,
        expected: IndexRevisionState,
        target: IndexRevisionState,
    ) -> None:
        """使用 CAS 推进非终态 revision。

        Args:
            revision_id: 目标 revision。
            expected: 调用方读取的当前状态。
            target: 下一状态。

        Returns:
            无返回值。

        """
        if expected in _TERMINAL_REVISION_STATES:
            raise RevisionStateError(
                "终态 revision 禁止继续写入。", stage="revision.state"
            )
        with self._connections.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE index_revisions SET state=? "
                "WHERE index_revision_id=? AND state=?",
                (target.value, revision_id, expected.value),
            )
            if cursor.rowcount != 1:
                raise RevisionStateError(
                    "revision 状态已变化或目标不存在。", stage="revision.state"
                )

    def write_chunks(self, revision_id: str, chunks: Sequence[Chunk]) -> None:
        """原子写 chunks、Exact 和 FTS staging 行。

        Args:
            revision_id: 目标 staging revision。
            chunks: 已通过 canonical validator 的 chunks。

        Returns:
            无返回值。

        """
        if any(chunk.index_revision_id != revision_id for chunk in chunks):
            raise ValidationFailed(
                "Chunk revision ID 与写入目标不一致。", stage="revision.chunks"
            )
        if any(_has_zero_scope(chunk) for chunk in chunks):
            raise ValidationFailed(
                "正式写路径禁止零值 scope ID。", stage="revision.chunks"
            )
        with self._connections.transaction(write=True) as connection:
            write_chunks_transaction(connection, chunks)
            connection.execute(
                "UPDATE index_revisions SET expected_chunk_count=? "
                "WHERE index_revision_id=?",
                (len(chunks), revision_id),
            )
            slot_rows = connection.execute(
                "SELECT slot_id FROM embedding_slots WHERE revision_id=?",
                (revision_id,),
            ).fetchall()
            for chunk in chunks:
                for row in slot_rows:
                    connection.execute(
                        "INSERT OR IGNORE INTO revision_chunk_embeddings("
                        "revision_id, chunk_id, slot_id, cache_scope, state, "
                        "attempt, retryable, updated_at) "
                        "VALUES (?, ?, ?, 'project', 'pending', "
                        "0, 0, ?)",
                        (
                            revision_id,
                            chunk.chunk_id,
                            str(row["slot_id"]),
                            _now(),
                        ),
                    )

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
        """持久化单 Chunk/Slot 的最新尝试状态。

        Args:
            revision_id: 目标 revision。
            chunk_id: 目标 chunk。
            slot_id: 目标 slot。
            state: 新进度状态。
            cache_key: cache 命中或生成的 key。
            attempt: 非负尝试序号。
            error_code: 可选稳定错误码。
            retryable: 用户显式 retry 是否安全。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE revision_chunk_embeddings SET state=?, "
                "cache_key=COALESCE(?, cache_key), "
                "attempt=?, "
                "error_code=?, retryable=?, updated_at=? WHERE revision_id=? "
                "AND chunk_id=? AND slot_id=?",
                (
                    state.value,
                    cache_key,
                    attempt,
                    error_code,
                    int(retryable),
                    _now(),
                    revision_id,
                    chunk_id,
                    slot_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFound(
                    "Chunk/Slot 进度行不存在。", stage="embedding.progress"
                )

    def embedding_states(
        self,
        revision_id: str,
        slot_id: str,
    ) -> dict[str, ChunkEmbeddingState]:
        """读取重启后仍存在的 Chunk/Slot 进度。

        Args:
            revision_id: 目标 revision。
            slot_id: 目标 slot。

        Returns:
            chunk ID 到状态的映射。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT chunk_id, state FROM revision_chunk_embeddings "
                "WHERE revision_id=? AND slot_id=? ORDER BY chunk_id",
                (revision_id, slot_id),
            ).fetchall()
        return {
            str(row["chunk_id"]): ChunkEmbeddingState(str(row["state"]))
            for row in rows
        }

    def update_embedding_coverage(
        self,
        revision_id: str,
        slot_id: str,
        *,
        valid_vector_count: int,
    ) -> None:
        """从持久化状态聚合 coverage 并绑定 Vector Store 实际计数。

        Args:
            revision_id: 目标 revision。
            slot_id: 目标 slot。
            valid_vector_count: Vector Store 回读有效计数。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            rows = connection.execute(
                "SELECT state, count(*) AS value "
                "FROM revision_chunk_embeddings "
                "WHERE revision_id=? AND slot_id=? GROUP BY state",
                (revision_id, slot_id),
            ).fetchall()
            counts = {str(row["state"]): int(row["value"]) for row in rows}
            expected = sum(counts.values())
            ratio = 0.0 if expected == 0 else valid_vector_count / expected
            connection.execute(
                "INSERT INTO revision_embedding_coverage("
                "revision_id, slot_id, expected_chunk_count, cached_count, "
                "embedded_count, vector_written_count, valid_vector_count, "
                "failed_count, coverage_ratio, state, last_verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(revision_id, slot_id) DO UPDATE SET "
                "expected_chunk_count=excluded.expected_chunk_count, "
                "cached_count=excluded.cached_count, "
                "embedded_count=excluded.embedded_count, "
                "vector_written_count=excluded.vector_written_count, "
                "valid_vector_count=excluded.valid_vector_count, "
                "failed_count=excluded.failed_count, "
                "coverage_ratio=excluded.coverage_ratio, "
                "state=excluded.state, "
                "last_verified_at=excluded.last_verified_at",
                (
                    revision_id,
                    slot_id,
                    expected,
                    counts.get("cached", 0),
                    counts.get("embedded", 0),
                    counts.get("vector_written", 0),
                    valid_vector_count,
                    counts.get("failed", 0),
                    ratio,
                    (
                        "complete"
                        if expected > 0 and ratio == 1.0
                        else "incomplete"
                    ),
                    _now(),
                ),
            )

    def record_validation(self, evidence: RevisionValidationEvidence) -> None:
        """把实际 Store 证据写入 VALIDATING revision 并标 READY。

        Args:
            evidence: 已完整复算的激活证据。

        Returns:
            无返回值。

        """
        payload = evidence.model_dump(mode="json")
        serialized = canonical_json(payload)
        evidence_hash = canonical_sha256(payload)
        with self._connections.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE index_revisions SET state='ready', "
                "validation_evidence_json=?, "
                "validation_evidence_hash=?, validated_at=? "
                "WHERE index_revision_id=? AND state='validating'",
                (serialized, evidence_hash, _now(), evidence.revision_id),
            )
            if cursor.rowcount != 1:
                raise RevisionStateError(
                    "只有 VALIDATING revision 可标记 READY。",
                    stage="revision.validate",
                )

    def activate(
        self,
        knowledge_base_id: str,
        evidence: RevisionValidationEvidence,
        *,
        reason: str,
        trace_id: str,
    ) -> None:
        """验证 evidence hash 后原子切换 active pointer。

        Args:
            knowledge_base_id: 目标知识库。
            evidence: 刚从实际 Store 重查的证据。
            reason: 非敏感激活原因。
            trace_id: 受控 trace ID。

        Returns:
            无返回值。

        """
        evidence_hash = canonical_sha256(evidence.model_dump(mode="json"))
        now = _now()
        with self._connections.transaction(write=True) as connection:
            target = connection.execute(
                "SELECT state, knowledge_base_id, validation_evidence_hash "
                "FROM index_revisions WHERE index_revision_id=?",
                (evidence.revision_id,),
            ).fetchone()
            if target is None or str(target["state"]) != "ready":
                raise RevisionStateError(
                    "只有 READY revision 可以激活。", stage="revision.activate"
                )
            if str(target["knowledge_base_id"]) != knowledge_base_id:
                raise ValidationFailed(
                    "Revision 不属于目标知识库。", stage="revision.activate"
                )
            if str(target["validation_evidence_hash"]) != evidence_hash:
                raise ValidationFailed(
                    "激活证据已漂移。", stage="revision.activate"
                )
            kb = connection.execute(
                "SELECT active_revision_id FROM knowledge_bases "
                "WHERE knowledge_base_id=?",
                (knowledge_base_id,),
            ).fetchone()
            if kb is None:
                raise NotFound("目标知识库不存在。", stage="revision.activate")
            old_revision_id = kb["active_revision_id"]
            if old_revision_id is not None:
                connection.execute(
                    "UPDATE index_revisions SET state='retired', retired_at=? "
                    "WHERE index_revision_id=? AND state='active'",
                    (now, old_revision_id),
                )
            connection.execute(
                "UPDATE index_revisions SET state='active', activated_at=? "
                "WHERE index_revision_id=? AND state='ready'",
                (now, evidence.revision_id),
            )
            connection.execute(
                "UPDATE knowledge_bases SET active_revision_id=?, updated_at=? "
                "WHERE knowledge_base_id=?",
                (evidence.revision_id, now, knowledge_base_id),
            )
            connection.execute(
                "INSERT INTO active_revision_history("
                "knowledge_base_id, old_revision_id, new_revision_id, "
                "activated_at, "
                "reason, trace_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    knowledge_base_id,
                    old_revision_id,
                    evidence.revision_id,
                    now,
                    reason,
                    trace_id,
                ),
            )

    def active_revision_id(self, knowledge_base_id: str) -> str | None:
        """读取知识库当前 active revision。

        Args:
            knowledge_base_id: 目标知识库。

        Returns:
            active revision ID 或 None。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT active_revision_id FROM knowledge_bases "
                "WHERE knowledge_base_id=?",
                (knowledge_base_id,),
            ).fetchone()
        if row is None:
            raise NotFound("目标知识库不存在。", stage="revision.active")
        value = row["active_revision_id"]
        return None if value is None else str(value)

    def active_revision_ids(self) -> tuple[str, ...]:
        """返回启动恢复需要的全部 Active Revision ID。

        Args:
            无参数；读取当前控制面。

        Returns:
            按稳定 ID 排序的 Active Revision 序列。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT active_revision_id FROM knowledge_bases "
                "WHERE active_revision_id IS NOT NULL AND deleted_at IS NULL "
                "ORDER BY active_revision_id"
            ).fetchall()
        return tuple(str(row["active_revision_id"]) for row in rows)

    def active_query_snapshot(
        self,
        scope: KnowledgeBaseScope,
        *,
        serving_fingerprint: str,
        retrieval_policy: RetrievalPolicy,
    ) -> ActiveRevisionQuerySnapshot:
        """在一个只读事务中冻结 P07 Active Revision 查询快照。

        Args:
            scope: 调用方项目和知识库边界。
            serving_fingerprint: 当前实际查询语义指纹。
            retrieval_policy: P07 有界 provisional 策略。

        Returns:
            可贯穿一个请求的 immutable snapshot。

        Raises:
            NotFound: 知识库不存在或不属于项目。
            IndexNotReady: 知识库没有 Active Revision。
            IndexCorrupt: Active 指针、slot 或 coverage 合同损坏。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT kb.project_id, kb.knowledge_base_id, "
                "kb.active_revision_id, r.state, r.index_fingerprint, "
                "r.physical_vector_namespace, r.embedding_topology_json, "
                "r.chunk_payload_schema_json, r.expected_chunk_count "
                "FROM knowledge_bases kb LEFT JOIN index_revisions r "
                "ON r.index_revision_id=kb.active_revision_id "
                "WHERE kb.knowledge_base_id=? AND kb.deleted_at IS NULL",
                (scope.knowledge_base_id,),
            ).fetchone()
            if row is None or str(row["project_id"]) != scope.project_id:
                raise NotFound("目标知识库不存在。", stage="retrieval.snapshot")
            revision_id = row["active_revision_id"]
            if revision_id is None:
                raise IndexNotReady(
                    "知识库尚无 Active Revision。",
                    stage="retrieval.snapshot",
                )
            if row["state"] != IndexRevisionState.ACTIVE.value:
                raise IndexCorrupt(
                    "Active 指针未指向 ACTIVE revision。",
                    stage="retrieval.snapshot",
                )
            slot_rows = connection.execute(
                "SELECT * FROM embedding_slots WHERE revision_id=? "
                "ORDER BY role, slot_id",
                (revision_id,),
            ).fetchall()
            coverage_rows = connection.execute(
                "SELECT s.slot_id, s.vector_name, s.dimension, "
                "coalesce(c.valid_vector_count, 0) AS vector_count, "
                "coalesce(c.expected_chunk_count, 0) AS chunk_count "
                "FROM embedding_slots s LEFT JOIN "
                "revision_embedding_coverage c "
                "ON c.revision_id=s.revision_id AND c.slot_id=s.slot_id "
                "WHERE s.revision_id=? AND s.required_for_activation=1 "
                "ORDER BY s.role, s.slot_id",
                (revision_id,),
            ).fetchall()
        try:
            topology = EmbeddingTopology.model_validate_json(
                str(row["embedding_topology_json"])
            )
            payload_schema = json.loads(str(row["chunk_payload_schema_json"]))
        except (TypeError, ValueError) as error:
            raise IndexCorrupt(
                "Active Revision resolved contract 无法读取。",
                stage="retrieval.snapshot",
            ) from error
        slots = tuple(_slot_from_row(item) for item in slot_rows)
        if topology.slots != slots or not isinstance(payload_schema, str):
            raise IndexCorrupt(
                "Active Revision topology 或 Chunk schema 漂移。",
                stage="retrieval.snapshot",
            )
        expected_chunks = int(row["expected_chunk_count"])
        coverages = tuple(
            EmbeddingCoverage(
                slot_id=str(item["slot_id"]),
                vector_name=str(item["vector_name"]),
                vector_count=int(item["vector_count"]),
                chunk_count=int(item["chunk_count"]),
                observed_dimension=int(item["dimension"]),
            )
            for item in coverage_rows
        )
        if len(coverages) != len(slots) or any(
            coverage.chunk_count != expected_chunks for coverage in coverages
        ):
            raise IndexCorrupt(
                "Active Revision coverage 证据不完整。",
                stage="retrieval.snapshot",
            )
        revision = IndexRevisionRef(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            index_revision_id=str(revision_id),
            index_fingerprint=str(row["index_fingerprint"]),
            state=IndexRevisionState.ACTIVE,
        )
        return ActiveRevisionQuerySnapshot(
            revision=revision,
            serving_fingerprint=serving_fingerprint,
            topology=topology,
            coverages=coverages,
            vector_spec=RevisionVectorSpec(
                revision=revision,
                physical_namespace=str(row["physical_vector_namespace"]),
                slots=slots,
            ),
            lexical_namespace=f"sqlite:{revision_id}",
            exact_namespace=f"sqlite:{revision_id}",
            chunk_payload_schema=payload_schema,
            retrieval_policy=retrieval_policy,
        )

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        """批量回读并验证 P07 canonical chunks。

        Args:
            snapshot: 请求开始时冻结的 revision。
            chunk_ids: 有界候选 ID，顺序必须保留。

        Returns:
            与去重输入顺序一致的 canonical chunks。

        Raises:
            IndexCorrupt: 任一候选缺失、重复或 scope 身份漂移。

        """
        ordered = tuple(dict.fromkeys(chunk_ids))
        if not ordered:
            return ()
        if len(ordered) > _MAX_HYDRATION_CHUNKS:
            raise ValueError("单次 canonical hydration 上限为 200。")
        revision = snapshot.revision
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT c.chunk_id, c.chunk_json, d.display_name "
                "FROM chunks c JOIN index_revisions r "
                "ON r.index_revision_id=c.revision_id "
                "JOIN documents d ON d.document_id=c.document_id "
                "WHERE c.revision_id=? AND c.chunk_id IN "
                "(SELECT value FROM json_each(?)) "
                "AND r.project_id=? AND r.knowledge_base_id=?",
                (
                    revision.index_revision_id,
                    canonical_json(ordered),
                    revision.project_id,
                    revision.knowledge_base_id,
                ),
            ).fetchall()
        by_id = {str(row["chunk_id"]): row for row in rows}
        missing = tuple(
            chunk_id for chunk_id in ordered if chunk_id not in by_id
        )
        if missing:
            raise IndexCorrupt(
                "检索候选无法从 canonical Chunk Store 回读。",
                stage="retrieval.hydrate",
                details={"missing_chunk_ids": list(missing)},
            )
        hydrated: list[HydratedChunk] = []
        for chunk_id in ordered:
            source = by_id[chunk_id]
            chunk = Chunk.model_validate_json(str(source["chunk_json"]))
            if (
                chunk.project_id != revision.project_id
                or chunk.knowledge_base_id != revision.knowledge_base_id
                or chunk.index_revision_id != revision.index_revision_id
            ):
                raise IndexCorrupt(
                    "Canonical Chunk scope/revision 身份漂移。",
                    stage="retrieval.hydrate",
                    details={"chunk_id": chunk_id},
                )
            hydrated.append(
                HydratedChunk(
                    chunk=chunk, display_name=str(source["display_name"])
                )
            )
        return tuple(hydrated)

    def section_chunk_ids(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        *,
        document_version_id: str,
        section_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        """读取同 revision/document version/section 的有界 ID。

        Args:
            snapshot: 请求开始时冻结的 revision。
            document_version_id: 不可变逻辑文档版本。
            section_id: canonical Chunk section。
            limit: 最大返回数。

        Returns:
            稳定排序的 Chunk ID。

        """
        if limit <= 0 or limit > _MAX_SECTION_CHUNKS:
            raise ValueError("section expansion limit 必须在 1..20。")
        revision = snapshot.revision
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT c.chunk_id FROM chunks c JOIN index_revisions r "
                "ON r.index_revision_id=c.revision_id "
                "WHERE c.revision_id=? AND c.document_version_id=? "
                "AND c.section_id=? AND r.project_id=? "
                "AND r.knowledge_base_id=? ORDER BY c.chunk_id LIMIT ?",
                (
                    revision.index_revision_id,
                    document_version_id,
                    section_id,
                    revision.project_id,
                    revision.knowledge_base_id,
                    limit,
                ),
            ).fetchall()
        return tuple(str(item["chunk_id"]) for item in rows)

    def knowledge_base_scope(self, knowledge_base_id: str) -> tuple[str, str]:
        """读取知识库的 project 与 Profile 身份。

        Args:
            knowledge_base_id: 目标知识库 ID。

        Returns:
            project ID 与 Profile ID。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT project_id, profile_id FROM knowledge_bases "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
        if row is None:
            raise NotFound("目标知识库不存在。", stage="control.kb")
        return str(row["project_id"]), str(row["profile_id"])

    def knowledge_base_summary(
        self, knowledge_base_id: str
    ) -> dict[str, object]:
        """返回知识库与 revision 的非敏感管理摘要。

        Args:
            knowledge_base_id: 目标知识库 ID。

        Returns:
            Active 指针与 Revision 计数摘要。

        """
        with self._connections.transaction() as connection:
            kb = connection.execute(
                "SELECT knowledge_base_id, project_id, profile_id, "
                "active_revision_id FROM knowledge_bases "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
            revisions = connection.execute(
                "SELECT index_revision_id, state, expected_document_count, "
                "expected_chunk_count, index_fingerprint FROM index_revisions "
                "WHERE knowledge_base_id=? ORDER BY created_at, "
                "index_revision_id",
                (knowledge_base_id,),
            ).fetchall()
        if kb is None:
            raise NotFound("目标知识库不存在。", stage="control.kb")
        return {
            **dict(kb),
            "revisions": tuple(dict(row) for row in revisions),
        }

    def active_documents(
        self, knowledge_base_id: str
    ) -> tuple[tuple[DocumentRef, str, str], ...]:
        """读取 active revision 的文档身份、source Artifact 和媒体类型。

        Args:
            knowledge_base_id: 目标知识库 ID。

        Returns:
            文档引用、Artifact ID 与媒体类型序列。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT d.project_id, d.knowledge_base_id, d.document_id, "
                "d.display_name, dv.source_artifact_id, dv.media_type "
                "FROM knowledge_bases kb JOIN revision_documents rd "
                "ON rd.revision_id=kb.active_revision_id "
                "JOIN documents d ON d.document_id=rd.document_id "
                "JOIN document_versions dv "
                "ON dv.document_version_id=rd.document_version_id "
                "WHERE kb.knowledge_base_id=? ORDER BY d.document_id",
                (knowledge_base_id,),
            ).fetchall()
        return tuple(
            (
                DocumentRef(
                    project_id=str(row["project_id"]),
                    knowledge_base_id=str(row["knowledge_base_id"]),
                    document_id=str(row["document_id"]),
                    display_name=str(row["display_name"]),
                ),
                str(row["source_artifact_id"]),
                str(row["media_type"]),
            )
            for row in rows
        )

    def cached_revision_vectors(
        self, revision_id: str
    ) -> dict[str, dict[str, tuple[float, ...]]]:
        """按持久化 cache key 回读 revision 的完整 named vectors。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            slot 和 Chunk 到向量的两层映射。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT e.chunk_id, e.slot_id, e.cache_key, s.dimension, "
                "c.vector_encoding_version, c.vector_bytes "
                "FROM revision_chunk_embeddings e "
                "JOIN embedding_slots s ON s.revision_id=e.revision_id "
                "AND s.slot_id=e.slot_id "
                "LEFT JOIN embedding_cache c ON c.cache_key=e.cache_key "
                "WHERE e.revision_id=? ORDER BY e.slot_id, e.chunk_id",
                (revision_id,),
            ).fetchall()
        result: dict[str, dict[str, tuple[float, ...]]] = {}
        for row in rows:
            if row["cache_key"] is None or row["vector_bytes"] is None:
                raise ValidationFailed(
                    "Revision 缺少可恢复的 embedding cache。",
                    stage="revision.backfill",
                )
            if str(row["vector_encoding_version"]) != "float32-le-v1":
                raise ValidationFailed(
                    "Revision embedding 编码版本不兼容。",
                    stage="revision.backfill",
                )
            dimension = int(row["dimension"])
            payload = bytes(row["vector_bytes"])
            if len(payload) != dimension * 4:
                raise ValidationFailed(
                    "Revision embedding 字节长度损坏。",
                    stage="revision.backfill",
                )
            vector = tuple(struct.unpack(f"<{dimension}f", payload))
            result.setdefault(str(row["slot_id"]), {})[str(row["chunk_id"])] = (
                vector
            )
        return result

    def revision_counts(self, revision_id: str) -> tuple[int, int, int]:
        """从数据库实际读取 document/chunk/FTS 行数。

        Args:
            revision_id: 目标 revision。

        Returns:
            document、chunk、FTS 行数。

        """
        with self._connections.transaction() as connection:
            document_count = connection.execute(
                "SELECT count(*) AS value FROM revision_documents "
                "WHERE revision_id=?",
                (revision_id,),
            ).fetchone()["value"]
            chunk_count = connection.execute(
                "SELECT count(*) AS value FROM chunks WHERE revision_id=?",
                (revision_id,),
            ).fetchone()["value"]
            fts_count = connection.execute(
                "SELECT count(*) AS value FROM chunks_fts WHERE revision_id=?",
                (revision_id,),
            ).fetchone()["value"]
        return int(document_count), int(chunk_count), int(fts_count)

    def embedding_coverage_rows(
        self,
        revision_id: str,
    ) -> dict[str, tuple[int, int, int, float, str]]:
        """读取 required slot 的持久化 coverage 证据。

        Args:
            revision_id: 目标 revision。

        Returns:
            slot 到 expected、valid、failed、ratio、state 的映射。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT s.slot_id, c.expected_chunk_count, "
                "c.valid_vector_count, "
                "c.failed_count, c.coverage_ratio, c.state "
                "FROM embedding_slots s "
                "LEFT JOIN revision_embedding_coverage c "
                "ON c.revision_id=s.revision_id AND c.slot_id=s.slot_id "
                "WHERE s.revision_id=? AND s.required_for_activation=1",
                (revision_id,),
            ).fetchall()
        result: dict[str, tuple[int, int, int, float, str]] = {}
        for row in rows:
            if row["expected_chunk_count"] is None:
                result[str(row["slot_id"])] = (0, 0, 0, 0.0, "missing")
            else:
                result[str(row["slot_id"])] = (
                    int(row["expected_chunk_count"]),
                    int(row["valid_vector_count"]),
                    int(row["failed_count"]),
                    float(row["coverage_ratio"]),
                    str(row["state"]),
                )
        return result

    def running_writer_count(self, revision_id: str) -> int:
        """统计仍可能修改 revision 的 RUNNING job。

        Args:
            revision_id: 目标 revision。

        Returns:
            RUNNING writer 数量。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT count(*) AS value FROM ingestion_jobs "
                "WHERE revision_id=? AND state='running' "
                "AND stage NOT IN ('validating', 'activate')",
                (revision_id,),
            ).fetchone()
        return int(row["value"])

    def document_scope_violation_count(self, revision_id: str) -> int:
        """从 FK 关联复核 revision snapshot 的 project/KB 归属。

        Args:
            revision_id: 目标 revision。

        Returns:
            scope 不一致的绑定数量。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT count(*) AS value FROM revision_documents rd "
                "JOIN index_revisions r ON r.index_revision_id=rd.revision_id "
                "JOIN documents d ON d.document_id=rd.document_id "
                "JOIN document_versions dv "
                "ON dv.document_version_id=rd.document_version_id "
                "WHERE rd.revision_id=? AND (d.project_id<>r.project_id "
                "OR d.knowledge_base_id<>r.knowledge_base_id "
                "OR dv.document_id<>rd.document_id)",
                (revision_id,),
            ).fetchone()
        return int(row["value"])

    def gc_snapshot(
        self,
        *,
        protected_retired_count: int,
        grace_before: str,
    ) -> dict[str, object]:
        """读取 GC Plan 绑定的完整保护与候选快照。

        Args:
            protected_retired_count: 每个知识库保留的最近 retired 数。
            grace_before: staged orphan 必须早于的 ISO 时间。

        Returns:
            不含正文、向量或绝对路径的快照。

        """
        with self._connections.transaction() as connection:
            active = [
                str(row["active_revision_id"])
                for row in connection.execute(
                    "SELECT active_revision_id FROM knowledge_bases "
                    "WHERE active_revision_id IS NOT NULL "
                    "ORDER BY active_revision_id"
                ).fetchall()
            ]
            running = [
                str(row["revision_id"])
                for row in connection.execute(
                    "SELECT DISTINCT revision_id FROM ingestion_jobs "
                    "WHERE state IN ('pending', 'running') "
                    "AND revision_id IS NOT NULL ORDER BY revision_id"
                ).fetchall()
            ]
            retired_rows = connection.execute(
                "SELECT index_revision_id, knowledge_base_id "
                "FROM index_revisions WHERE state='retired' "
                "ORDER BY knowledge_base_id, retired_at DESC, "
                "index_revision_id"
            ).fetchall()
            protected_retired: list[str] = []
            revision_candidates: list[str] = []
            seen_per_kb: dict[str, int] = {}
            for row in retired_rows:
                kb_id = str(row["knowledge_base_id"])
                seen = seen_per_kb.get(kb_id, 0)
                revision_id = str(row["index_revision_id"])
                if seen < protected_retired_count or revision_id in running:
                    protected_retired.append(revision_id)
                else:
                    revision_candidates.append(revision_id)
                seen_per_kb[kb_id] = seen + 1
            orphan_blobs = [
                str(row["artifact_id"])
                for row in connection.execute(
                    "SELECT b.artifact_id FROM blob_objects b "
                    "LEFT JOIN blob_references r "
                    "ON r.artifact_id=b.artifact_id "
                    "WHERE r.artifact_id IS NULL AND b.physical_state='staged' "
                    "AND b.created_at<? ORDER BY b.artifact_id",
                    (grace_before,),
                ).fetchall()
            ]
            blob_references = [
                {
                    "artifact_id": str(row["artifact_id"]),
                    "reference_count": int(row["reference_count"]),
                }
                for row in connection.execute(
                    "SELECT artifact_id, count(*) AS reference_count "
                    "FROM blob_references GROUP BY artifact_id "
                    "ORDER BY artifact_id"
                ).fetchall()
            ]
            vector_collections = [
                str(row["physical_vector_namespace"])
                for row in connection.execute(
                    "SELECT physical_vector_namespace FROM index_revisions "
                    "ORDER BY physical_vector_namespace"
                ).fetchall()
            ]
            cache = connection.execute(
                "SELECT count(*) AS row_count, "
                "min(last_used_at) AS oldest_last_used_at "
                "FROM embedding_cache"
            ).fetchone()
        return {
            "protected_retired_count": protected_retired_count,
            "grace_before": grace_before,
            "active_revisions": active,
            "protected_retired_revisions": protected_retired,
            "running_job_revisions": running,
            "revision_candidates": revision_candidates,
            "orphan_blob_candidates": orphan_blobs,
            "blob_reference_snapshot": blob_references,
            "vector_collections": vector_collections,
            "embedding_cache_snapshot": {
                "row_count": int(cache["row_count"]),
                "oldest_last_used_at": cache["oldest_last_used_at"],
            },
        }

    def save_gc_plan(
        self,
        plan_id: str,
        database_identity: str,
        snapshot: Mapping[str, object],
        plan_hash: str,
    ) -> None:
        """持久化 dry-run GC Plan。

        Args:
            plan_id: 稳定 GC Plan ID。
            database_identity: 不暴露路径的数据库身份摘要。
            snapshot: 保护集和候选集。
            plan_hash: canonical plan hash。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO gc_plans(plan_id, database_identity, "
                "snapshot_json, plan_hash, state, created_at) "
                "VALUES (?, ?, ?, ?, 'planned', ?)",
                (
                    plan_id,
                    database_identity,
                    canonical_json(snapshot),
                    plan_hash,
                    _now(),
                ),
            )

    def load_gc_plan(self, plan_id: str) -> tuple[str, dict[str, object], str]:
        """读取尚未 apply 的 GC Plan。

        Args:
            plan_id: 目标 Plan ID。

        Returns:
            数据库身份、快照和 plan hash。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT database_identity, snapshot_json, plan_hash "
                "FROM gc_plans "
                "WHERE plan_id=? AND state='planned'",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise NotFound("可应用的 GC Plan 不存在。", stage="gc.load")
        snapshot = json.loads(str(row["snapshot_json"]))
        if not isinstance(snapshot, dict):
            raise ValidationFailed("GC Plan snapshot 已损坏。", stage="gc.load")
        return str(row["database_identity"]), snapshot, str(row["plan_hash"])

    def revision_vector_spec(self, revision_id: str) -> RevisionVectorSpec:
        """严格重建 GC 删除所需 Vector revision schema。

        Args:
            revision_id: 目标 retired revision。

        Returns:
            完整 Core Vector spec。

        """
        with self._connections.transaction() as connection:
            revision = connection.execute(
                "SELECT project_id, knowledge_base_id, state, "
                "index_fingerprint, "
                "physical_vector_namespace FROM index_revisions "
                "WHERE index_revision_id=?",
                (revision_id,),
            ).fetchone()
            slots = connection.execute(
                "SELECT * FROM embedding_slots WHERE revision_id=? "
                "ORDER BY role, slot_id",
                (revision_id,),
            ).fetchall()
        if revision is None:
            raise NotFound("GC revision 不存在。", stage="gc.spec")
        slot_models = tuple(
            EmbeddingSlotIdentity(
                slot_id=str(row["slot_id"]),
                role=EmbeddingSlotRole(str(row["role"])),
                provider_id=str(row["provider_id"]),
                model=str(row["model"]),
                vector_name=str(row["vector_name"]),
                dimension=int(row["dimension"]),
                max_input_tokens=int(row["max_input_tokens"]),
                adapter_revision=str(row["adapter_revision"]),
                document_request_policy=json.loads(
                    str(row["document_request_policy_json"])
                ),
                query_request_policy=json.loads(
                    str(row["query_request_policy_json"])
                ),
                normalization=str(row["normalization"]),
            )
            for row in slots
        )
        return RevisionVectorSpec(
            revision=IndexRevisionRef(
                project_id=str(revision["project_id"]),
                knowledge_base_id=str(revision["knowledge_base_id"]),
                index_revision_id=revision_id,
                index_fingerprint=str(revision["index_fingerprint"]),
                state=IndexRevisionState(str(revision["state"])),
            ),
            physical_namespace=str(revision["physical_vector_namespace"]),
            slots=slot_models,
        )

    def delete_retired_revision(self, revision_id: str) -> None:
        """在 Vector collection 删除成功后清理 retired 控制记录。

        Args:
            revision_id: 必须仍为未受保护的 RETIRED revision。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            state = connection.execute(
                "SELECT state FROM index_revisions WHERE index_revision_id=?",
                (revision_id,),
            ).fetchone()
            if state is None or str(state["state"]) != "retired":
                raise RevisionStateError(
                    "GC 只能删除 RETIRED revision。", stage="gc.delete"
                )
            row_ids = tuple(
                int(row["row_id"])
                for row in connection.execute(
                    "SELECT row_id FROM chunks WHERE revision_id=?",
                    (revision_id,),
                ).fetchall()
            )
            for row_id in row_ids:
                connection.execute(
                    "DELETE FROM chunks_fts WHERE rowid=?", (row_id,)
                )
            connection.execute(
                "DELETE FROM exact_identifiers WHERE revision_id=?",
                (revision_id,),
            )
            connection.execute(
                "DELETE FROM revision_embedding_coverage WHERE revision_id=?",
                (revision_id,),
            )
            connection.execute(
                "DELETE FROM revision_chunk_embeddings WHERE revision_id=?",
                (revision_id,),
            )
            connection.execute(
                "DELETE FROM embedding_slots WHERE revision_id=?",
                (revision_id,),
            )
            connection.execute(
                "DELETE FROM chunks WHERE revision_id=?", (revision_id,)
            )
            connection.execute(
                "DELETE FROM revision_documents WHERE revision_id=?",
                (revision_id,),
            )
            connection.execute(
                "DELETE FROM blob_references WHERE revision_id=?",
                (revision_id,),
            )
            connection.execute(
                "DELETE FROM index_revisions WHERE index_revision_id=?",
                (revision_id,),
            )

    def claim_orphan_blob(self, artifact_id: str) -> bool:
        """在写事务内确认无引用并标记 quarantine。

        Args:
            artifact_id: GC Plan 中的 staged orphan。

        Returns:
            成功获取删除权时为 True。

        """
        with self._connections.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE blob_objects SET physical_state='quarantine' "
                "WHERE artifact_id=? AND physical_state='staged' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM blob_references WHERE artifact_id=?)",
                (artifact_id, artifact_id),
            )
        return cursor.rowcount == 1

    def finish_orphan_blob(self, artifact_id: str, *, deleted: bool) -> None:
        """完成或回滚一次物理 Blob GC。

        Args:
            artifact_id: 已 claim 的 Artifact。
            deleted: 物理删除是否成功。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            if deleted:
                connection.execute(
                    "DELETE FROM blob_objects WHERE artifact_id=? "
                    "AND physical_state='quarantine'",
                    (artifact_id,),
                )
            else:
                connection.execute(
                    "UPDATE blob_objects SET physical_state='staged' "
                    "WHERE artifact_id=? AND physical_state='quarantine'",
                    (artifact_id,),
                )

    def mark_gc_plan_applied(self, plan_id: str) -> None:
        """把完整执行成功的 GC Plan 标记 applied。

        Args:
            plan_id: 目标 Plan。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE gc_plans SET state='applied', applied_at=? "
                "WHERE plan_id=? AND state='planned'",
                (_now(), plan_id),
            )

    def revision_row(self, revision_id: str) -> dict[str, object]:
        """读取不含正文的 revision 控制行。

        Args:
            revision_id: 目标 revision。

        Returns:
            字段名到标量值的副本。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT index_revision_id, project_id, knowledge_base_id, "
                "state, "
                "index_fingerprint, physical_vector_namespace, "
                "expected_document_count, expected_chunk_count, "
                "validation_evidence_hash FROM index_revisions "
                "WHERE index_revision_id=?",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise NotFound("revision 不存在。", stage="revision.read")
        return dict(row)

    def chunk_rows(self, revision_id: str) -> tuple[Chunk, ...]:
        """严格反序列化 revision 的 canonical chunks。

        Args:
            revision_id: 目标 revision。

        Returns:
            按 chunk ID 排序的 Core chunks。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT chunk_json FROM chunks WHERE revision_id=? "
                "ORDER BY chunk_id",
                (revision_id,),
            ).fetchall()
        return tuple(
            Chunk.model_validate_json(str(row["chunk_json"])) for row in rows
        )

    def parse_rows(
        self,
        revision_id: str,
    ) -> tuple[tuple[DocumentIR, ParseReport, ChunkingReport], ...]:
        """严格读取 Document IR、ParseReport 与 ChunkingReport。

        Args:
            revision_id: 目标 revision。

        Returns:
            每个文档的 canonical IR、解析报告和分块报告。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT document_ir_json, parse_report_json, "
                "chunking_report_json "
                "FROM revision_documents "
                "WHERE revision_id=? ORDER BY document_id",
                (revision_id,),
            ).fetchall()
        return tuple(
            (
                DocumentIR.model_validate_json(str(row["document_ir_json"])),
                ParseReport.model_validate_json(str(row["parse_report_json"])),
                ChunkingReport.model_validate_json(
                    str(row["chunking_report_json"])
                ),
            )
            for row in rows
        )

    def put(self, record: MetadataRecord) -> None:
        """保留 MetadataStorePort 兼容写入。

        Args:
            record: 命名空间、键和值。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO metadata(namespace, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE "
                "SET value=excluded.value",
                (record.namespace, record.key, canonical_json(record.value)),
            )

    def get(self, namespace: str, key: str) -> MetadataRecord | None:
        """保留 MetadataStorePort 兼容读取。

        Args:
            namespace: 受控命名空间。
            key: 记录键。

        Returns:
            找到的记录或 None。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["value"]))
        if not isinstance(value, dict):
            raise ValidationFailed(
                "metadata canonical JSON 已损坏。", stage="control.metadata"
            )
        return MetadataRecord(
            namespace=namespace,
            key=key,
            value=freeze_json_object(value),
        )

    def close(self) -> None:
        """幂等关闭控制面。

        Args:
            无参数；不拥有独立长连接。

        Returns:
            无返回值。

        """
        self._closed = True


def _has_zero_scope(chunk: Chunk) -> bool:
    return any(
        value.endswith("0" * 32)
        for value in (
            chunk.project_id,
            chunk.knowledge_base_id,
            chunk.index_revision_id,
        )
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["SqliteControlStore"]
