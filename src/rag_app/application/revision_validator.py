"""从实际 SQLite/FTS/Vector Store 复算 P06 激活门。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    Chunk,
    ChunkingReport,
    DocumentIR,
    ParseReport,
    RevisionValidationEvidence,
    RevisionVectorSpec,
    VectorPointAudit,
    validate_document_ir,
    vector_point_id,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.ports import ChunkValidationPort, VectorStorePort

_ZERO_QUALITY_FIELDS = (
    "stable_id_duplicate_count",
    "missing_source_chars",
    "cross_section_violations",
    "cross_group_violations",
    "oversize_violations",
    "missing_child_group_count",
    "missing_note_ref_count",
    "orphan_relation_count",
)


class _RevisionValidationStore(Protocol):
    """Validator 所需的 SQLite 实际读取视图。"""

    def revision_counts(self, revision_id: str) -> tuple[int, int, int]:
        """读取 Revision 实际计数。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            文档、Chunk 和 FTS 行数。

        """
        ...

    def revision_row(self, revision_id: str) -> dict[str, object]:
        """读取 Revision 控制行。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            非敏感控制字段映射。

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

    def parse_rows(
        self,
        revision_id: str,
    ) -> tuple[tuple[DocumentIR, ParseReport, ChunkingReport], ...]:
        """读取解析与分块报告。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            Document IR、解析报告和分块报告序列。

        """
        ...

    def embedding_coverage_rows(
        self,
        revision_id: str,
    ) -> dict[str, tuple[int, int, int, float, str]]:
        """读取 required slot coverage。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            slot 到实际 coverage 字段的映射。

        """
        ...

    def running_writer_count(self, revision_id: str) -> int:
        """统计仍可写入的 Job。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            运行中 writer 数量。

        """
        ...

    def document_scope_violation_count(self, revision_id: str) -> int:
        """统计文档 scope 违规。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            违规绑定数量。

        """
        ...


class RevisionValidator:
    """不信任调用栈内列表，只使用重新读取和 canonical validator。"""

    def __init__(
        self,
        control: _RevisionValidationStore,
        vector_store: VectorStorePort,
        chunk_validator: ChunkValidationPort,
    ) -> None:
        """保存实际 Store 与 resolved chunking 合同。

        Args:
            control: SQLite 权威读取视图。
            vector_store: Memory 或 Qdrant revision Store。
            chunk_validator: 使用 resolved 策略的持久化 Chunk 校验器。

        Returns:
            无返回值。

        """
        self._control = control
        self._vector_store = vector_store
        self._chunk_validator = chunk_validator

    def validate(
        self,
        spec: RevisionVectorSpec,
        *,
        current_index_fingerprint: str,
    ) -> RevisionValidationEvidence:
        """执行完整激活门并返回可哈希证据。

        Args:
            spec: revision 与 named-vector schema。
            current_index_fingerprint: 当前 composition 实际指纹。

        Returns:
            可写入 SQLite 并用于原子激活的证据。

        Raises:
            ValidationFailed: 任一实际 Store 或质量断言失败。

        """
        revision_id = spec.revision.index_revision_id
        row = self._control.revision_row(revision_id)
        if row["index_fingerprint"] != current_index_fingerprint:
            raise ValidationFailed(
                "Revision index fingerprint 已漂移。", stage="revision.validate"
            )
        document_count, chunk_count, fts_count = self._control.revision_counts(
            revision_id
        )
        expected_document_count = _required_int(row, "expected_document_count")
        expected_chunk_count = _required_int(row, "expected_chunk_count")
        if document_count != expected_document_count:
            raise ValidationFailed(
                "Revision document snapshot 不完整。", stage="revision.validate"
            )
        if chunk_count != expected_chunk_count or fts_count != chunk_count:
            raise ValidationFailed(
                "Chunk/FTS 实际计数不一致。", stage="revision.validate"
            )
        if self._control.document_scope_violation_count(revision_id):
            raise ValidationFailed(
                "DocumentVersion scope 验证失败。", stage="revision.validate"
            )
        chunks = self._control.chunk_rows(revision_id)
        report_checks = self._validate_documents(
            chunks, self._control.parse_rows(revision_id)
        )
        inventory = self._vector_store.audit_revision(spec)
        expected_inventory = tuple(
            sorted(
                (_expected_point(spec, chunk) for chunk in chunks),
                key=lambda item: (
                    item.point_id or "",
                    item.chunk_id or "",
                ),
            )
        )
        if (
            inventory.invalid_record_count
            or inventory.raw_record_count != chunk_count
            or inventory.points != expected_inventory
        ):
            raise ValidationFailed(
                "Vector Point 未与 canonical Chunk 全量一致。",
                stage="revision.validate",
            )
        vector_counts = {
            slot.vector_name: sum(
                slot.vector_name in point.vector_names
                for point in inventory.points
            )
            for slot in spec.slots
        }
        coverage = self._control.embedding_coverage_rows(revision_id)
        for slot in spec.slots:
            values = coverage.get(slot.slot_id)
            if values != (chunk_count, chunk_count, 0, 1.0, "complete"):
                raise ValidationFailed(
                    "Required slot coverage 未达到 100%。",
                    stage="revision.validate",
                )
            if vector_counts.get(slot.vector_name) != chunk_count:
                raise ValidationFailed(
                    "Named vector 实际计数不完整。", stage="revision.validate"
                )
        running = self._control.running_writer_count(revision_id)
        if running:
            raise ValidationFailed(
                "仍有 RUNNING writer 修改 revision。", stage="revision.validate"
            )
        probe_passed = self._deterministic_probe(spec, chunks)
        if not probe_passed:
            raise ValidationFailed(
                "Vector deterministic probe 失败。", stage="revision.validate"
            )
        return RevisionValidationEvidence(
            revision_id=revision_id,
            index_fingerprint=current_index_fingerprint,
            document_count=document_count,
            chunk_count=chunk_count,
            fts_count=fts_count,
            vector_counts=tuple(sorted(vector_counts.items())),
            report_checks=freeze_json_object(report_checks),
            deterministic_probe_passed=True,
            running_writer_count=running,
            vector_inventory_hash=inventory.inventory_hash,
        )

    def _validate_documents(
        self,
        chunks: Sequence[Chunk],
        rows: Sequence[tuple[DocumentIR, ParseReport, ChunkingReport]],
    ) -> dict[str, object]:
        checks: dict[str, object] = {}
        for document_ir, parse_report, stored_report in rows:
            validate_document_ir(document_ir)
            if parse_report != document_ir.parse_report:
                raise ValidationFailed(
                    "ParseReport 与 Document IR 不一致。",
                    stage="revision.validate",
                )
            document_chunks = tuple(
                chunk
                for chunk in chunks
                if chunk.version == document_ir.version
            )
            rebuilt = self._chunk_validator.validate_persisted(
                document_chunks, document_ir
            )
            if rebuilt != stored_report.model_copy(
                update={"elapsed_seconds": 0.0}
            ):
                raise ValidationFailed(
                    "ChunkingReport 复算不一致。", stage="revision.validate"
                )
            if stored_report.source_span_coverage != 1.0:
                raise ValidationFailed(
                    "Source span coverage 未达到 1.0。",
                    stage="revision.validate",
                )
            for field_name in _ZERO_QUALITY_FIELDS:
                value = int(getattr(stored_report, field_name))
                if value != 0:
                    raise ValidationFailed(
                        "Chunking quality 激活断言失败。",
                        stage="revision.validate",
                    )
                checks[field_name] = 0
        checks["source_span_coverage"] = 1.0
        return checks

    def _deterministic_probe(
        self,
        spec: RevisionVectorSpec,
        chunks: Sequence[Chunk],
    ) -> bool:
        if not chunks:
            return False
        point_id = vector_point_id(
            spec.revision.index_revision_id, chunks[0].chunk_id
        )
        fetched = self._vector_store.fetch_points(spec, (point_id,))
        if len(fetched) != 1:
            return False
        slot = spec.slots[0]
        vector = fetched[0].vector_map().get(slot.vector_name)
        if vector is None:
            return False
        hits = self._vector_store.search_named(
            spec,
            slot_id=slot.slot_id,
            vector_name=slot.vector_name,
            query_vector=vector,
            limit=len(chunks),
        )
        return any(hit.point_id == point_id for hit in hits)


def _required_int(row: dict[str, object], key: str) -> int:
    """从 SQLite 映射读取非布尔整数。"""
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationFailed(
            f"Revision 字段 {key} 已损坏。", stage="revision.validate"
        )
    return value


def _expected_point(
    spec: RevisionVectorSpec,
    chunk: Chunk,
) -> VectorPointAudit:
    """从 canonical Chunk 构造不可泄密的预期 Point 审计项。"""
    dimensions = tuple(
        sorted((slot.vector_name, slot.dimension) for slot in spec.slots)
    )
    return VectorPointAudit(
        point_id=vector_point_id(
            spec.revision.index_revision_id, chunk.chunk_id
        ),
        convertible=True,
        reason_code="OK",
        chunk_id=chunk.chunk_id,
        document_id=chunk.version.document_id,
        document_version_id=chunk.version.document_version_id,
        role=chunk.role.value,
        section_id=chunk.section_id,
        neighbor_group_id=chunk.neighbor_group_id,
        content_sha256=chunk.content_sha256,
        vector_names=tuple(name for name, _ in dimensions),
        vector_dimensions=dimensions,
    )


__all__ = ["RevisionValidator"]
