"""P06 不可变 IndexRevision 与 named-vector 公共模型。"""

from __future__ import annotations

import math
import uuid
from enum import StrEnum
from typing import Self

from pydantic import (
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)
from rag_app.core.models.lifecycle import IndexRevisionRef
from rag_app.core.models.provider import EmbeddingSlotIdentity


class ChunkEmbeddingState(StrEnum):
    """单个 revision/chunk/slot 的可恢复进度。"""

    PENDING = "pending"
    CACHED = "cached"
    EMBEDDED = "embedded"
    VECTOR_WRITTEN = "vector_written"
    FAILED = "failed"


class RevisionVectorSpec(FrozenModel):
    """一个不可变 revision 的完整 named-vector schema。"""

    revision: IndexRevisionRef
    physical_namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9_]+$",
    )
    slots: tuple[EmbeddingSlotIdentity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_slots(self) -> Self:
        if len({slot.slot_id for slot in self.slots}) != len(self.slots):
            raise ValueError("revision slot ID 必须唯一。")
        if len({slot.vector_name for slot in self.slots}) != len(self.slots):
            raise ValueError("revision vector name 必须唯一。")
        return self

    def slot(self, slot_id: str) -> EmbeddingSlotIdentity:
        """按 ID 读取 slot。

        Args:
            slot_id: 必须精确匹配的 slot ID。

        Returns:
            对应的向量空间身份。

        Raises:
            KeyError: slot 不属于该 revision。

        """
        for slot in self.slots:
            if slot.slot_id == slot_id:
                return slot
        raise KeyError(slot_id)

    def has_same_schema(self, other: RevisionVectorSpec) -> bool:
        """比较向量 schema，明确忽略 revision 生命周期状态。

        Args:
            other: 待比较的 revision vector spec。

        Returns:
            scope、revision、fingerprint、namespace 与 slots 是否相同。

        """
        revision = self.revision
        other_revision = other.revision
        return (
            self.physical_namespace == other.physical_namespace
            and self.slots == other.slots
            and revision.project_id == other_revision.project_id
            and revision.knowledge_base_id == other_revision.knowledge_base_id
            and revision.index_revision_id
            == other_revision.index_revision_id
            and revision.index_fingerprint
            == other_revision.index_fingerprint
        )


class VectorPointPayload(FrozenModel):
    """Qdrant 仅保存定位和过滤字段的小型 payload。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    role: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    neighbor_group_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NamedVectorPoint(FrozenModel):
    """一次性携带全部 required named vectors 的完整 Point。"""

    point_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    payload: VectorPointPayload
    vectors: tuple[tuple[str, tuple[StrictFloat, ...]], ...] = Field(repr=False)

    @field_validator("vectors", mode="before")
    @classmethod
    def _freeze_vectors(cls, value: object) -> object:
        if isinstance(value, dict):
            return tuple(sorted(value.items()))
        return value

    @model_validator(mode="after")
    def _validate_vectors(self) -> Self:
        names = tuple(name for name, _ in self.vectors)
        if not names or len(set(names)) != len(names):
            raise ValueError("Point named vectors 必须非空且唯一。")
        for _, vector in self.vectors:
            if not vector or any(not math.isfinite(item) for item in vector):
                raise ValueError("Point vector 必须非空且只含有限值。")
            if not any(item != 0.0 for item in vector):
                raise ValueError("Point vector 禁止全零。")
        return self

    def vector_map(self) -> dict[str, tuple[float, ...]]:
        """返回仅供 adapter 边界使用的可变映射副本。

        Args:
            无参数；读取当前 Point。

        Returns:
            named vector 到数值元组的映射。

        """
        return dict(self.vectors)


class VectorSearchResult(FrozenModel):
    """不泄漏 Qdrant 类型的向量检索命中。"""

    point_id: str
    chunk_id: str
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    role: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: StrictFloat
    rank: StrictInt = Field(gt=0)


class VectorRevisionValidation(FrozenModel):
    """从实际 Vector Store 回读得到的激活证据。"""

    point_count: StrictInt = Field(ge=0)
    vector_counts: tuple[tuple[str, StrictInt], ...]
    invalid_point_count: StrictInt = Field(ge=0)

    @field_validator("vector_counts", mode="before")
    @classmethod
    def _freeze_counts(cls, value: object) -> object:
        if isinstance(value, dict):
            return tuple(sorted(value.items()))
        return value


class VectorPointAudit(FrozenModel):
    """不暴露向量或正文的单个物理 Point 审计结果。"""

    point_id: str | None = None
    convertible: bool
    reason_code: str = Field(min_length=1)
    chunk_id: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    role: str | None = None
    section_id: str | None = None
    neighbor_group_id: str | None = None
    content_sha256: str | None = None
    vector_names: tuple[str, ...] = ()
    vector_dimensions: tuple[tuple[str, StrictInt], ...] = ()


class VectorRevisionInventory(FrozenModel):
    """一次完整物理遍历生成的安全 Vector inventory。"""

    raw_record_count: StrictInt = Field(ge=0)
    converted_record_count: StrictInt = Field(ge=0)
    invalid_record_count: StrictInt = Field(ge=0)
    points: tuple[VectorPointAudit, ...]
    inventory_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if len(self.points) != self.raw_record_count:
            raise ValueError("Vector inventory 必须逐条记录原始 Point。")
        if self.converted_record_count + self.invalid_record_count != (
            self.raw_record_count
        ):
            raise ValueError("Vector inventory 计数不守恒。")
        return self


class RevisionValidationEvidence(FrozenModel):
    """从 SQLite/FTS/Vector 实际读取的不可变激活证据。"""

    revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    document_count: StrictInt = Field(ge=0)
    chunk_count: StrictInt = Field(ge=0)
    fts_count: StrictInt = Field(ge=0)
    vector_counts: tuple[tuple[str, StrictInt], ...]
    report_checks: JsonObject
    deterministic_probe_passed: bool
    running_writer_count: StrictInt = Field(ge=0)
    vector_inventory_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @field_validator("vector_counts", mode="before")
    @classmethod
    def _freeze_vector_counts(cls, value: object) -> object:
        if isinstance(value, dict):
            return tuple(sorted(value.items()))
        return value

    @field_validator("report_checks", mode="before")
    @classmethod
    def _freeze_report_checks(cls, value: object) -> JsonObject:
        return freeze_json_object(value)


class GcPlan(FrozenModel):
    """绑定权威状态快照并需重算的 GC dry-run 计划。"""

    plan_id: str = Field(pattern=r"^gcplan_[0-9a-f]{32}$")
    database_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot: JsonObject
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("snapshot", mode="before")
    @classmethod
    def _freeze_snapshot(cls, value: object) -> JsonObject:
        return freeze_json_object(value)


_POINT_NAMESPACE = uuid.UUID("f7d70c36-6585-4ce1-9f5d-f67fb9d4238e")


def vector_point_id(index_revision_id: str, chunk_id: str) -> str:
    """由 revision 和 chunk 共同生成稳定 UUIDv5。

    Args:
        index_revision_id: 不可变 IndexRevision ID。
        chunk_id: canonical Chunk ID。

    Returns:
        跨 revision 不碰撞的 UUID 字符串。

    """
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{index_revision_id}:{chunk_id}"))


__all__ = [
    "ChunkEmbeddingState",
    "GcPlan",
    "NamedVectorPoint",
    "RevisionValidationEvidence",
    "RevisionVectorSpec",
    "VectorPointPayload",
    "VectorPointAudit",
    "VectorRevisionInventory",
    "VectorRevisionValidation",
    "VectorSearchResult",
    "vector_point_id",
]
