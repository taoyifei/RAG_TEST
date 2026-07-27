"""管理 API 任务到真实 Qdrant 发布的闭环测试。"""

from __future__ import annotations

import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import Chunk, ElementKind, Locator, PipelineSpec
from rag_app.index.job_runner import (
    IndexJobRunner,
    JobRunnerConfig,
    JobRunnerServices,
)
from rag_app.index.qdrant import IndexedChunk
from rag_app.manifest import ManifestRepository
from rag_app.state import JobKind, JobState, SourceVersion, StateStore

_API_KEY = "test-only-qdrant-key"


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _pipeline() -> PipelineSpec:
    return PipelineSpec(
        schema_version="1",
        parser_revision="docx-parser-v1",
        ocr_model="server-gpu-ocr-unselected",
        ocr_revision="unselected",
        chunker_revision="structural-v1",
        chunker_parameters=(("target_tokens", "384"),),
        embedding_model="test-embedding",
        embedding_revision="test-revision",
        embedding_dimension=3,
        sparse_model="qdrant/bm25",
        sparse_revision="test-bm25",
        index_revision="qdrant-v1.18.3",
        reranker_model="test-reranker",
        reranker_revision="test-revision",
        llm_revisions=(("test-llm", "test-revision"),),
        prompt_revision="test-prompt",
    )


def _build_chunks(
    source_path: str,
    version: SourceVersion,
) -> tuple[IndexedChunk, ...]:
    chunk = Chunk(
        chunk_id=f"chunk_{version.content_sha256[:32]}",
        source_id=version.source_id,
        doc_version=version.doc_version,
        pipeline_fingerprint=version.pipeline_fingerprint,
        text=source_path,
        embedding_text=source_path,
        element_kind=ElementKind.PARAGRAPH,
        locators=(
            Locator(
                file_path=source_path,
                paragraph_index=1,
                fragment=source_path,
            ),
        ),
        content_sha256=version.content_sha256,
    )
    return (
        IndexedChunk(
            chunk=chunk,
            dense=[1.0, 0.0, 0.0],
            sparse=models.SparseVector(indices=[1], values=[1.0]),
        ),
    )


def test_full_then_incremental_job_updates_manifest_and_zero_duplicates(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-job-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "甲.docx").write_bytes(b"first")
    (docs / "乙.docx").write_bytes(b"second")
    control = StateStore(tmp_path / "control.sqlite3")
    control.initialize()
    manifests = ManifestRepository(tmp_path / "manifests.sqlite3")
    manifests.initialize()
    pipeline = _pipeline()
    runner = IndexJobRunner(
        config=JobRunnerConfig(
            alias_name=alias,
            input_root=docs,
            index_state_dir=tmp_path / "indexes",
            collection_prefix=f"rag-job-{suffix}",
            lease_seconds=60,
        ),
        services=JobRunnerServices(
            control=control,
            manifests=manifests,
            qdrant=client,
            pipeline=pipeline,
            build_chunks_factory=lambda _: _build_chunks,
        ),
    )
    created_collections: set[str] = set()
    try:
        full = control.create_job(
            idempotency_key="full:v1",
            kind=JobKind.FULL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        full_result = runner.run_next(worker_id="single-index-worker")
        assert full_result is not None
        created_collections.add(full_result.collection_name)
        assert control.get_job(full.job_id).state == JobState.SUCCEEDED
        assert client.count(full_result.collection_name, exact=True).count == 2
        assert runner.run_next(worker_id="single-index-worker") is None
        assert client.count(full_result.collection_name, exact=True).count == 2

        (docs / "甲.docx").rename(docs / "新甲.docx")
        (docs / "乙.docx").unlink()
        (docs / "丙.docx").write_bytes(b"third")
        incremental = control.create_job(
            idempotency_key="incremental:v2",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        result = runner.run_next(worker_id="single-index-worker")
        assert result is not None
        assert result.collection_name == full_result.collection_name
        assert control.get_job(incremental.job_id).state == JobState.SUCCEEDED
        assert client.count(result.collection_name, exact=True).count == 3

        active = manifests.get_active()
        assert active is not None
        assert {
            source.current_path for source in active.manifest.sources
        } == {"新甲.docx", "丙.docx"}
        assert manifests.count_revisions(result.collection_name) == 2
    finally:
        target = next(
            (
                item.collection_name
                for item in client.get_aliases().aliases
                if item.alias_name == alias
            ),
            None,
        )
        if target is not None:
            client.delete_collection(target)
        for collection in created_collections:
            if client.collection_exists(collection):
                client.delete_collection(collection)
