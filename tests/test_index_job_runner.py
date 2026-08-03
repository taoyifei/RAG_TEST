"""管理 API 任务到真实 Qdrant 发布的闭环测试。"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
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
)
from rag_app.index.job_runner import (
    IndexJobRunner,
    JobRunnerConfig,
    JobRunnerServices,
    JobRunResult,
)
from rag_app.index.qdrant import IndexedChunk, QdrantIndex
from rag_app.index.verifier import TargetIndexVerifier
from rag_app.manifest import ManifestRepository
from rag_app.retrieval.routing import KeywordRouteRule, KeywordSoftRouter
from rag_app.state import JobKind, JobState, SourceVersion, StateStore

_API_KEY = "test-only-qdrant-key"


def _state_path(root: Path, collection_name: str) -> Path:
    digest = hashlib.sha256(collection_name.encode()).hexdigest()[:24]
    return root / f"index-{digest}.sqlite3"


def _alias_target(client: QdrantClient, alias: str) -> str | None:
    return next(
        (
            item.collection_name
            for item in client.get_aliases().aliases
            if item.alias_name == alias
        ),
        None,
    )


def _payloads(
    client: QdrantClient,
    collection_name: str,
) -> tuple[dict[str, object] | None, ...]:
    records, _ = client.scroll(
        collection_name,
        limit=10,
        with_payload=True,
        with_vectors=False,
    )
    return tuple(record.payload for record in records)


def _collection_names(client: QdrantClient) -> set[str]:
    return {item.name for item in client.get_collections().collections}


def _delete_test_collections(
    client: QdrantClient,
    collection_prefix: str,
) -> None:
    created_collections = {
        collection
        for collection in _collection_names(client)
        if collection.startswith(f"{collection_prefix}-")
    }
    for collection in sorted(created_collections):
        for snapshot in client.list_snapshots(collection):
            client.delete_snapshot(collection, snapshot.name)
        client.delete_collection(collection)


def _assert_manifest_matches_active_records(
    client: QdrantClient,
    manifest: IndexManifest,
) -> None:
    records, _ = client.scroll(
        manifest.collection_name,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="version_state",
                    match=models.MatchValue(value="active"),
                )
            ]
        ),
        limit=100,
        with_payload=True,
        with_vectors=False,
    )
    actual = {
        (
            str(record.payload["source_id"]),
            str(record.payload["source_path"]),
            str(record.payload["doc_version"]),
        )
        for record in records
        if record.payload is not None
    }
    expected = {
        (source.source_id, source.current_path, source.doc_version)
        for source in manifest.sources
    }
    assert actual == expected


def _assert_collection_can_rollback(
    client: QdrantClient,
    *,
    alias: str,
    old_collection: str,
    target_manifest: IndexManifest,
    expected_old_count: int,
) -> None:
    old_index = QdrantIndex(
        client,
        collection_name=old_collection,
        dense_dimension=target_manifest.pipeline.embedding_dimension,
        pipeline_fingerprint=target_manifest.pipeline_fingerprint,
    )
    target_index = QdrantIndex(
        client,
        collection_name=target_manifest.collection_name,
        dense_dimension=target_manifest.pipeline.embedding_dimension,
        pipeline_fingerprint=target_manifest.pipeline_fingerprint,
    )
    old_index.switch_alias(alias)
    assert _alias_target(client, alias) == old_collection
    assert client.count(old_collection, exact=True).count == expected_old_count
    target_index.switch_alias(alias)
    assert _alias_target(client, alias) == target_manifest.collection_name


def _assert_idle_with_exact_count(
    runner: IndexJobRunner,
    client: QdrantClient,
    collection_name: str,
    expected_count: int,
) -> None:
    assert runner.run_next(worker_id="single-index-worker") is None
    assert client.count(collection_name, exact=True).count == expected_count


def _require_succeeded(result: JobRunResult | None) -> JobRunResult:
    assert result is not None
    assert result.state == JobState.SUCCEEDED, result
    assert result.collection_name
    return result


def _published_state(
    client: QdrantClient,
    alias: str,
    collection_name: str,
    state_path: Path,
) -> tuple[
    str | None,
    tuple[dict[str, object] | None, ...],
    StateStore,
    tuple[SourceVersion, ...],
]:
    state = StateStore(state_path)
    return (
        _alias_target(client, alias),
        _payloads(client, collection_name),
        state,
        state.list_active_sources(),
    )


def _assert_delete_succeeds_when_add_fails(
    *,
    config: JobRunnerConfig,
    services: JobRunnerServices,
    old_collection: str,
) -> str:
    docs = config.input_root
    (docs / "新甲.docx").unlink()
    (docs / "乙.docx").write_bytes(b"second")
    (docs / "丙.docx").write_bytes(b"third")
    old_manifest = services.manifests.get_active()
    assert old_manifest is not None
    old_payloads = _payloads(services.qdrant, old_collection)

    def failing_builder(
        source_path: str,
        version: SourceVersion,
    ) -> tuple[IndexedChunk, ...]:
        if source_path == "丙.docx":
            raise RuntimeError("test-only add failure")
        return _build_chunks(source_path, version)

    runner = IndexJobRunner(
        config=config,
        services=JobRunnerServices(
            control=services.control,
            manifests=services.manifests,
            qdrant=services.qdrant,
            pipeline=services.pipeline,
            build_chunks_factory=lambda _: failing_builder,
        ),
    )
    job = services.control.create_job(
        idempotency_key="cow:delete-then-add-failure",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=services.pipeline.fingerprint(),
    )
    before_collections = _collection_names(services.qdrant)

    failed = runner.run_next(worker_id="cow-worker")

    assert failed is not None
    assert failed.state == JobState.FAILED
    assert services.control.get_job(job.job_id).state == JobState.FAILED
    targets = _collection_names(services.qdrant) - before_collections
    assert len(targets) == 1
    target = targets.pop()
    target_state = StateStore(_state_path(config.index_state_dir, target))
    assert {
        source.current_path for source in target_state.list_active_sources()
    } == {"乙.docx"}
    assert services.manifests.get_active() == old_manifest
    assert _alias_target(services.qdrant, config.alias_name) == old_collection
    assert _payloads(services.qdrant, old_collection) == old_payloads
    return target


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
        trust_env=False,
    )


def _pipeline() -> PipelineSpec:
    return PipelineSpec(
        schema_version="2",
        parser_revision="docx-parser-v1",
        ocr_model="server-gpu-ocr-unselected",
        ocr_revision="unselected",
        chunker_revision="structural-v1",
        chunker_parameters=(
            ("target_tokens", "384"),
            ("hard_max_tokens", "512"),
            ("overlap_tokens", "64"),
        ),
        embedding_model="test-embedding",
        embedding_revision="test-revision",
        embedding_dimension=3,
        sparse_model="qdrant/bm25",
        sparse_revision="test-bm25",
        index_revision="qdrant-v1.18.3",
        reranker_model="test-reranker",
        reranker_revision="test-revision",
        llm_model="test-llm",
        llm_revisions=(("test-llm", "test-revision"),),
        prompt_revision="test-prompt",
    )


def _build_chunks(
    source_path: str,
    version: SourceVersion,
) -> tuple[IndexedChunk, ...]:
    locator = Locator(
        file_path=source_path,
        paragraph_index=1,
        segment_index=1,
        fragment=source_path,
    )
    chunk = Chunk(
        chunk_id=f"chunk_{version.content_sha256[:32]}",
        source_id=version.source_id,
        doc_version=version.doc_version,
        pipeline_fingerprint=version.pipeline_fingerprint,
        section_id="section_" + "a" * 32,
        neighbor_group_id="group_" + "b" * 32,
        chunk_role=ChunkRole.TEXT,
        source_spans=(
            ChunkSourceSpan(
                element_id="element-index-job",
                locator=locator,
                start_char=0,
                end_char=len(source_path),
                source_start_char=0,
                source_end_char=len(source_path),
            ),
        ),
        text=source_path,
        embedding_text=source_path,
        element_kind=ElementKind.PARAGRAPH,
        locators=(locator,),
        content_sha256=version.content_sha256,
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    return (
        IndexedChunk(
            chunk=chunk,
            dense=[1.0, 0.0, 0.0],
            sparse=models.SparseVector(indices=[1], values=[1.0]),
        ),
    )


def test_job_runner_invokes_target_verifier_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "source.docx").write_bytes(b"source")
    control = StateStore(tmp_path / "control.sqlite3")
    control.initialize()
    manifests = ManifestRepository(tmp_path / "manifests.sqlite3")
    manifests.initialize()
    pipeline = _pipeline()
    runner = IndexJobRunner(
        config=JobRunnerConfig(
            alias_name=f"rag-verifier-alias-{suffix}",
            input_root=docs,
            index_state_dir=tmp_path / "indexes",
            collection_prefix=f"rag-verifier-{suffix}",
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
    control.create_job(
        idempotency_key=f"target-verifier:{suffix}",
        kind=JobKind.FULL,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    before_collections = _collection_names(client)
    calls = 0

    def reject_target(_verifier: TargetIndexVerifier) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("target verification rejected")

    monkeypatch.setattr(TargetIndexVerifier, "verify", reject_target)
    try:
        result = runner.run_next(worker_id="target-verifier-worker")
        created = _collection_names(client) - before_collections

        assert result is not None
        assert result.state == JobState.FAILED
        assert calls == 1
        assert len(created) == 1
        target = created.pop()
        assert client.list_snapshots(target) == []
        assert manifests.get(target) is None
    finally:
        for collection_name in _collection_names(client) - before_collections:
            client.delete_collection(collection_name)


def test_full_then_incremental_job_updates_manifest_and_zero_duplicates(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-job-active-{suffix}"
    collection_prefix = f"rag-job-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    index_state_dir = tmp_path / "indexes"
    index_state_dir.mkdir()
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
            index_state_dir=index_state_dir,
            collection_prefix=collection_prefix,
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
    try:
        full = control.create_job(
            idempotency_key=f"full:v1:{suffix}",
            kind=JobKind.FULL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        full_result = _require_succeeded(
            runner.run_next(worker_id="single-index-worker")
        )
        full_state = StateStore(
            _state_path(index_state_dir, full_result.collection_name)
        )
        original_sources = full_state.list_active_sources()
        assert control.get_job(full.job_id).state == JobState.SUCCEEDED
        assert client.count(full_result.collection_name, exact=True).count == 2
        _assert_idle_with_exact_count(
            runner,
            client,
            full_result.collection_name,
            2,
        )

        (docs / "甲.docx").rename(docs / "新甲.docx")
        (docs / "乙.docx").unlink()
        (docs / "丙.docx").write_bytes(b"third")
        incremental = control.create_job(
            idempotency_key=f"incremental:v2:{suffix}",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        result = _require_succeeded(
            runner.run_next(worker_id="single-index-worker")
        )
        assert result.collection_name != full_result.collection_name
        assert (
            control.get_job(incremental.job_id).state == JobState.SUCCEEDED
        ), result
        assert client.count(result.collection_name, exact=True).count == 3
        active = manifests.get_active()
        assert active is not None
        _assert_collection_can_rollback(
            client,
            alias=alias,
            old_collection=full_result.collection_name,
            target_manifest=active.manifest,
            expected_old_count=2,
        )
        assert full_state.list_active_sources() == original_sources

        assert active.manifest.collection_name == result.collection_name
        _assert_manifest_matches_active_records(client, active.manifest)
        assert manifests.count_revisions(result.collection_name) == 1
        assert _alias_target(client, alias) == result.collection_name
    finally:
        try:
            _delete_test_collections(client, collection_prefix)
        finally:
            client.close()


def test_incremental_failure_keeps_old_alias_manifest_qdrant_and_state(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-cow-failure-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "甲.docx").write_bytes(b"first")
    (docs / "乙.docx").write_bytes(b"second")
    control = StateStore(tmp_path / "control.sqlite3")
    control.initialize()
    manifests = ManifestRepository(tmp_path / "manifests.sqlite3")
    manifests.initialize()
    pipeline = _pipeline()
    config = JobRunnerConfig(
        alias_name=alias,
        input_root=docs,
        index_state_dir=tmp_path / "indexes",
        collection_prefix=f"rag-cow-failure-{suffix}",
        lease_seconds=60,
    )
    services = JobRunnerServices(
        control=control,
        manifests=manifests,
        qdrant=client,
        pipeline=pipeline,
        build_chunks_factory=lambda _: _build_chunks,
    )
    runner = IndexJobRunner(config=config, services=services)
    collections: set[str] = set()
    try:
        control.create_job(
            idempotency_key="cow:full",
            kind=JobKind.FULL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        full = runner.run_next(worker_id="cow-worker")
        assert full is not None
        collections.add(full.collection_name)
        old_manifest = manifests.get_active()
        assert old_manifest is not None
        old_alias_target, old_payloads, old_state, old_sources = (
            _published_state(
                client,
                alias,
                full.collection_name,
                _state_path(tmp_path / "indexes", full.collection_name),
            )
        )

        (docs / "甲.docx").rename(docs / "新甲.docx")
        (docs / "乙.docx").write_bytes(b"changed")

        def failing_builder(
            source_path: str,
            version: SourceVersion,
        ) -> tuple[IndexedChunk, ...]:
            if source_path == "乙.docx":
                raise RuntimeError("test-only second item failure")
            return _build_chunks(source_path, version)

        failing_runner = IndexJobRunner(
            config=config,
            services=JobRunnerServices(
                control=control,
                manifests=manifests,
                qdrant=client,
                pipeline=pipeline,
                build_chunks_factory=lambda _: failing_builder,
            ),
        )
        incremental = control.create_job(
            idempotency_key="cow:incremental:failure",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        before_collections = _collection_names(client)

        failed = failing_runner.run_next(worker_id="cow-worker")

        assert failed is not None
        assert failed.state == JobState.FAILED
        assert control.get_job(incremental.job_id).state == JobState.FAILED
        after_collections = _collection_names(client)
        collections.update(after_collections - before_collections)
        active = manifests.get_active()
        assert active == old_manifest
        assert _alias_target(client, alias) == old_alias_target
        assert _payloads(client, full.collection_name) == old_payloads
        assert old_state.list_active_sources() == old_sources
        collections.add(
            _assert_delete_succeeds_when_add_fails(
                config=config,
                services=services,
                old_collection=full.collection_name,
            )
        )
    finally:
        index = QdrantIndex(
            client,
            collection_name="unused-cleanup",
            dense_dimension=pipeline.embedding_dimension,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        index.delete_alias(alias)
        for collection in collections:
            if client.collection_exists(collection):
                client.delete_collection(collection)


def test_same_control_job_recovers_after_publish_before_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-control-recovery-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "甲.docx").write_bytes(b"first")
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
            collection_prefix=f"rag-control-recovery-{suffix}",
            lease_seconds=1,
        ),
        services=JobRunnerServices(
            control=control,
            manifests=manifests,
            qdrant=client,
            pipeline=pipeline,
            build_chunks_factory=lambda _: _build_chunks,
        ),
    )
    job = control.create_job(
        idempotency_key="full:publish-before-finish",
        kind=JobKind.FULL,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    original_finish = control.finish_job
    crashed = False

    def crash_once(
        *,
        job_id: str,
        worker_id: str,
        error_code: str | None,
    ) -> None:
        nonlocal crashed
        if error_code is None and not crashed:
            crashed = True
            raise SystemExit("test-only crash after publish")
        original_finish(
            job_id=job_id,
            worker_id=worker_id,
            error_code=error_code,
        )

    monkeypatch.setattr(control, "finish_job", crash_once)
    target = ""
    try:
        with pytest.raises(SystemExit, match="after publish"):
            runner.run_next(worker_id="recovery-worker")
        active = manifests.get_active()
        assert active is not None
        target = active.manifest.collection_name
        assert control.get_job(job.job_id).state == JobState.RUNNING
        control.renew_job_lease(
            job_id=job.job_id,
            worker_id="recovery-worker",
            now=datetime(2000, 1, 1, tzinfo=UTC),
            lease_seconds=1,
        )
        monkeypatch.setattr(control, "finish_job", original_finish)

        recovered = runner.run_next(worker_id="recovery-worker")

        assert recovered is not None
        assert recovered.collection_name == target
        assert recovered.state == JobState.SUCCEEDED
        assert control.get_job(job.job_id).state == JobState.SUCCEEDED
        assert manifests.count_revisions(target) == 1
        assert _alias_target(client, alias) == target
    finally:
        if target and client.collection_exists(target):
            client.delete_collection(target)


def test_control_and_local_heartbeats_cover_long_build(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-long-build-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "甲.docx").write_bytes(b"first")
    control = StateStore(tmp_path / "control.sqlite3")
    control.initialize()
    manifests = ManifestRepository(tmp_path / "manifests.sqlite3")
    manifests.initialize()
    pipeline = _pipeline()
    build_started = threading.Event()
    release_build = threading.Event()

    def blocking_builder(
        source_path: str,
        version: SourceVersion,
    ) -> tuple[IndexedChunk, ...]:
        build_started.set()
        assert release_build.wait(5)
        return _build_chunks(source_path, version)

    runner = IndexJobRunner(
        config=JobRunnerConfig(
            alias_name=alias,
            input_root=docs,
            index_state_dir=tmp_path / "indexes",
            collection_prefix=f"rag-long-build-{suffix}",
            lease_seconds=1,
        ),
        services=JobRunnerServices(
            control=control,
            manifests=manifests,
            qdrant=client,
            pipeline=pipeline,
            build_chunks_factory=lambda _: blocking_builder,
        ),
    )
    control.create_job(
        idempotency_key="full:long-build",
        kind=JobKind.FULL,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    results: list[JobRunResult | None] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            results.append(runner.run_next(worker_id="heartbeat-worker"))
        except BaseException as error:
            errors.append(error)

    worker_thread = threading.Thread(target=execute, daemon=False)
    worker_thread.start()
    target = ""
    try:
        assert build_started.wait(30)
        assert threading.Event().wait(1.2) is False
        assert (
            control.claim_next_job(
                worker_id="control-thief",
                now=datetime.now(UTC),
                lease_seconds=1,
            )
            is None
        )
        state_path = next((tmp_path / "indexes").glob("index-*.sqlite3"))
        local = StateStore(state_path)
        assert (
            local.claim_next_job(
                worker_id="local-thief",
                now=datetime.now(UTC),
                lease_seconds=1,
            )
            is None
        )
        release_build.set()
        worker_thread.join(60)
        assert not worker_thread.is_alive()
        assert errors == [] and len(results) == 1
        result = results[0]
        assert result is not None
        assert result.state == JobState.SUCCEEDED
        target = result.collection_name
        assert not any(
            thread.name.startswith("rag-lease-heartbeat-")
            for thread in threading.enumerate()
        )
    finally:
        release_build.set()
        worker_thread.join(60)
        if target and client.collection_exists(target):
            client.delete_collection(target)


def test_control_heartbeat_failure_returns_stable_lease_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-lease-lost-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "甲.docx"
    source.write_bytes(b"first")
    control = StateStore(tmp_path / "control.sqlite3")
    control.initialize()
    manifests = ManifestRepository(tmp_path / "manifests.sqlite3")
    manifests.initialize()
    pipeline = _pipeline()
    config = JobRunnerConfig(
        alias_name=alias,
        input_root=docs,
        index_state_dir=tmp_path / "indexes",
        collection_prefix=f"rag-lease-lost-{suffix}",
        lease_seconds=1,
    )
    services = JobRunnerServices(
        control=control,
        manifests=manifests,
        qdrant=client,
        pipeline=pipeline,
        build_chunks_factory=lambda _: _build_chunks,
    )
    runner = IndexJobRunner(config=config, services=services)
    control.create_job(
        idempotency_key="lease-lost:base",
        kind=JobKind.FULL,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    base = runner.run_next(worker_id="lease-owner")
    assert base is not None
    source.write_bytes(b"changed")
    job = control.create_job(
        idempotency_key="lease-lost:incremental",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    old_manifest = manifests.get_active()
    assert old_manifest is not None
    old_payloads = _payloads(client, base.collection_name)
    allow_failure = threading.Event()
    failure_seen = threading.Event()
    original_renew = control.renew_job_lease

    def fail_renewal(
        *,
        job_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> object:
        if allow_failure.is_set():
            failure_seen.set()
            raise sqlite3.OperationalError("test-only renewal failure")
        return original_renew(
            job_id=job_id,
            worker_id=worker_id,
            now=now,
            lease_seconds=lease_seconds,
        )

    def wait_for_failure(
        source_path: str,
        version: SourceVersion,
    ) -> tuple[IndexedChunk, ...]:
        allow_failure.set()
        assert failure_seen.wait(2)
        return _build_chunks(source_path, version)

    monkeypatch.setattr(control, "renew_job_lease", fail_renewal)
    failing_runner = IndexJobRunner(
        config=config,
        services=JobRunnerServices(
            control=control,
            manifests=manifests,
            qdrant=client,
            pipeline=pipeline,
            build_chunks_factory=lambda _: wait_for_failure,
        ),
    )
    before_collections = _collection_names(client)
    try:
        failed = failing_runner.run_next(worker_id="lease-owner")

        assert failed is not None
        assert failed.error_code == "LEASE_LOST"
        assert failed.state == JobState.FAILED
        assert control.get_job(job.job_id).error_code == "LEASE_LOST"
        assert manifests.get_active() == old_manifest
        assert _alias_target(client, alias) == base.collection_name
        assert _payloads(client, base.collection_name) == old_payloads
    finally:
        for collection in _collection_names(client) - before_collections:
            client.delete_collection(collection)
        if client.collection_exists(base.collection_name):
            client.delete_collection(base.collection_name)


def test_policy_change_rejects_incremental_and_requires_new_collection(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-policy-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.docx").write_bytes(b"content")
    control = StateStore(tmp_path / "control.sqlite3")
    control.initialize()
    manifests = ManifestRepository(tmp_path / "manifests.sqlite3")
    manifests.initialize()
    old_pipeline = _pipeline().model_copy(
        update={"corpus_policy_sha256": "a" * 64}
    )
    config = JobRunnerConfig(
        alias_name=alias,
        input_root=docs,
        index_state_dir=tmp_path / "indexes",
        collection_prefix=f"rag-policy-{suffix}",
        lease_seconds=60,
    )
    old_runner = IndexJobRunner(
        config=config,
        services=JobRunnerServices(
            control=control,
            manifests=manifests,
            qdrant=client,
            pipeline=old_pipeline,
            build_chunks_factory=lambda _: _build_chunks,
        ),
    )
    collections: set[str] = set()
    try:
        old_job = control.create_job(
            idempotency_key="policy:old:full",
            kind=JobKind.FULL,
            pipeline_fingerprint=old_pipeline.fingerprint(),
        )
        old_result = old_runner.run_next(worker_id="worker-old")
        assert old_result is not None
        assert control.get_job(old_job.job_id).state == JobState.SUCCEEDED
        collections.add(old_result.collection_name)
        old_count = client.count(
            old_result.collection_name,
            exact=True,
        ).count

        new_pipeline = old_pipeline.model_copy(
            update={"corpus_policy_sha256": "b" * 64}
        )
        new_runner = IndexJobRunner(
            config=config,
            services=JobRunnerServices(
                control=control,
                manifests=manifests,
                qdrant=client,
                pipeline=new_pipeline,
                build_chunks_factory=lambda _: _build_chunks,
            ),
        )
        incremental = control.create_job(
            idempotency_key="policy:new:incremental",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=new_pipeline.fingerprint(),
        )
        rejected = new_runner.run_next(worker_id="worker-new")
        assert rejected is not None
        assert rejected.state == JobState.FAILED
        assert (
            control.get_job(incremental.job_id).state
            == JobState.FAILED
        )
        assert client.count(
            old_result.collection_name,
            exact=True,
        ).count == old_count
        assert any(
            item.alias_name == alias
            and item.collection_name == old_result.collection_name
            for item in client.get_aliases().aliases
        )

        full = control.create_job(
            idempotency_key="policy:new:full",
            kind=JobKind.FULL,
            pipeline_fingerprint=new_pipeline.fingerprint(),
        )
        new_result = new_runner.run_next(worker_id="worker-new")
        assert new_result is not None
        assert control.get_job(full.job_id).state == JobState.SUCCEEDED
        collections.add(new_result.collection_name)
        assert new_result.collection_name != old_result.collection_name
        assert any(
            item.alias_name == alias
            and item.collection_name == new_result.collection_name
            for item in client.get_aliases().aliases
        )
    finally:
        for collection in collections:
            if client.collection_exists(collection):
                client.delete_collection(collection)


def test_worker_rejects_old_payload_schema_before_chunk_builder(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-schema-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.docx").write_bytes(b"content")
    control = StateStore(tmp_path / "control.sqlite3")
    control.initialize()
    manifests = ManifestRepository(tmp_path / "manifests.sqlite3")
    manifests.initialize()
    pipeline = _pipeline()
    config = JobRunnerConfig(
        alias_name=alias,
        input_root=docs,
        index_state_dir=tmp_path / "indexes",
        collection_prefix=f"rag-schema-{suffix}",
        lease_seconds=60,
    )
    initial_runner = IndexJobRunner(
        config=config,
        services=JobRunnerServices(
            control=control,
            manifests=manifests,
            qdrant=client,
            pipeline=pipeline,
            build_chunks_factory=lambda _: _build_chunks,
        ),
    )
    collection = ""
    try:
        control.create_job(
            idempotency_key="schema:full",
            kind=JobKind.FULL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        full_result = initial_runner.run_next(worker_id="worker-full")
        assert full_result is not None
        assert full_result.state == JobState.SUCCEEDED
        collection = full_result.collection_name
        client.update_collection(
            collection_name=collection,
            metadata={"payload_schema_version": "1"},
        )
        builder_calls: list[str] = []

        def forbidden_builder(_: StateStore) -> object:
            builder_calls.append("called")
            raise AssertionError("旧 payload schema 不得进入 chunk builder。")

        runner = IndexJobRunner(
            config=config,
            services=JobRunnerServices(
                control=control,
                manifests=manifests,
                qdrant=client,
                pipeline=pipeline,
                build_chunks_factory=forbidden_builder,
            ),
        )
        control.create_job(
            idempotency_key="schema:incremental",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )

        result = runner.run_next(worker_id="worker-incremental")

        assert result is not None
        assert result.state == JobState.FAILED
        assert result.error_code == "INDEX_VALUEERROR"
        assert builder_calls == []
    finally:
        if collection and client.collection_exists(collection):
            client.delete_collection(collection)


def test_full_rebuild_preserves_source_identity_and_soft_route(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-full-identity-active-{suffix}"
    docs = tmp_path / "docs"
    docs.mkdir()
    original = docs / "规范.docx"
    original.write_bytes(b"first")
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
            collection_prefix=f"rag-full-identity-{suffix}",
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
    collections: set[str] = set()
    try:
        control.create_job(
            idempotency_key="identity:full:first",
            kind=JobKind.FULL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        first = runner.run_next(worker_id="identity-worker")
        assert first is not None
        collections.add(first.collection_name)
        first_manifest = manifests.get_active()
        assert first_manifest is not None
        source_id = first_manifest.manifest.sources[0].source_id

        original.write_bytes(b"changed")
        control.create_job(
            idempotency_key="identity:full:update",
            kind=JobKind.FULL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        updated = runner.run_next(worker_id="identity-worker")
        assert updated is not None
        collections.add(updated.collection_name)
        updated_manifest = manifests.get_active()
        assert updated_manifest is not None
        assert updated_manifest.manifest.sources[0].source_id == source_id

        renamed = docs / "新规范.docx"
        original.rename(renamed)
        control.create_job(
            idempotency_key="identity:full:rename",
            kind=JobKind.FULL,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        rebuilt = runner.run_next(worker_id="identity-worker")
        assert rebuilt is not None
        collections.add(rebuilt.collection_name)
        rebuilt_manifest = manifests.get_active()
        assert rebuilt_manifest is not None
        assert rebuilt_manifest.manifest.sources[0].source_id == source_id
        assert rebuilt_manifest.manifest.sources[0].current_path == (
            "新规范.docx"
        )

        router = KeywordSoftRouter(
            (
                KeywordRouteRule(
                    route_id="stable-source",
                    keywords=("规范",),
                    source_ids=(source_id,),
                ),
            ),
            minimum_confidence=1.0,
        )
        decision = router.route("规范")
        assert decision.source_ids == (source_id,)
        assert source_id in {
            source.source_id
            for source in rebuilt_manifest.manifest.sources
        }
    finally:
        index = QdrantIndex(
            client,
            collection_name="unused-cleanup",
            dense_dimension=pipeline.embedding_dimension,
            pipeline_fingerprint=pipeline.fingerprint(),
        )
        index.delete_alias(alias)
        for collection in collections:
            if client.collection_exists(collection):
                client.delete_collection(collection)
