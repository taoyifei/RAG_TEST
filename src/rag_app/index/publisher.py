"""全量索引 snapshot、alias 与 manifest 发布事务。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rag_app.contracts import IndexManifest
from rag_app.index.qdrant import QdrantIndex
from rag_app.manifest import (
    ManifestRepository,
    ManifestState,
    StoredManifest,
)

__all__ = ["FullIndexPublisher", "PublishResult", "PublishState"]


class PublishState(StrEnum):
    """全量索引发布结果。"""

    PUBLISHED = "published"
    RECOVERED = "recovered"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class PublishResult:
    """一次全量索引发布的审计结果。"""

    collection_name: str
    manifest_sha256: str
    state: PublishState


class FullIndexPublisher:
    """按 snapshot、alias、manifest 顺序发布完整索引。"""

    def __init__(
        self,
        repository: ManifestRepository,
        index: QdrantIndex,
        *,
        alias_name: str,
    ) -> None:
        """保存发布依赖。

        Args:
            repository: manifest 历史库。
            index: 已完整构建的新物理 collection。
            alias_name: 查询使用的活动索引别名。

        """
        self._repository = repository
        self._index = index
        self._alias_name = alias_name

    def publish(self, manifest: IndexManifest) -> PublishResult:
        """幂等发布一个完整索引。

        Args:
            manifest: 与新物理 collection 对应的完整 manifest。

        Returns:
            新发布、崩溃恢复或无变化结果。

        Raises:
            ValueError: manifest 与物理 collection 不兼容。
            RuntimeError: snapshot 缺少校验摘要或历史状态冲突。

        """
        self._validate_manifest(manifest)
        stored = self._repository.get(manifest.collection_name)
        alias_target = self._index.alias_target(self._alias_name)
        if stored is not None:
            self._require_same_manifest(stored, manifest)
            if (
                stored.state == ManifestState.ACTIVE
                and alias_target == manifest.collection_name
            ):
                return _result(stored, PublishState.UNCHANGED)
            if (
                stored.state == ManifestState.STAGING
                and alias_target == manifest.collection_name
            ):
                self._repository.activate(manifest.collection_name)
                active = self._repository.get(manifest.collection_name)
                return _result(_require_stored(active), PublishState.RECOVERED)
            if stored.state != ManifestState.STAGING:
                raise RuntimeError("不能重新发布 retired manifest。")
        else:
            snapshot = self._index.create_snapshot()
            if snapshot.checksum is None:
                raise RuntimeError("Qdrant snapshot 缺少 checksum。")
            stored = self._repository.stage(
                manifest,
                snapshot_name=snapshot.name,
                snapshot_checksum=snapshot.checksum,
            )

        old_target = alias_target
        self._index.switch_alias(self._alias_name)
        try:
            self._repository.activate(manifest.collection_name)
        except Exception:
            if old_target is None:
                self._index.delete_alias(self._alias_name)
            else:
                self._index.switch_alias_to(
                    self._alias_name,
                    old_target,
                )
            raise
        active = self._repository.get(manifest.collection_name)
        return _result(_require_stored(active), PublishState.PUBLISHED)

    def _validate_manifest(self, manifest: IndexManifest) -> None:
        if manifest.collection_name != self._index.collection_name:
            raise ValueError("manifest collection 与待发布索引不一致。")
        if (
            manifest.pipeline_fingerprint
            != self._index.pipeline_fingerprint
        ):
            raise ValueError("manifest pipeline 与待发布索引不一致。")

    def _require_same_manifest(
        self,
        stored: StoredManifest,
        manifest: IndexManifest,
    ) -> None:
        if stored.manifest != manifest:
            raise ValueError("collection 已绑定其他 manifest。")


def _result(
    stored: StoredManifest,
    state: PublishState,
) -> PublishResult:
    return PublishResult(
        collection_name=stored.manifest.collection_name,
        manifest_sha256=stored.manifest_sha256,
        state=state,
    )


def _require_stored(value: StoredManifest | None) -> StoredManifest:
    if value is None:
        raise RuntimeError("发布后 manifest 记录丢失。")
    return value
