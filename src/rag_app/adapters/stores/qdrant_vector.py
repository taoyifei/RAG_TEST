"""Qdrant 1.18 不可变 revision 与完整 named-vector Point adapter。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.adapters.legacy.stores import InMemoryVectorStore
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import Conflict, IndexCompatibilityError
from rag_app.core.models import (
    EmbeddingSlotIdentity,
    IndexRevisionRef,
    NamedVectorPoint,
    RevisionVectorSpec,
    SearchHit,
    VectorPointPayload,
    VectorRevisionValidation,
    VectorSearchRequest,
    VectorSearchResult,
    VectorWriteRequest,
)


class _QdrantRecord(Protocol):
    """本地隔离第三方 Record 的最小读取面。"""

    id: object
    payload: object
    vector: object


class QdrantRevisionVectorStore:
    """每个 KB/Revision 使用独占 collection 的 Qdrant adapter。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.VECTOR_STORE,
        name="qdrant-local",
        version="qdrant-client-1.18.0",
        mode=ProviderMode.LOCAL,
    )

    def __init__(
        self,
        location: str | Path = ":memory:",
        *,
        client: QdrantClient | None = None,
    ) -> None:
        """构造 local-memory/local-path 或注入的显式客户端。

        Args:
            location: `:memory:` 或 P06 data root 内的本地路径。
            client: 测试或显式 remote 组合根注入的客户端。

        Returns:
            无返回值。

        """
        local_mode = "remote-injected"
        if client is not None:
            self._client = client
        elif str(location) == ":memory:":
            local_mode = "local-memory"
            self._client = QdrantClient(":memory:")
        else:
            local_mode = "local-path"
            path = Path(location)
            if path.exists() and path.is_symlink():
                raise ValueError("Qdrant local path 禁止 symlink。")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._client = QdrantClient(path=str(path))
        self.descriptor = QdrantRevisionVectorStore.descriptor.model_copy(
            update={
                "version": f"qdrant-client-1.18.0:{local_mode}",
            }
        )
        self._specs: dict[str, RevisionVectorSpec] = {}
        self._legacy = InMemoryVectorStore()
        self._closed = False

    def create_revision(self, spec: RevisionVectorSpec) -> None:
        """幂等创建固定 named-vector schema。

        Args:
            spec: revision 与 required vector schema。

        Returns:
            无返回值。

        Raises:
            Conflict: 已有 collection schema 不兼容。

        """
        name = spec.physical_namespace
        configured = {
            slot.vector_name: models.VectorParams(
                size=slot.dimension,
                distance=models.Distance.COSINE,
            )
            for slot in spec.slots
        }
        if self._client.collection_exists(name):
            self._validate_collection_schema(spec)
        else:
            self._client.create_collection(
                collection_name=name,
                vectors_config=configured,
            )
        existing = self._specs.get(name)
        if existing is not None and existing != spec:
            raise Conflict(
                "Qdrant collection 已绑定不同 revision。", stage="qdrant.create"
            )
        self._specs[name] = spec

    def upsert_complete_points(
        self,
        spec: RevisionVectorSpec,
        points: tuple[NamedVectorPoint, ...],
    ) -> None:
        """一次 upsert 每个 Point 的全部 required vectors。

        Args:
            spec: 目标不可变 schema。
            points: 完整 Point 序列。

        Returns:
            无返回值。

        """
        self._require_spec(spec)
        expected = {slot.vector_name: slot.dimension for slot in spec.slots}
        qdrant_points: list[models.PointStruct] = []
        for point in points:
            vectors = point.vector_map()
            self._validate_point_identity(spec, point)
            if set(vectors) != set(expected):
                raise IndexCompatibilityError(
                    "Qdrant Point 必须一次包含全部 required vectors。",
                    stage="qdrant.upsert",
                )
            if any(
                len(vectors[name]) != dimension
                for name, dimension in expected.items()
            ):
                raise IndexCompatibilityError(
                    "Qdrant vector 维度不匹配。", stage="qdrant.upsert"
                )
            qdrant_points.append(
                models.PointStruct(
                    id=point.point_id,
                    vector={
                        name: list(vector) for name, vector in vectors.items()
                    },
                    payload=point.payload.model_dump(mode="json"),
                )
            )
        if qdrant_points:
            self._client.upsert(
                collection_name=spec.physical_namespace,
                points=qdrant_points,
                wait=True,
            )

    def fetch_points(
        self,
        spec: RevisionVectorSpec,
        point_ids: tuple[str, ...],
    ) -> tuple[NamedVectorPoint, ...]:
        """回读 payload 与全部 named vectors。

        Args:
            spec: 目标 revision schema。
            point_ids: 稳定 UUIDv5 IDs。

        Returns:
            Qdrant 实际存在的完整 Point。

        """
        self._require_spec(spec)
        records = self._client.retrieve(
            collection_name=spec.physical_namespace,
            ids=list(point_ids),
            with_payload=True,
            with_vectors=True,
        )
        converted = {
            str(record.id): self._record_to_point(spec, record)
            for record in records
        }
        return tuple(
            converted[point_id]
            for point_id in point_ids
            if point_id in converted
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
        """仅查询 slot 对应的 named vector 并硬过滤 scope。

        Args:
            spec: 目标 revision schema。
            slot_id: 目标 slot。
            vector_name: 必须属于 slot 的 vector name。
            query_vector: 同维度查询向量。
            limit: 最大命中数。

        Returns:
            Qdrant 分数降序命中。

        """
        self._require_spec(spec)
        slot = self._slot(spec, slot_id, vector_name)
        if len(query_vector) != slot.dimension:
            raise IndexCompatibilityError(
                "Qdrant query 维度不匹配。", stage="qdrant.search"
            )
        revision = spec.revision
        response = self._client.query_points(
            collection_name=spec.physical_namespace,
            query=list(query_vector),
            using=vector_name,
            query_filter=models.Filter(
                must=[
                    _match("project_id", revision.project_id),
                    _match("knowledge_base_id", revision.knowledge_base_id),
                    _match("index_revision_id", revision.index_revision_id),
                ]
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        results = []
        for rank, point in enumerate(response.points, start=1):
            payload = VectorPointPayload.model_validate(point.payload or {})
            if payload.index_revision_id != revision.index_revision_id:
                raise IndexCompatibilityError(
                    "Qdrant payload revision 不匹配。", stage="qdrant.search"
                )
            results.append(
                VectorSearchResult(
                    point_id=str(point.id),
                    chunk_id=payload.chunk_id,
                    score=float(point.score),
                    rank=rank,
                )
            )
        return tuple(results)

    def count_vectors(self, spec: RevisionVectorSpec, vector_name: str) -> int:
        """从实际 Point 回读统计 named vector 数。

        Args:
            spec: 目标 revision schema。
            vector_name: 必须属于 schema 的 vector name。

        Returns:
            实际有效向量数量。

        """
        self._require_spec(spec)
        if vector_name not in {slot.vector_name for slot in spec.slots}:
            raise IndexCompatibilityError(
                "vector name 不属于 revision。", stage="qdrant.count"
            )
        return sum(
            vector_name in point.vector_map()
            for point in self._all_points(spec)
        )

    def validate_vector_revision(
        self,
        spec: RevisionVectorSpec,
    ) -> VectorRevisionValidation:
        """回读全部 Point 并验证 payload、完整性和维度。

        Args:
            spec: 目标 revision schema。

        Returns:
            实际 Point/vector 计数。

        """
        self._require_spec(spec)
        counts = {slot.vector_name: 0 for slot in spec.slots}
        invalid = 0
        points = self._all_points(spec, tolerate_invalid=True)
        for point in points:
            try:
                self._validate_point_identity(spec, point)
                vectors = point.vector_map()
                if set(vectors) != set(counts):
                    raise ValueError("missing vector")
                for slot in spec.slots:
                    if len(vectors[slot.vector_name]) != slot.dimension:
                        raise ValueError("bad dimension")
                    counts[slot.vector_name] += 1
            except (ValueError, IndexCompatibilityError):
                invalid += 1
        return VectorRevisionValidation(
            point_count=len(points),
            vector_counts=tuple(sorted(counts.items())),
            invalid_point_count=invalid,
        )

    def delete_revision(self, spec: RevisionVectorSpec) -> None:
        """删除整个 revision collection。

        Args:
            spec: 已由 GC Plan 授权的 revision schema。

        Returns:
            无返回值。

        """
        self._require_spec(spec)
        self._client.delete_collection(spec.physical_namespace)
        self._specs.pop(spec.physical_namespace, None)

    def write(self, request: VectorWriteRequest) -> None:
        """保留 P01-P05 slot-specific Memory 兼容路径。

        Args:
            request: 旧 VectorWriteRequest。

        Returns:
            无返回值。

        """
        self._legacy.write(request)

    def search(self, request: VectorSearchRequest) -> tuple[SearchHit, ...]:
        """保留 P01-P05 Memory 查询兼容路径。

        Args:
            request: 旧 VectorSearchRequest。

        Returns:
            旧 Core SearchHit。

        """
        return self._legacy.search(request)

    def validate_revision(self, revision: IndexRevisionRef) -> None:
        """验证 canonical 或旧 Memory revision 存在。

        Args:
            revision: 目标 revision。

        Returns:
            无返回值。

        """
        if any(spec.revision == revision for spec in self._specs.values()):
            return
        self._legacy.validate_revision(revision)

    def close(self) -> None:
        """幂等关闭 Qdrant 与兼容 Store。

        Args:
            无参数；关闭当前客户端。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._legacy.close()
        self._client.close()

    def _require_spec(self, spec: RevisionVectorSpec) -> None:
        if not self._client.collection_exists(spec.physical_namespace):
            raise IndexCompatibilityError(
                "Qdrant revision collection 不存在。", stage="qdrant.schema"
            )
        existing = self._specs.get(spec.physical_namespace)
        if existing is not None and existing != spec:
            raise IndexCompatibilityError(
                "Qdrant revision schema 不匹配。", stage="qdrant.schema"
            )
        self._validate_collection_schema(spec)
        self._specs[spec.physical_namespace] = spec

    def _validate_collection_schema(self, spec: RevisionVectorSpec) -> None:
        information = self._client.get_collection(spec.physical_namespace)
        vectors = information.config.params.vectors
        if not isinstance(vectors, Mapping):
            raise Conflict(
                "Qdrant collection 不是 named-vector schema。",
                stage="qdrant.schema",
            )
        observed = {name: int(params.size) for name, params in vectors.items()}
        expected = {slot.vector_name: slot.dimension for slot in spec.slots}
        if observed != expected:
            raise Conflict(
                "Qdrant collection schema 与 revision 不一致。",
                stage="qdrant.schema",
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
                "slot 不属于 revision。", stage="qdrant.search"
            ) from None
        if slot.vector_name != vector_name:
            raise IndexCompatibilityError(
                "slot/vector name 交叉被拒绝。", stage="qdrant.search"
            )
        return slot

    def _all_points(
        self,
        spec: RevisionVectorSpec,
        *,
        tolerate_invalid: bool = False,
    ) -> tuple[NamedVectorPoint, ...]:
        records, offset = self._client.scroll(
            collection_name=spec.physical_namespace,
            limit=256,
            with_payload=True,
            with_vectors=True,
        )
        all_records = list(records)
        while offset is not None:
            records, offset = self._client.scroll(
                collection_name=spec.physical_namespace,
                offset=offset,
                limit=256,
                with_payload=True,
                with_vectors=True,
            )
            all_records.extend(records)
        points = []
        for record in all_records:
            try:
                points.append(self._record_to_point(spec, record))
            except (ValueError, IndexCompatibilityError):
                if not tolerate_invalid:
                    raise
        return tuple(points)

    def _record_to_point(
        self, spec: RevisionVectorSpec, record: object
    ) -> NamedVectorPoint:
        typed_record = cast(_QdrantRecord, record)
        payload = typed_record.payload or {}
        raw_vectors = typed_record.vector
        if not isinstance(raw_vectors, Mapping):
            raise IndexCompatibilityError(
                "Qdrant Point 未回读 named vectors。", stage="qdrant.fetch"
            )
        vectors = {
            str(name): tuple(
                float(value) for value in cast(list[float], vector)
            )
            for name, vector in raw_vectors.items()
        }
        point = NamedVectorPoint(
            point_id=str(typed_record.id),
            payload=VectorPointPayload.model_validate(payload),
            vectors=tuple(sorted(vectors.items())),
        )
        self._validate_point_identity(spec, point)
        return point

    def _validate_point_identity(
        self, spec: RevisionVectorSpec, point: NamedVectorPoint
    ) -> None:
        payload = point.payload
        revision = spec.revision
        if (
            payload.project_id != revision.project_id
            or payload.knowledge_base_id != revision.knowledge_base_id
            or payload.index_revision_id != revision.index_revision_id
        ):
            raise IndexCompatibilityError(
                "Qdrant Point payload scope 不匹配。", stage="qdrant.point"
            )


def _match(key: str, value: str) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


__all__ = ["QdrantRevisionVectorStore"]
