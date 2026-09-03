"""不可变 revision 的离线 Memory named-vector Store。"""

from __future__ import annotations

import math

from rag_app.adapters.legacy.stores import InMemoryVectorStore
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import Conflict, IndexCompatibilityError
from rag_app.core.models import (
    EmbeddingSlotIdentity,
    NamedVectorPoint,
    RevisionVectorSpec,
    VectorRevisionValidation,
    VectorSearchResult,
)


class MemoryRevisionVectorStore(InMemoryVectorStore):
    """保留旧 write/search，并增加完整 Point 写入合同。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.VECTOR_STORE,
        name="memory-vector",
        version="2",
        mode=ProviderMode.DETERMINISTIC,
    )

    def __init__(self) -> None:
        """创建空的 revision namespace 映射。

        Args:
            无参数；不创建外部资源。

        Returns:
            无返回值。

        """
        super().__init__()
        self._specs: dict[str, RevisionVectorSpec] = {}
        self._points: dict[str, dict[str, NamedVectorPoint]] = {}

    def create_revision(self, spec: RevisionVectorSpec) -> None:
        """幂等创建不可变 schema。

        Args:
            spec: revision 与全部 named vectors。

        Returns:
            无返回值。

        Raises:
            Conflict: namespace 已绑定不同 schema。

        """
        key = spec.physical_namespace
        existing = self._specs.get(key)
        if existing is not None and existing != spec:
            raise Conflict(
                "Vector namespace 已绑定不同 revision。", stage="vector.create"
            )
        self._specs[key] = spec
        self._points.setdefault(key, {})

    def upsert_complete_points(
        self,
        spec: RevisionVectorSpec,
        points: tuple[NamedVectorPoint, ...],
    ) -> None:
        """校验完整向量集合后幂等写入 Point。

        Args:
            spec: 目标不可变 schema。
            points: 每项都含全部 required vectors 的 Point。

        Returns:
            无返回值。

        """
        self._require_spec(spec)
        expected_names = {slot.vector_name for slot in spec.slots}
        namespace = self._points[spec.physical_namespace]
        for point in points:
            self._validate_point(spec, point, expected_names)
            existing = namespace.get(point.point_id)
            if existing is not None and existing != point:
                raise Conflict(
                    "不可变 Point 已存在不同内容。", stage="vector.upsert"
                )
            namespace[point.point_id] = point

    def fetch_points(
        self,
        spec: RevisionVectorSpec,
        point_ids: tuple[str, ...],
    ) -> tuple[NamedVectorPoint, ...]:
        """按输入顺序回读完整 Point。

        Args:
            spec: 目标 revision schema。
            point_ids: 稳定 UUIDv5 IDs。

        Returns:
            实际存在的 Point。

        """
        self._require_spec(spec)
        points = self._points[spec.physical_namespace]
        return tuple(
            points[point_id] for point_id in point_ids if point_id in points
        )

    def search_named(
        self,
        spec: RevisionVectorSpec,
        *,
        slot_id: str,
        vector_name: str,
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[VectorSearchResult, ...]:
        """在严格 slot/vector 空间内执行余弦查询。

        Args:
            spec: 目标 revision schema。
            slot_id: 目标 slot。
            vector_name: 必须属于该 slot 的向量名。
            query_vector: 同维度查询向量。
            limit: 最大命中数。

        Returns:
            稳定排序的向量命中。

        """
        self._require_spec(spec)
        slot = self._slot(spec, slot_id, vector_name)
        if len(query_vector) != slot.dimension:
            raise IndexCompatibilityError(
                "查询向量维度不匹配。", stage="vector.search"
            )
        scored = []
        for point in self._points[spec.physical_namespace].values():
            vector = point.vector_map()[vector_name]
            scored.append((point, _cosine(query_vector, vector)))
        scored.sort(key=lambda item: (-item[1], item[0].point_id))
        return tuple(
            VectorSearchResult(
                point_id=point.point_id,
                chunk_id=point.payload.chunk_id,
                score=score,
                rank=rank,
            )
            for rank, (point, score) in enumerate(scored[:limit], start=1)
        )

    def count_vectors(self, spec: RevisionVectorSpec, vector_name: str) -> int:
        """统计实际包含指定 named vector 的 Point。

        Args:
            spec: 目标 revision schema。
            vector_name: 必须属于 schema 的向量名。

        Returns:
            有效向量数量。

        """
        self._require_spec(spec)
        if vector_name not in {slot.vector_name for slot in spec.slots}:
            raise IndexCompatibilityError(
                "vector name 不属于 revision。", stage="vector.count"
            )
        return sum(
            vector_name in point.vector_map()
            for point in self._points[spec.physical_namespace].values()
        )

    def validate_vector_revision(
        self,
        spec: RevisionVectorSpec,
    ) -> VectorRevisionValidation:
        """回读全部 Point 并统计实际有效 named vectors。

        Args:
            spec: 目标 revision schema。

        Returns:
            实际 Point/vector 计数和无效数。

        """
        self._require_spec(spec)
        counts = {slot.vector_name: 0 for slot in spec.slots}
        invalid = 0
        expected = set(counts)
        for point in self._points[spec.physical_namespace].values():
            try:
                self._validate_point(spec, point, expected)
            except (ValueError, IndexCompatibilityError):
                invalid += 1
                continue
            for name in counts:
                counts[name] += 1
        return VectorRevisionValidation(
            point_count=len(self._points[spec.physical_namespace]),
            vector_counts=tuple(sorted(counts.items())),
            invalid_point_count=invalid,
        )

    def delete_revision(self, spec: RevisionVectorSpec) -> None:
        """删除整个不可变 namespace。

        Args:
            spec: 已由 GC Plan 授权的 revision schema。

        Returns:
            无返回值。

        """
        self._require_spec(spec)
        self._points.pop(spec.physical_namespace, None)
        self._specs.pop(spec.physical_namespace, None)

    def close(self) -> None:
        """幂等释放 legacy 与 P06 Memory 数据。

        Args:
            无参数；关闭当前 Store。

        Returns:
            无返回值。

        """
        if getattr(self, "_closed", False):
            return
        self._points.clear()
        self._specs.clear()
        super().close()

    def _require_spec(self, spec: RevisionVectorSpec) -> None:
        if self._specs.get(spec.physical_namespace) != spec:
            raise IndexCompatibilityError(
                "Vector revision schema 不匹配。", stage="vector.schema"
            )

    def _slot(
        self,
        spec: RevisionVectorSpec,
        slot_id: str,
        vector_name: str,
    ) -> EmbeddingSlotIdentity:
        try:
            slot = spec.slot(slot_id)
        except KeyError:
            raise IndexCompatibilityError(
                "slot 不属于 revision。", stage="vector.search"
            ) from None
        if slot.vector_name != vector_name:
            raise IndexCompatibilityError(
                "slot/vector name 交叉被拒绝。", stage="vector.search"
            )
        return slot

    def _validate_point(
        self,
        spec: RevisionVectorSpec,
        point: NamedVectorPoint,
        expected_names: set[str],
    ) -> None:
        payload = point.payload
        if (
            payload.project_id != spec.revision.project_id
            or payload.knowledge_base_id != spec.revision.knowledge_base_id
            or payload.index_revision_id != spec.revision.index_revision_id
        ):
            raise IndexCompatibilityError(
                "Point payload scope 不匹配。", stage="vector.point"
            )
        vectors = point.vector_map()
        if set(vectors) != expected_names:
            raise IndexCompatibilityError(
                "Point 缺少 required named vector。", stage="vector.point"
            )
        for slot in spec.slots:
            if len(vectors[slot.vector_name]) != slot.dimension:
                raise IndexCompatibilityError(
                    "Point vector 维度不匹配。", stage="vector.point"
                )


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


__all__ = ["MemoryRevisionVectorStore"]
