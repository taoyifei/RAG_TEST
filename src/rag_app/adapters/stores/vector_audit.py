"""Memory 与 Qdrant 共用的安全 Vector Point 审计逻辑。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    NamedVectorPoint,
    RevisionVectorSpec,
    VectorPointAudit,
    VectorRevisionInventory,
    vector_point_id,
)


def audit_named_point(
    spec: RevisionVectorSpec,
    point: object,
) -> VectorPointAudit:
    """按完整 revision 合同审计已转换 Point。

    Args:
        spec: 目标 revision schema。
        point: 已转换但仍不可信的 Point 对象。

    Returns:
        不含向量值的审计项。

    """
    raw_point = (
        point.model_dump(mode="python")
        if isinstance(point, NamedVectorPoint)
        else point
    )
    try:
        validated = NamedVectorPoint.model_validate(raw_point)
    except (TypeError, ValidationError, ValueError):
        return VectorPointAudit(
            convertible=False,
            reason_code="POINT_MODEL_INVALID",
        )
    payload = validated.payload
    dimensions = tuple(
        sorted((name, len(vector)) for name, vector in validated.vectors)
    )
    base = {
        "point_id": _canonical_uuid(validated.point_id),
        "chunk_id": payload.chunk_id,
        "document_id": payload.document_id,
        "document_version_id": payload.document_version_id,
        "role": payload.role,
        "section_id": payload.section_id,
        "neighbor_group_id": payload.neighbor_group_id,
        "content_sha256": payload.content_sha256,
        "vector_names": tuple(name for name, _ in dimensions),
        "vector_dimensions": dimensions,
    }
    reason = _point_reason(spec, validated)
    return VectorPointAudit.model_validate(
        {
            **base,
            "convertible": reason == "OK",
            "reason_code": reason,
        }
    )


def inventory_from_audits(
    points: Sequence[VectorPointAudit],
) -> VectorRevisionInventory:
    """从逐条审计项构造计数守恒且可哈希的 inventory。

    Args:
        points: 与物理遍历顺序一致的审计项。

    Returns:
        稳定排序和哈希后的 revision inventory。

    """
    ordered = tuple(
        sorted(
            points,
            key=lambda item: (
                item.point_id or "",
                item.chunk_id or "",
                item.reason_code,
            ),
        )
    )
    converted = sum(item.convertible for item in ordered)
    inventory_hash = canonical_sha256(
        tuple(item.model_dump(mode="json") for item in ordered)
    )
    return VectorRevisionInventory(
        raw_record_count=len(ordered),
        converted_record_count=converted,
        invalid_record_count=len(ordered) - converted,
        points=ordered,
        inventory_hash=inventory_hash,
    )


def safe_vector_shape(raw_vectors: object) -> tuple[
    tuple[str, ...], tuple[tuple[str, int], ...]
]:
    """只提取 named-vector 名称和维度，不保留数值。

    Args:
        raw_vectors: Qdrant 返回的未知 vector 字段。

    Returns:
        可安全记录的名称与维度；结构无效时返回空元组。

    """
    if not isinstance(raw_vectors, Mapping):
        return (), ()
    dimensions: list[tuple[str, int]] = []
    for raw_name, raw_vector in raw_vectors.items():
        if not isinstance(raw_name, str):
            return (), ()
        if not isinstance(raw_vector, Sequence) or isinstance(
            raw_vector, (str, bytes, bytearray)
        ):
            return (), ()
        dimensions.append((raw_name, len(raw_vector)))
    ordered = tuple(sorted(dimensions))
    return tuple(name for name, _ in ordered), ordered


def _point_reason(spec: RevisionVectorSpec, point: NamedVectorPoint) -> str:
    payload = point.payload
    revision = spec.revision
    if _canonical_uuid(point.point_id) is None:
        return "INVALID_POINT_ID"
    if point.point_id != vector_point_id(
        revision.index_revision_id, payload.chunk_id
    ):
        return "POINT_ID_CHUNK_MISMATCH"
    if (
        payload.project_id != revision.project_id
        or payload.knowledge_base_id != revision.knowledge_base_id
        or payload.index_revision_id != revision.index_revision_id
    ):
        return "PAYLOAD_SCOPE_MISMATCH"
    expected = {slot.vector_name: slot.dimension for slot in spec.slots}
    observed = {name: len(vector) for name, vector in point.vectors}
    if set(observed) != set(expected):
        return "VECTOR_NAMES_MISMATCH"
    if observed != expected:
        return "VECTOR_DIMENSION_MISMATCH"
    return "OK"


def _canonical_uuid(value: object) -> str | None:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if str(value) == canonical else None


__all__ = [
    "audit_named_point",
    "inventory_from_audits",
    "safe_vector_shape",
]
