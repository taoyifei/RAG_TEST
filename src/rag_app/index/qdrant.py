"""Qdrant dense+sparse collection 与版本激活。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import Chunk

__all__ = ["IndexedChunk", "QdrantIndex"]

_DENSE_VECTOR_NAME = "dense"
_SPARSE_VECTOR_NAME = "bm25"
_VERSION_STAGING = "staging"
_VERSION_ACTIVE = "active"
_VERSION_RETIRED = "retired"
_SHA256_HEX_LENGTH = 64
_PAYLOAD_SCHEMA_VERSION = "2"
_STAGING_JOB_KEY = "staging_control_job_id"
_BASE_MANIFEST_KEY = "staging_base_manifest_sha256"
_BASE_ACTIVE_COUNT_KEY = "staging_base_active_count"


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """完成 dense 与 sparse 编码的一个分块。"""

    chunk: Chunk
    dense: list[float]
    sparse: models.SparseVector | models.Document


class QdrantIndex:
    """管理一个 pipeline 专属 Qdrant collection。"""

    def __init__(
        self,
        client: QdrantClient,
        *,
        collection_name: str,
        dense_dimension: int,
        pipeline_fingerprint: str,
    ) -> None:
        """保存 collection 契约。

        Args:
            client: 已配置 API key 与超时的 Qdrant 客户端。
            collection_name: pipeline 专属物理 collection 名。
            dense_dimension: embedding 向量维度。
            pipeline_fingerprint: collection 对应的 pipeline 指纹。

        Returns:
            无返回值。

        """
        if dense_dimension <= 0:
            raise ValueError("dense_dimension 必须为正数。")
        self._client = client
        self.collection_name = collection_name
        self._dense_dimension = dense_dimension
        self._pipeline_fingerprint = pipeline_fingerprint

    @property
    def pipeline_fingerprint(self) -> str:
        """返回该物理 collection 的 pipeline 指纹。

        Args:
            无参数；读取构造时冻结的指纹。

        Returns:
            当前物理 collection 的 pipeline 指纹。

        """
        return self._pipeline_fingerprint

    def create_collection(self) -> None:
        """创建或严格校验 dense+sparse collection。

        Args:
            无参数。

        Returns:
            无返回值。

        Raises:
            ValueError: 现有 collection 与 pipeline 或维度不兼容。

        """
        if self._client.collection_exists(self.collection_name):
            self._validate_existing_collection()
            return
        self._client.create_collection(
            collection_name=self.collection_name,
            vectors_config={
                _DENSE_VECTOR_NAME: models.VectorParams(
                    size=self._dense_dimension,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                _SPARSE_VECTOR_NAME: models.SparseVectorParams(
                    modifier=models.Modifier.IDF,
                )
            },
            on_disk_payload=True,
            metadata={
                "pipeline_fingerprint": self._pipeline_fingerprint,
                "schema_version": "1",
                "payload_schema_version": _PAYLOAD_SCHEMA_VERSION,
            },
        )
        for field_name in (
            "chunk_id",
            "source_id",
            "source_path",
            "doc_version",
            "version_state",
            "document_status",
            "authority_level",
            "element_kind",
            "section_id",
            "neighbor_group_id",
            "chunk_role",
        ):
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )
        for field_name in ("effective_from", "effective_to"):
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.DATETIME,
                wait=True,
            )

    def prepare_staging_collection(
        self,
        *,
        control_job_id: str,
        base_manifest_sha256: str | None,
    ) -> None:
        """创建或恢复同一 full job 的空 staging collection。

        Args:
            control_job_id: 创建 target 的 control job。
            base_manifest_sha256: 发布前活动 manifest；首发时为 None。

        Returns:
            无返回值。

        Raises:
            ValueError: 既有 collection 不属于同一 job、pipeline 或基线。

        """
        if self._client.collection_exists(self.collection_name):
            self._validate_existing_collection()
            self.require_staging_identity(
                control_job_id=control_job_id,
                base_manifest_sha256=base_manifest_sha256,
            )
            return
        self.create_collection()
        self._set_staging_identity(
            control_job_id=control_job_id,
            base_manifest_sha256=base_manifest_sha256,
            base_active_count=0,
        )

    def clone_registered_snapshot(
        self,
        *,
        source_collection_name: str,
        snapshot_name: str,
        checksum: str,
        control_job_id: str,
        base_manifest_sha256: str,
    ) -> None:
        """从已登记活动 snapshot 克隆新的物理 collection。

        Args:
            source_collection_name: snapshot 所属活动 collection。
            snapshot_name: manifest 登记的安全 snapshot 文件名。
            checksum: manifest 登记的 snapshot SHA256。
            control_job_id: 创建 target 的 control job。
            base_manifest_sha256: 冻结活动 manifest 摘要。

        Returns:
            无返回值。

        Raises:
            ValueError: snapshot、collection、schema 或 staging 身份不一致。
            RuntimeError: Qdrant 未确认恢复，或恢复后活动点数不一致。

        """
        _validate_snapshot_identity(snapshot_name, checksum)
        _require_collection_name(source_collection_name)
        if source_collection_name == self.collection_name:
            raise ValueError("增量 target collection 不能是活动 collection。")
        if self._client.collection_exists(self.collection_name):
            self._validate_existing_collection()
            self.require_staging_identity(
                control_job_id=control_job_id,
                base_manifest_sha256=base_manifest_sha256,
            )
            return
        if not self._client.collection_exists(source_collection_name):
            raise ValueError("活动 source collection 不存在。")
        self._validate_existing_collection(source_collection_name)
        self.require_registered_snapshot(
            collection_name=source_collection_name,
            snapshot_name=snapshot_name,
            checksum=checksum,
        )
        source_active_count = self.count_active_exact(
            source_collection_name
        )
        location = (
            "file:///qdrant/snapshots/"
            f"{source_collection_name}/{snapshot_name}"
        )
        recovered = self._client.recover_snapshot(
            collection_name=self.collection_name,
            location=location,
            checksum=checksum,
            priority=models.SnapshotPriority.SNAPSHOT,
            wait=True,
        )
        if recovered is not True:
            raise RuntimeError("Qdrant 未确认 snapshot clone 成功。")
        self._validate_existing_collection()
        target_active_count = self.count_active_exact()
        if target_active_count != source_active_count:
            raise RuntimeError("snapshot clone 前后活动点精确计数不一致。")
        self._set_staging_identity(
            control_job_id=control_job_id,
            base_manifest_sha256=base_manifest_sha256,
            base_active_count=source_active_count,
        )

    def require_registered_snapshot(
        self,
        *,
        collection_name: str,
        snapshot_name: str,
        checksum: str,
    ) -> None:
        """要求 manifest snapshot 仍在指定 collection 精确登记。

        Args:
            collection_name: snapshot 所属物理 collection。
            snapshot_name: manifest 冻结的 snapshot 文件名。
            checksum: manifest 冻结的 snapshot SHA256。

        Returns:
            无返回值。

        Raises:
            ValueError: collection 不兼容，或 snapshot 身份不唯一、不匹配。

        """
        _require_collection_name(collection_name)
        _validate_snapshot_identity(snapshot_name, checksum)
        if not self._client.collection_exists(collection_name):
            raise ValueError("活动 source collection 不存在。")
        self._validate_existing_collection(collection_name)
        snapshots = [
            snapshot
            for snapshot in self._client.list_snapshots(collection_name)
            if snapshot.name == snapshot_name
        ]
        if (
            len(snapshots) != 1
            or snapshots[0].checksum is None
            or snapshots[0].checksum != checksum
        ):
            raise ValueError("活动 manifest snapshot 未在 Qdrant 精确登记。")

    def require_staging_identity(
        self,
        *,
        control_job_id: str,
        base_manifest_sha256: str | None,
    ) -> None:
        """要求既有 target collection 属于同一 job 和基线。

        Args:
            control_job_id: 预期 control job。
            base_manifest_sha256: 预期活动 manifest；首发时为 None。

        Returns:
            无返回值。

        Raises:
            ValueError: metadata 身份缺失或不一致。

        """
        actual_base = self.staging_base_manifest_sha256(
            control_job_id=control_job_id
        )
        if actual_base != base_manifest_sha256:
            raise ValueError("target collection staging 身份不一致。")

    def staging_base_manifest_sha256(
        self,
        *,
        control_job_id: str,
    ) -> str | None:
        """读取同一 control job 绑定的 base manifest 摘要。

        Args:
            control_job_id: 预期创建 target 的 control job。

        Returns:
            增量基线摘要；full 首发 target 返回 None。

        Raises:
            ValueError: staging metadata 缺失、格式无效或 job 不一致。

        """
        metadata = (
            self._client.get_collection(self.collection_name).config.metadata
            or {}
        )
        raw_base = metadata.get(_BASE_MANIFEST_KEY)
        if (
            metadata.get(_STAGING_JOB_KEY) != control_job_id
            or not isinstance(raw_base, str)
            or not isinstance(metadata.get(_BASE_ACTIVE_COUNT_KEY), int)
        ):
            raise ValueError("target collection staging 身份不一致。")
        if not raw_base:
            return None
        _validate_sha256(raw_base, "base manifest")
        return raw_base

    def count_active_exact(
        self,
        collection_name: str | None = None,
    ) -> int:
        """精确统计物理 collection 的全部活动点。

        Args:
            collection_name: 可选 collection；默认使用当前 target。

        Returns:
            `version_state=active` 的精确点数。

        """
        return self._client.count(
            collection_name or self.collection_name,
            count_filter=models.Filter(
                must=[_match("version_state", _VERSION_ACTIVE)]
            ),
            exact=True,
        ).count

    def fetch_active_payloads(
        self,
        chunk_ids: tuple[str, ...],
    ) -> dict[str, dict[str, object]]:
        """按逻辑 chunk ID 批量读取活动 payload。

        Args:
            chunk_ids: 去重前的逻辑 chunk ID。

        Returns:
            仅含活动版本、以 chunk ID 为键的完整 payload。

        Raises:
            ValueError: 返回 payload 缺少或重复 chunk ID。

        """
        unique_ids = tuple(dict.fromkeys(chunk_ids))
        if not unique_ids:
            return {}
        records, _ = self._client.scroll(
            collection_name=self.collection_name,
            scroll_filter=models.Filter(
                must=[
                    _match("version_state", _VERSION_ACTIVE),
                    models.FieldCondition(
                        key="chunk_id",
                        match=models.MatchAny(any=list(unique_ids)),
                    ),
                ]
            ),
            limit=len(unique_ids),
            with_payload=True,
            with_vectors=False,
        )
        payloads: dict[str, dict[str, object]] = {}
        for record in records:
            if record.payload is None:
                raise ValueError("相邻 chunk 缺少 payload。")
            payload = {
                str(key): value for key, value in record.payload.items()
            }
            chunk_id = payload.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError("相邻 chunk payload 缺少 chunk_id。")
            if chunk_id in payloads:
                raise ValueError("活动索引含重复逻辑 chunk ID。")
            payloads[chunk_id] = payload
        return payloads

    def stage_chunks(self, chunks: Sequence[IndexedChunk]) -> None:
        """把完整编码的 chunk 幂等写为 staging。

        Args:
            chunks: dense、sparse 与 payload 已完成的分块。

        Returns:
            无返回值。

        Raises:
            ValueError: dense 维度或 pipeline 指纹不兼容。

        """
        points: list[models.PointStruct] = []
        for indexed in chunks:
            if len(indexed.dense) != self._dense_dimension:
                raise ValueError("dense 向量维度与 collection 不一致。")
            if (
                indexed.chunk.pipeline_fingerprint
                != self._pipeline_fingerprint
            ):
                raise ValueError("chunk pipeline 指纹与 collection 不一致。")
            points.append(
                models.PointStruct(
                    id=_point_id(
                        indexed.chunk.doc_version,
                        indexed.chunk.chunk_id,
                    ),
                    vector={
                        _DENSE_VECTOR_NAME: indexed.dense,
                        _SPARSE_VECTOR_NAME: indexed.sparse,
                    },
                    payload=_chunk_payload(
                        indexed.chunk,
                        version_state=_VERSION_STAGING,
                    ),
                )
            )
        if points:
            self._client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True,
            )

    def count_version(
        self,
        source_id: str,
        doc_version: str,
        version_state: str,
    ) -> int:
        """精确统计一个来源版本的点数。

        Args:
            source_id: 持久来源标识。
            doc_version: 内容版本。
            version_state: staging、active 或 retired。

        Returns:
            精确点数。

        """
        result = self._client.count(
            collection_name=self.collection_name,
            count_filter=_version_filter(
                source_id,
                doc_version,
                version_state,
            ),
            exact=True,
        )
        return result.count

    def activate_source_version(
        self,
        source_id: str,
        doc_version: str,
    ) -> None:
        """先启新再停旧，失败时恢复单一旧活动版本。

        Args:
            source_id: 持久来源标识。
            doc_version: 已完整写入 staging 的内容版本。

        Returns:
            无返回值。

        Raises:
            LookupError: 新版本没有 staging 点。

        """
        staging_filter = _version_filter(
            source_id,
            doc_version,
            _VERSION_STAGING,
        )
        staging_count = self._client.count(
            self.collection_name,
            count_filter=staging_filter,
            exact=True,
        ).count
        active_filter = _version_filter(
            source_id,
            doc_version,
            _VERSION_ACTIVE,
        )
        active_count = self._client.count(
            self.collection_name,
            count_filter=active_filter,
            exact=True,
        ).count
        if staging_count == 0 and active_count == 0:
            raise LookupError("新版本没有 staging 或 active 点。")
        old_filter = models.Filter(
            must=[
                _match("source_id", source_id),
                _match("version_state", _VERSION_ACTIVE),
            ],
            must_not=[_match("doc_version", doc_version)],
        )
        promoted_from_staging = active_count == 0
        try:
            if staging_count > 0:
                self._client.set_payload(
                    collection_name=self.collection_name,
                    payload={"version_state": _VERSION_ACTIVE},
                    points=staging_filter,
                    wait=True,
                )
            self._client.set_payload(
                collection_name=self.collection_name,
                payload={"version_state": _VERSION_RETIRED},
                points=old_filter,
                wait=True,
            )
        except Exception:
            if promoted_from_staging:
                self._client.set_payload(
                    collection_name=self.collection_name,
                    payload={"version_state": _VERSION_STAGING},
                    points=active_filter,
                    wait=True,
                )
            raise

    def retire_source(self, source_id: str) -> None:
        """停用一个已删除来源的全部活动点。

        Args:
            source_id: 持久来源标识。

        Returns:
            无返回值。

        """
        self._client.set_payload(
            collection_name=self.collection_name,
            payload={"version_state": _VERSION_RETIRED},
            points=models.Filter(
                must=[
                    _match("source_id", source_id),
                    _match("version_state", _VERSION_ACTIVE),
                ]
            ),
            wait=True,
        )

    def rename_source(self, source_id: str, new_path: str) -> None:
        """幂等更新活动证据中的展示路径与 locator。

        Args:
            source_id: 持久来源标识。
            new_path: 新相对路径。

        Returns:
            无返回值。

        Raises:
            ValueError: 新路径为空或现有 locator payload 损坏。

        """
        if not new_path:
            raise ValueError("new_path 不能为空。")
        offset: models.ExtendedPointId | None = None
        while True:
            records, offset = self._client.scroll(
                collection_name=self.collection_name,
                scroll_filter=models.Filter(
                    must=[
                        _match("source_id", source_id),
                        _match("version_state", _VERSION_ACTIVE),
                    ]
                ),
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for record in records:
                locators = _renamed_locators(record.payload, new_path)
                source_spans = _renamed_source_spans(
                    record.payload,
                    new_path,
                )
                self._client.set_payload(
                    collection_name=self.collection_name,
                    payload={
                        "source_path": new_path,
                        "locators": locators,
                        "source_spans": source_spans,
                    },
                    points=[record.id],
                    wait=True,
                )
            if offset is None:
                return

    def delete_staging(self, source_id: str, doc_version: str) -> None:
        """删除失败版本的 staging 点。

        Args:
            source_id: 持久来源标识。
            doc_version: 失败的内容版本。

        Returns:
            无返回值。

        """
        self._client.delete(
            collection_name=self.collection_name,
            points_selector=_version_filter(
                source_id,
                doc_version,
                _VERSION_STAGING,
            ),
            wait=True,
        )

    def query_dense(
        self,
        vector: list[float],
        *,
        limit: int,
        additional_filter: models.Filter | None = None,
    ) -> list[models.ScoredPoint]:
        """只在 active 版本上执行 dense 查询。

        Args:
            vector: 查询 embedding。
            limit: 返回上限。
            additional_filter: 状态、权威与有效期等确定性预过滤。

        Returns:
            带完整 payload 的得分点。

        """
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=vector,
            using=_DENSE_VECTOR_NAME,
            query_filter=_active_filter(additional_filter),
            limit=limit,
            with_payload=True,
        )
        return response.points

    def query_sparse(
        self,
        query: models.SparseVector | models.Document,
        *,
        limit: int,
        additional_filter: models.Filter | None = None,
    ) -> list[models.ScoredPoint]:
        """只在 active 版本上执行 BM25 sparse 查询。

        Args:
            query: 与 ingest 配置一致的查询 sparse 向量或 Document。
            limit: 返回上限。
            additional_filter: 状态、权威与有效期等确定性预过滤。

        Returns:
            带完整 payload 的得分点。

        """
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=query,
            using=_SPARSE_VECTOR_NAME,
            query_filter=_active_filter(additional_filter),
            limit=limit,
            with_payload=True,
        )
        return response.points

    def switch_alias(self, alias_name: str) -> None:
        """用一个 Qdrant 请求原子切换活动索引别名。

        Args:
            alias_name: 业务查询使用的活动索引别名。

        Returns:
            无返回值。

        """
        self.switch_alias_to(alias_name, self.collection_name)

    def switch_alias_to(
        self,
        alias_name: str,
        collection_name: str,
    ) -> None:
        """把 alias 原子切到指定物理 collection。

        Args:
            alias_name: 业务查询使用的活动索引别名。
            collection_name: 已存在的目标物理 collection。

        Returns:
            无返回值。

        """
        aliases = self._client.get_aliases().aliases
        operations: list[
            models.CreateAliasOperation | models.DeleteAliasOperation
        ] = []
        if any(alias.alias_name == alias_name for alias in aliases):
            operations.append(
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=alias_name)
                )
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=collection_name,
                    alias_name=alias_name,
                )
            )
        )
        self._client.update_collection_aliases(operations)

    def alias_target(self, alias_name: str) -> str | None:
        """返回 alias 当前物理 collection。

        Args:
            alias_name: 业务索引别名。

        Returns:
            物理 collection；alias 不存在时返回 None。

        """
        for alias in self._client.get_aliases().aliases:
            if alias.alias_name == alias_name:
                target = alias.collection_name
                if self.collection_name == alias_name:
                    self.require_compatible_collection(target)
                return target
        return None

    def delete_alias(self, alias_name: str) -> None:
        """删除存在的 alias。

        Args:
            alias_name: 待删除业务别名。

        Returns:
            无返回值。

        """
        if self.alias_target(alias_name) is None:
            return
        self._client.update_collection_aliases(
            [
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=alias_name)
                )
            ]
        )

    def create_snapshot(self) -> models.SnapshotDescription:
        """创建 collection snapshot。

        Args:
            无参数。

        Returns:
            Qdrant snapshot 描述。

        Raises:
            RuntimeError: Qdrant 未返回 snapshot 描述。

        """
        snapshot = self._client.create_snapshot(
            self.collection_name,
            wait=True,
        )
        if snapshot is None:
            raise RuntimeError("Qdrant 未返回 snapshot 描述。")
        return snapshot

    def recover_snapshot(
        self,
        *,
        snapshot_name: str,
        checksum: str,
    ) -> None:
        """从 Qdrant 容器内已校验的 collection snapshot 恢复。

        Args:
            snapshot_name: `create_snapshot` 返回的纯文件名。
            checksum: snapshot 的 64 位小写 SHA256。

        Returns:
            无返回值。

        Raises:
            ValueError: 文件名或摘要不安全。
            RuntimeError: Qdrant 未确认恢复成功。

        """
        _validate_snapshot_identity(snapshot_name, checksum)
        location = (
            "file:///qdrant/snapshots/"
            f"{self.collection_name}/{snapshot_name}"
        )
        recovered = self._client.recover_snapshot(
            collection_name=self.collection_name,
            location=location,
            checksum=checksum,
            priority=models.SnapshotPriority.SNAPSHOT,
            wait=True,
        )
        if recovered is not True:
            raise RuntimeError("Qdrant 未确认 snapshot 恢复成功。")
        self._validate_existing_collection()

    def _set_staging_identity(
        self,
        *,
        control_job_id: str,
        base_manifest_sha256: str | None,
        base_active_count: int,
    ) -> None:
        if not control_job_id:
            raise ValueError("control_job_id 不能为空。")
        if base_manifest_sha256 is not None:
            _validate_sha256(base_manifest_sha256, "base manifest")
        if base_active_count < 0:
            raise ValueError("base_active_count 不能为负数。")
        info = self._client.get_collection(self.collection_name)
        metadata = dict(info.config.metadata or {})
        metadata.update(
            {
                _STAGING_JOB_KEY: control_job_id,
                _BASE_MANIFEST_KEY: base_manifest_sha256 or "",
                _BASE_ACTIVE_COUNT_KEY: base_active_count,
            }
        )
        updated = self._client.update_collection(
            self.collection_name,
            metadata=metadata,
        )
        if updated is not True:
            raise RuntimeError("Qdrant 未确认 staging metadata 更新。")
        self.require_staging_identity(
            control_job_id=control_job_id,
            base_manifest_sha256=base_manifest_sha256,
        )

    def require_compatible_collection(
        self,
        collection_name: str | None = None,
    ) -> None:
        """严格校验现有 collection 的向量与 payload schema。

        Args:
            collection_name: 可选物理 collection；默认使用构造时名称。

        Returns:
            无返回值。

        Raises:
            ValueError: collection 与当前索引契约不兼容。

        """
        self._validate_existing_collection(collection_name)

    def _validate_existing_collection(
        self,
        collection_name: str | None = None,
    ) -> None:
        target = collection_name or self.collection_name
        info = self._client.get_collection(target)
        vectors = info.config.params.vectors
        if not isinstance(vectors, dict):
            raise ValueError("现有 collection 缺少命名 dense 向量。")
        dense = vectors.get(_DENSE_VECTOR_NAME)
        if dense is None or dense.size != self._dense_dimension:
            raise ValueError("现有 collection dense 维度不兼容。")
        sparse = info.config.params.sparse_vectors or {}
        if _SPARSE_VECTOR_NAME not in sparse:
            raise ValueError("现有 collection 缺少 BM25 sparse 向量。")
        metadata = info.config.metadata or {}
        if metadata.get("pipeline_fingerprint") != self._pipeline_fingerprint:
            raise ValueError("现有 collection pipeline 指纹不兼容。")
        if (
            metadata.get("payload_schema_version")
            != _PAYLOAD_SCHEMA_VERSION
        ):
            raise ValueError("现有 collection payload schema 不是 v2。")


def _chunk_payload(chunk: Chunk, *, version_state: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "chunk_id": chunk.chunk_id,
        "source_id": chunk.source_id,
        "source_path": chunk.locators[0].file_path,
        "doc_version": chunk.doc_version,
        "pipeline_fingerprint": chunk.pipeline_fingerprint,
        "section_id": chunk.section_id,
        "neighbor_group_id": chunk.neighbor_group_id,
        "chunk_role": chunk.chunk_role.value,
        "version_state": version_state,
        "text": chunk.text,
        "embedding_text": chunk.embedding_text,
        "element_kind": chunk.element_kind.value,
        "locators": [
            locator.model_dump(mode="json") for locator in chunk.locators
        ],
        "source_spans": [
            span.model_dump(mode="json") for span in chunk.source_spans
        ],
        "content_sha256": chunk.content_sha256,
        "previous_chunk_id": chunk.previous_chunk_id,
        "next_chunk_id": chunk.next_chunk_id,
        "document_status": chunk.document_status,
        "authority_level": chunk.authority_level,
        "contains_ocr": chunk.contains_ocr,
        "minimum_ocr_confidence": chunk.minimum_ocr_confidence,
    }
    if chunk.effective_from is not None:
        payload["effective_from"] = chunk.effective_from.isoformat()
    if chunk.effective_to is not None:
        payload["effective_to"] = chunk.effective_to.isoformat()
    return payload


def _version_filter(
    source_id: str,
    doc_version: str,
    version_state: str,
) -> models.Filter:
    return models.Filter(
        must=[
            _match("source_id", source_id),
            _match("doc_version", doc_version),
            _match("version_state", version_state),
        ]
    )


def _active_filter(
    additional_filter: models.Filter | None,
) -> models.Filter:
    must: list[models.Condition] = [
        _match("version_state", _VERSION_ACTIVE)
    ]
    if additional_filter is not None:
        must.append(additional_filter)
    return models.Filter(must=must)


def _match(field: str, value: str) -> models.FieldCondition:
    return models.FieldCondition(
        key=field,
        match=models.MatchValue(value=value),
    )


def _validate_snapshot_identity(name: str, checksum: str) -> None:
    if (
        not name.endswith(".snapshot")
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("snapshot_name 必须是安全的 snapshot 文件名。")
    _validate_sha256(checksum, "snapshot checksum")


def _validate_sha256(value: str, label: str) -> None:
    if len(value) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} 必须是 64 位小写十六进制。")


def _require_collection_name(value: str) -> None:
    if not value or "/" in value or "\\" in value:
        raise ValueError("collection 名称不安全。")


def _point_id(doc_version: str, chunk_id: str) -> uuid.UUID:
    """生成版本隔离的 Qdrant 物理点标识。

    Args:
        doc_version: 不可变内容版本。
        chunk_id: 跨版本稳定的逻辑分块标识。

    Returns:
        同一版本重试时稳定、不同版本互不覆盖的 UUID。

    """
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"docx-rag:{doc_version}:{chunk_id}",
    )


def _renamed_locators(
    payload: dict[str, object] | None,
    new_path: str,
) -> list[dict[str, object]]:
    if payload is None:
        raise ValueError("Qdrant 点缺少 payload。")
    raw_locators = payload.get("locators")
    if not isinstance(raw_locators, list) or not raw_locators:
        raise ValueError("Qdrant 点缺少 locator 列表。")
    renamed: list[dict[str, object]] = []
    for raw_locator in raw_locators:
        if not isinstance(raw_locator, dict):
            raise ValueError("Qdrant locator payload 格式无效。")
        locator = {str(key): value for key, value in raw_locator.items()}
        locator["file_path"] = new_path
        renamed.append(locator)
    return renamed


def _renamed_source_spans(
    payload: dict[str, object] | None,
    new_path: str,
) -> list[dict[str, object]]:
    if payload is None:
        raise ValueError("Qdrant 点缺少 payload。")
    raw_spans = payload.get("source_spans")
    if not isinstance(raw_spans, list) or not raw_spans:
        raise ValueError("Qdrant 点缺少 source span 列表。")
    renamed: list[dict[str, object]] = []
    for raw_span in raw_spans:
        if not isinstance(raw_span, dict):
            raise ValueError("Qdrant source span payload 格式无效。")
        span = {str(key): value for key, value in raw_span.items()}
        raw_locator = span.get("locator")
        if not isinstance(raw_locator, dict):
            raise ValueError("Qdrant source span locator 格式无效。")
        locator = {str(key): value for key, value in raw_locator.items()}
        locator["file_path"] = new_path
        span["locator"] = locator
        renamed.append(span)
    return renamed
