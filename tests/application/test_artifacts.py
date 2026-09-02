"""P06 前置的 ParsedArtifact Blob 事务合同。"""

from __future__ import annotations

import hashlib

import pytest

from rag_app.adapters.legacy.stores import InMemoryBlobStore
from rag_app.application.artifacts import persist_artifacts_transactionally
from rag_app.core.models import ParsedArtifact
from rag_app.core.ports import BlobPutResult, BlobWriteRequest


def _artifact(content: bytes) -> ParsedArtifact:
    digest = hashlib.sha256(content).hexdigest()
    return ParsedArtifact(
        artifact_id=f"sha256:{digest}",
        content_sha256=digest,
        media_type="application/octet-stream",
        content=content,
        role="embedded_media",
    )


class _FailAfterStore(InMemoryBlobStore):
    def __init__(self, fail_artifact_id: str) -> None:
        super().__init__()
        self.fail_artifact_id = fail_artifact_id
        self.deleted: list[str] = []

    def put_if_absent(self, request: BlobWriteRequest) -> BlobPutResult:
        if request.blob_id == self.fail_artifact_id:
            raise OSError("injected write failure")
        return super().put_if_absent(request)

    def delete(self, blob_id: str) -> None:
        self.deleted.append(blob_id)
        super().delete(blob_id)


def test_transaction_rolls_back_only_blobs_created_by_this_attempt() -> None:
    existing = _artifact(b"existing")
    created = _artifact(b"created")
    failing = _artifact(b"failing")
    store = _FailAfterStore(failing.artifact_id)
    store.put_if_absent(
        BlobWriteRequest(
            blob_id=existing.artifact_id,
            content_sha256=existing.content_sha256,
            media_type=existing.media_type,
            content=existing.content,
        )
    )

    with pytest.raises(OSError, match="injected"):
        persist_artifacts_transactionally(
            (existing, created, failing),
            store,
        )

    assert store.exists(existing.artifact_id)
    assert not store.exists(created.artifact_id)
    assert store.deleted == [created.artifact_id]


def test_duplicate_artifacts_are_written_once() -> None:
    artifact = _artifact(b"same bytes")
    store = InMemoryBlobStore()

    result = persist_artifacts_transactionally(
        (artifact, artifact),
        store,
    )

    assert result.created_artifact_ids == (artifact.artifact_id,)
    assert result.existing_artifact_ids == ()
