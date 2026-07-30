"""发布前统一校验 Qdrant target 与独立 SQLite state。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.contracts import IndexManifest
from rag_app.index.qdrant import QdrantIndex
from rag_app.state import StateStore, VersionState
from rag_app.state.models import ActiveSource, CollectionStateIdentity

__all__ = ["TargetIndexVerifier", "TargetVerification"]


@dataclass(frozen=True, slots=True)
class TargetVerification:
    """一次 target 全量一致性校验的非敏感摘要。"""

    source_count: int
    active_point_count: int


class TargetIndexVerifier:
    """在 snapshot、alias 和 manifest 变更前验证完整 target。"""

    def __init__(
        self,
        *,
        state: StateStore,
        index: QdrantIndex,
        manifest: IndexManifest,
        identity: CollectionStateIdentity,
    ) -> None:
        """冻结本次待发布 target 的四方身份。

        Args:
            state: target collection 对应的独立 SQLite 状态库。
            index: target 物理 Qdrant collection。
            manifest: 即将发布的完整来源 manifest。
            identity: control job、pipeline 与 base manifest 身份。

        Returns:
            无返回值。

        """
        self._state = state
        self._index = index
        self._manifest = manifest
        self._identity = identity

    def verify(self) -> TargetVerification:
        """证明 target state、Qdrant 点和待发布 manifest 完全一致。

        Args:
            无参数。

        Returns:
            不含正文的来源数与活动点数摘要。

        Raises:
            FileNotFoundError: target SQLite state 不存在或不安全。
            LookupError: manifest 声明的来源版本未写入 state。
            RuntimeError: 来源、点数、状态或完整性不一致。
            ValueError: collection、pipeline、schema 或身份不兼容。

        """
        self._require_manifest_identity()
        self._state.require_integrity()
        self._state.require_collection_identity(
            control_job_id=self._identity.control_job_id,
            pipeline_fingerprint=self._identity.pipeline_fingerprint,
            base_manifest_sha256=self._identity.base_manifest_sha256,
        )
        self._index.require_compatible_collection()
        self._index.require_staging_identity(
            control_job_id=self._identity.control_job_id,
            base_manifest_sha256=self._identity.base_manifest_sha256,
        )
        expected_sources = tuple(
            ActiveSource(
                source_id=source.source_id,
                current_path=source.current_path,
                content_sha256=source.content_sha256,
                doc_version=source.doc_version,
            )
            for source in self._manifest.sources
        )
        if self._state.list_active_sources() != expected_sources:
            raise RuntimeError(
                "target state 活动来源与待发布 manifest 不一致。"
            )
        expected_point_count = sum(
            self._verify_source(source) for source in expected_sources
        )
        actual_point_count = self._index.count_active_exact()
        if actual_point_count != expected_point_count:
            raise RuntimeError(
                "target collection 活动点总数与来源 chunk_count 之和不一致。"
            )
        if self._index.count_state_exact(VersionState.STAGING.value) != 0:
            raise RuntimeError("target collection 仍含 staging 点。")
        return TargetVerification(
            source_count=len(expected_sources),
            active_point_count=actual_point_count,
        )

    def _require_manifest_identity(self) -> None:
        if self._manifest.collection_name != self._index.collection_name:
            raise ValueError("target manifest collection 身份不一致。")
        if (
            self._manifest.pipeline_fingerprint
            != self._identity.pipeline_fingerprint
            or self._index.pipeline_fingerprint
            != self._identity.pipeline_fingerprint
        ):
            raise ValueError(
                "target manifest、state 与 collection pipeline 不一致。"
            )
        if (
            self._manifest.pipeline.index_revision
            != self._index.index_revision
        ):
            raise ValueError(
                "target manifest 与 collection index revision 不一致。"
            )
        if any(not source.active for source in self._manifest.sources):
            raise ValueError("待发布 manifest 只能包含 active 来源。")

    def _verify_source(self, source: ActiveSource) -> int:
        version = self._state.get_source_version(
            source.source_id,
            source.doc_version,
        )
        if (
            version.state != VersionState.ACTIVE
            or version.content_sha256 != source.content_sha256
            or version.pipeline_fingerprint
            != self._identity.pipeline_fingerprint
        ):
            raise RuntimeError("target state 活动来源版本字段不一致。")
        if version.chunk_count is None or version.chunk_count <= 0:
            raise RuntimeError("target state 活动来源缺少有效 chunk_count。")
        actual_count = self._index.count_version(
            source.source_id,
            source.doc_version,
            VersionState.ACTIVE.value,
        )
        if actual_count != version.chunk_count:
            raise RuntimeError(
                "target collection 来源版本点数与 SQLite chunk_count 不一致。"
            )
        return version.chunk_count
