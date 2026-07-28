import uuid
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import Chunk, ElementKind, Locator
from rag_app.index import IndexedChunk, QdrantIndex
from rag_app.index.planner import (
    DiscoveredSource,
    SyncActionKind,
    plan_incremental_sync,
)
from rag_app.index.worker import SyncChunkBuilder, SyncWorker
from rag_app.state import JobKind, JobState, SourceVersion, StateStore
from rag_app.state.plans import SyncPlanStore

_API_KEY = "test-only-qdrant-key"
_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64


@dataclass(frozen=True, slots=True)
class _WorkerHarness:
    state: StateStore
    plans: SyncPlanStore
    worker: SyncWorker


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _chunks(path: str, version: SourceVersion) -> list[IndexedChunk]:
    chunk = Chunk(
        chunk_id=f"chunk_{version.content_sha256[:32]}",
        source_id=version.source_id,
        doc_version=version.doc_version,
        pipeline_fingerprint=version.pipeline_fingerprint,
        text=f"{path}:{version.content_sha256[0]}",
        embedding_text=f"{path}:{version.content_sha256[0]}",
        element_kind=ElementKind.PARAGRAPH,
        locators=(
            Locator(
                file_path=path,
                paragraph_index=1,
                fragment=path,
            ),
        ),
        content_sha256=version.content_sha256,
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    return [
        IndexedChunk(
            chunk=chunk,
            dense=[1.0] + [0.0] * 1023,
            sparse=models.SparseVector(indices=[1], values=[1.0]),
        )
    ]


def _run_plan(
    *,
    harness: _WorkerHarness,
    discovered: tuple[DiscoveredSource, ...],
    key: str,
    builder: SyncChunkBuilder = _chunks,
) -> str:
    job = harness.state.create_job(
        idempotency_key=key,
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    plan = plan_incremental_sync(
        discovered,
        harness.state.list_active_sources(),
    )
    harness.plans.save(job.job_id, plan)
    result = harness.worker.run_next(
        worker_id="single-worker",
        lease_seconds=60,
        build_chunks=builder,
    )
    assert result is not None
    return result.job_id


def test_real_sync_worker_add_update_rename_delete_and_retry(
    tmp_path: Path,
) -> None:
    client = _client()
    collection = f"rag-sync-worker-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=1024,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    plans = SyncPlanStore(state.path)
    plans.initialize()
    worker = SyncWorker(state, plans, index)
    harness = _WorkerHarness(state=state, plans=plans, worker=worker)
    try:
        index.create_collection()
        first_job_id = _run_plan(
            harness=harness,
            discovered=(
                DiscoveredSource("甲.docx", "a" * 64),
                DiscoveredSource("乙.docx", "b" * 64),
                DiscoveredSource("丙.docx", "c" * 64),
            ),
            key="incremental:first",
        )
        assert state.get_job(first_job_id).state == JobState.SUCCEEDED

        attempts = 0

        def transient_builder(
            path: str,
            version: SourceVersion,
        ) -> list[IndexedChunk]:
            nonlocal attempts
            if path == "丁.docx" and attempts == 0:
                attempts += 1
                raise TimeoutError("test-only")
            return _chunks(path, version)

        second_job_id = _run_plan(
            harness=harness,
            discovered=(
                DiscoveredSource("新甲.docx", "a" * 64),
                DiscoveredSource("丙.docx", "e" * 64),
                DiscoveredSource("丁.docx", "d" * 64),
            ),
            key="incremental:second",
            builder=transient_builder,
        )

        assert attempts == 1
        assert state.get_job(second_job_id).state == JobState.SUCCEEDED
        assert {
            source.current_path: source.content_sha256
            for source in state.list_active_sources()
        } == {
            "新甲.docx": "a" * 64,
            "丙.docx": "e" * 64,
            "丁.docx": "d" * 64,
        }
        active_points = index.query_dense(
            [1.0] + [0.0] * 1023,
            limit=10,
        )
        assert {point.payload["source_path"] for point in active_points} == {
            "新甲.docx",
            "丙.docx",
            "丁.docx",
        }
        add_item = next(
            item
            for item in plans.list_items(second_job_id)
            if item.action.kind == SyncActionKind.ADD
        )
        assert add_item.attempt == 2
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
