"""P06 前置的解析制品 Blob 事务合同。"""

from __future__ import annotations

from collections.abc import Sequence

from rag_app.core.models import ParsedArtifact
from rag_app.core.models.common import FrozenModel
from rag_app.core.ports import BlobPutResult, BlobStorePort, BlobWriteRequest


class ArtifactPersistenceResult(FrozenModel):
    """一次制品事务实际创建和复用的 Blob 身份。"""

    created_artifact_ids: tuple[str, ...]
    existing_artifact_ids: tuple[str, ...]


def persist_artifacts_transactionally(
    artifacts: Sequence[ParsedArtifact],
    blob_store: BlobStorePort,
) -> ArtifactPersistenceResult:
    """幂等保存制品，并只回滚本次新建的 Blob。

    Args:
        artifacts: Parser 返回的 source/media 制品。
        blob_store: 支持 CREATED/EXISTING 结果的 Blob Store。

    Returns:
        本次创建和复用的稳定 artifact ID。

    Raises:
        Exception: Store 写入失败；回滚本次 CREATED 后原样抛出。

    """
    unique = {artifact.artifact_id: artifact for artifact in artifacts}
    if len(unique) != len(artifacts):
        for artifact in artifacts:
            if unique[artifact.artifact_id] != artifact:
                raise ValueError("同一 artifact ID 禁止对应不同制品。")
    created: list[str] = []
    existing: list[str] = []
    try:
        for artifact in unique.values():
            outcome = blob_store.put_if_absent(
                BlobWriteRequest(
                    blob_id=artifact.artifact_id,
                    content_sha256=artifact.content_sha256,
                    media_type=artifact.media_type,
                    content=artifact.content,
                )
            )
            if outcome is BlobPutResult.CREATED:
                created.append(artifact.artifact_id)
            else:
                existing.append(artifact.artifact_id)
    except Exception:
        for artifact_id in reversed(created):
            blob_store.delete(artifact_id)
        raise
    return ArtifactPersistenceResult(
        created_artifact_ids=tuple(created),
        existing_artifact_ids=tuple(existing),
    )


__all__ = ["ArtifactPersistenceResult", "persist_artifacts_transactionally"]
