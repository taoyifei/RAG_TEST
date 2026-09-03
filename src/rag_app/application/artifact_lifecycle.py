"""Filesystem Blob 与 SQLite catalog 的崩溃安全生命周期协调。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    BlobCatalogEntry,
    BlobPhysicalState,
    BlobReference,
    ParsedArtifact,
)
from rag_app.core.ports import (
    ArtifactCatalogPort,
    BlobPutResult,
    BlobStorePort,
    BlobWriteRequest,
)


class BlobLocatorPort(Protocol):
    """Filesystem Blob Store 的受控 locator 只读视图。"""

    def locator(self, blob_id: str) -> str:
        """返回受控相对 locator。

        Args:
            blob_id: 目标 Blob 对象 ID。

        Returns:
            不含绝对路径的相对定位符。

        """
        ...


class ArtifactLifecycleService:
    """先创建/stage，提交引用后才把 Artifact 标为 available。"""

    def __init__(
        self,
        blob_store: BlobStorePort,
        catalog: ArtifactCatalogPort,
        locator: BlobLocatorPort,
    ) -> None:
        """注入物理 Store、权威 catalog 和 locator 边界。

        Args:
            blob_store: content-addressed 物理对象 Store。
            catalog: SQLite Artifact catalog。
            locator: 不暴露绝对路径的相对 locator 提供者。

        Returns:
            无返回值。

        """
        self._blob_store = blob_store
        self._catalog = catalog
        self._locator = locator

    def persist(
        self,
        artifacts: Sequence[ParsedArtifact],
        *,
        owner_document_version_id: str,
        revision_id: str,
        job_id: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """保存 Artifact 并提交可重算引用。

        物理创建后若进程崩溃，对象保持无引用 staged/orphan，后续只能由带
        宽限期的 GC Plan 回收；业务异常不会直接删除共享对象。

        Args:
            artifacts: Parser 返回的待持久化制品。
            owner_document_version_id: 当前逻辑 DocumentVersion。
            revision_id: 当前 staging revision。
            job_id: 创建对象的 ingestion job。

        Returns:
            本次 CREATED 和 EXISTING Artifact IDs。

        """
        unique = {artifact.artifact_id: artifact for artifact in artifacts}
        if len(unique) != len(artifacts):
            for artifact in artifacts:
                if unique[artifact.artifact_id] != artifact:
                    raise ValueError("同一 Artifact ID 禁止对应不同内容。")
        created: list[str] = []
        existing: list[str] = []
        references: list[BlobReference] = []
        for artifact in unique.values():
            outcome = self._blob_store.put_if_absent(
                BlobWriteRequest(
                    blob_id=artifact.artifact_id,
                    content_sha256=artifact.content_sha256,
                    media_type=artifact.media_type,
                    content=artifact.content,
                )
            )
            target = created if outcome is BlobPutResult.CREATED else existing
            target.append(artifact.artifact_id)
            self._catalog.stage(
                BlobCatalogEntry(
                    artifact_id=artifact.artifact_id,
                    content_sha256=artifact.content_sha256,
                    size_bytes=len(artifact.content),
                    media_type=artifact.media_type,
                    physical_state=BlobPhysicalState.STAGED,
                    physical_locator=self._locator.locator(
                        artifact.artifact_id
                    ),
                    created_by_job_id=job_id,
                )
            )
        for artifact in unique.values():
            owner_type = (
                "document_version"
                if artifact.role == "source_document"
                else "parsed_media"
            )
            owner_id = (
                owner_document_version_id
                if artifact.role == "source_document"
                else deterministic_id(
                    "bref",
                    revision_id,
                    owner_document_version_id,
                    artifact.artifact_id,
                )
            )
            reference_id = deterministic_id(
                "bref",
                artifact.artifact_id,
                owner_type,
                owner_id,
                artifact.role,
                (
                    revision_id
                    if artifact.role != "source_document"
                    else "persistent-document-version"
                ),
            )
            references.append(
                BlobReference(
                    reference_id=reference_id,
                    artifact_id=artifact.artifact_id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    role=artifact.role,
                    revision_id=(
                        None
                        if artifact.role == "source_document"
                        else revision_id
                    ),
                )
            )
        self._catalog.commit_references(references)
        return tuple(created), tuple(existing)


__all__ = ["ArtifactLifecycleService", "BlobLocatorPort"]
