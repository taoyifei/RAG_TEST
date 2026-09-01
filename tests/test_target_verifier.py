from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import (
    Chunk,
    ChunkRole,
    ChunkSourceSpan,
    ElementKind,
    IndexManifest,
    Locator,
    PipelineSpec,
    SourceRecord,
)
from rag_app.index.qdrant import IndexedChunk, QdrantIndex
from rag_app.index.verifier import TargetIndexVerifier
from rag_app.state import JobKind, StateStore
from rag_app.state.models import CollectionStateIdentity

pytestmark = pytest.mark.local_integration

_API_KEY = "test-only-qdrant-key"


@dataclass(frozen=True, slots=True)
class _TargetFixture:
    client: QdrantClient
    index: QdrantIndex
    state: StateStore
    manifest: IndexManifest
    identity: CollectionStateIdentity
    point_id: object


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _pipeline() -> PipelineSpec:
    return PipelineSpec(
        schema_version="2",
        parser_revision="docx-parser-v1",
        ocr_model="test-ocr",
        ocr_revision="test-ocr-v1",
        chunker_revision="structural-v1",
        chunker_parameters=(
            ("target_tokens", "384"),
            ("hard_max_tokens", "512"),
            ("overlap_tokens", "64"),
        ),
        embedding_model="test-embedding",
        embedding_revision="test-embedding-v1",
        embedding_dimension=3,
        sparse_model="qdrant/bm25",
        sparse_revision="test-bm25-v1",
        index_revision="qdrant-v1.18.3",
        reranker_model="test-reranker",
        reranker_revision="test-reranker-v1",
        llm_model="test-llm",
        llm_revisions=(("test-llm", "test-llm-v1"),),
        prompt_revision="test-prompt-v1",
    )


def _indexed_chunk(
    *,
    source_id: str,
    doc_version: str,
    content_sha256: str,
    pipeline_fingerprint: str,
    chunk_suffix: str = "1",
) -> IndexedChunk:
    locator = Locator(
        file_path="source.docx",
        paragraph_index=1,
        segment_index=1,
        fragment="target verifier",
    )
    chunk = Chunk(
        chunk_id="chunk_" + chunk_suffix * 32,
        source_id=source_id,
        doc_version=doc_version,
        pipeline_fingerprint=pipeline_fingerprint,
        section_id="section_" + "2" * 32,
        neighbor_group_id="group_" + "3" * 32,
        chunk_role=ChunkRole.TEXT,
        source_spans=(
            ChunkSourceSpan(
                element_id="element-target-verifier",
                locator=locator,
                start_char=0,
                end_char=15,
                source_start_char=0,
                source_end_char=15,
            ),
        ),
        text="target verifier",
        embedding_text="target verifier",
        element_kind=ElementKind.PARAGRAPH,
        locators=(locator,),
        content_sha256=content_sha256,
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    return IndexedChunk(
        chunk=chunk,
        dense=[1.0, 0.0, 0.0],
        sparse=models.SparseVector(indices=[1], values=[1.0]),
    )


def _target_fixture(tmp_path: Path) -> _TargetFixture:
    client = _client()
    suffix = uuid.uuid4().hex
    pipeline = _pipeline()
    fingerprint = pipeline.fingerprint()
    collection_name = f"rag-target-verifier-{suffix}"
    identity = CollectionStateIdentity(
        control_job_id="job_" + "a" * 32,
        pipeline_fingerprint=fingerprint,
        base_manifest_sha256=None,
    )
    index = QdrantIndex(
        client,
        collection_name=collection_name,
        dense_dimension=pipeline.embedding_dimension,
        pipeline_fingerprint=fingerprint,
        index_revision=pipeline.index_revision,
    )
    index.prepare_staging_collection(
        control_job_id=identity.control_job_id,
        base_manifest_sha256=None,
    )
    state = StateStore(tmp_path / f"{collection_name}.sqlite3")
    state.initialize()
    state.bind_collection_identity(
        control_job_id=identity.control_job_id,
        pipeline_fingerprint=fingerprint,
        base_manifest_sha256=None,
    )
    job = state.create_job(
        idempotency_key=f"target-verifier:{suffix}",
        kind=JobKind.FULL,
        pipeline_fingerprint=fingerprint,
    )
    version = state.stage_source_version(
        job_id=job.job_id,
        source_path="source.docx",
        content_sha256="b" * 64,
        pipeline_fingerprint=fingerprint,
    )
    indexed = _indexed_chunk(
        source_id=version.source_id,
        doc_version=version.doc_version,
        content_sha256=version.content_sha256,
        pipeline_fingerprint=fingerprint,
    )
    index.stage_chunks((indexed,))
    state.record_staged_chunk_count(
        version.source_id,
        version.doc_version,
        1,
    )
    index.activate_source_version(version.source_id, version.doc_version)
    state.activate_source_version(version.source_id, version.doc_version)
    records, _ = client.scroll(
        collection_name,
        limit=10,
        with_payload=True,
        with_vectors=True,
    )
    manifest = IndexManifest(
        manifest_version="1",
        collection_name=collection_name,
        created_at=datetime(2026, 7, 30, tzinfo=UTC),
        pipeline=pipeline,
        pipeline_fingerprint=fingerprint,
        sources=(
            SourceRecord(
                source_id=version.source_id,
                current_path=version.source_path,
                content_sha256=version.content_sha256,
                doc_version=version.doc_version,
            ),
        ),
    )
    return _TargetFixture(
        client=client,
        index=index,
        state=state,
        manifest=manifest,
        identity=identity,
        point_id=records[0].id,
    )


def _verifier(target: _TargetFixture) -> TargetIndexVerifier:
    return TargetIndexVerifier(
        state=target.state,
        index=target.index,
        manifest=target.manifest,
        identity=target.identity,
    )


def test_target_verifier_accepts_complete_target_and_reentry(
    tmp_path: Path,
) -> None:
    target = _target_fixture(tmp_path)
    try:
        first = _verifier(target).verify()
        second = _verifier(target).verify()

        assert first == second
        assert first.source_count == 1
        assert first.active_point_count == 1
    finally:
        target.client.delete_collection(target.index.collection_name)

@pytest.mark.parametrize(
    "drift",
    ["deleted", "extra", "chunk_count", "staging"],
)
def test_target_verifier_rejects_qdrant_and_state_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    target = _target_fixture(tmp_path)
    source = target.manifest.sources[0]
    try:
        if drift == "deleted":
            target.client.delete(
                target.index.collection_name,
                points_selector=models.PointIdsList(
                    points=[target.point_id],
                ),
                wait=True,
            )
        elif drift == "extra":
            extra = _indexed_chunk(
                source_id="src_" + "c" * 32,
                doc_version="sha256:" + "d" * 64,
                content_sha256="d" * 64,
                pipeline_fingerprint=target.manifest.pipeline_fingerprint,
                chunk_suffix="4",
            )
            target.index.stage_chunks((extra,))
            target.index.activate_source_version(
                extra.chunk.source_id,
                extra.chunk.doc_version,
            )
        elif drift == "chunk_count":
            with sqlite3.connect(target.state.path) as connection:
                connection.execute(
                    """
                    UPDATE source_versions SET chunk_count = 2
                    WHERE source_id = ? AND doc_version = ?
                    """,
                    (source.source_id, source.doc_version),
                )
        else:
            extra = _indexed_chunk(
                source_id=source.source_id,
                doc_version=source.doc_version,
                content_sha256=source.content_sha256,
                pipeline_fingerprint=target.manifest.pipeline_fingerprint,
                chunk_suffix="5",
            )
            target.index.stage_chunks((extra,))

        with pytest.raises((RuntimeError, ValueError)):
            _verifier(target).verify()
    finally:
        target.client.delete_collection(target.index.collection_name)


def test_target_verifier_rejects_corrupt_sqlite_state(tmp_path: Path) -> None:
    target = _target_fixture(tmp_path)
    try:
        corrupt_path = tmp_path / "corrupt.sqlite3"
        corrupt_path.write_bytes(b"not-a-sqlite-database")
        corrupt_path.replace(target.state.path)

        with pytest.raises((sqlite3.DatabaseError, RuntimeError, ValueError)):
            _verifier(target).verify()
    finally:
        target.client.delete_collection(target.index.collection_name)
