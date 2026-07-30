from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from rag_app import cli
from rag_app.contracts import IndexManifest, PipelineSpec
from rag_app.index.gc import (
    GarbageCollectorConfig,
    GarbageItemKind,
    IndexGarbageCollector,
)
from rag_app.index.qdrant import QdrantIndex
from rag_app.manifest import (
    ManifestRepository,
    ReadOnlyManifestRepository,
)
from rag_app.state import JobKind, JobState, StateStore
from rag_app.state.jobs import ReadOnlyJobStore
from rag_app.state.models import CollectionStateIdentity

_API_KEY = "test-only-qdrant-key"


@dataclass(frozen=True, slots=True)
class _GcScenario:
    collector: IndexGarbageCollector
    client: QdrantClient
    control: StateStore
    manifests: ManifestRepository
    alias: str
    active: str
    retired_kept: tuple[str, str]
    retired_expired: str
    failed: str
    orphan: str
    unknown: str
    state_dir: Path
    extra_snapshot: str
    collections: tuple[str, ...]


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


def _manifest(
    collection_name: str,
    pipeline: PipelineSpec,
    hour: int,
) -> IndexManifest:
    return IndexManifest(
        manifest_version="1",
        collection_name=collection_name,
        created_at=datetime(2026, 7, 30, hour, tzinfo=UTC),
        pipeline=pipeline,
        pipeline_fingerprint=pipeline.fingerprint(),
        sources=(),
    )


def _state_path(state_dir: Path, collection_name: str) -> Path:
    digest = hashlib.sha256(collection_name.encode()).hexdigest()[:24]
    return state_dir / f"index-{digest}.sqlite3"


def _target_name(prefix: str, fingerprint: str, job_id: str) -> str:
    return (
        f"{prefix}-"
        f"{fingerprint.removeprefix('sha256:')[:12]}-"
        f"{job_id.removeprefix('job_')[:12]}"
    )


def _create_state(
    state_dir: Path,
    collection_name: str,
    identity: CollectionStateIdentity,
) -> None:
    state = StateStore(_state_path(state_dir, collection_name))
    state.initialize()
    state.bind_collection_identity(
        control_job_id=identity.control_job_id,
        pipeline_fingerprint=identity.pipeline_fingerprint,
        base_manifest_sha256=identity.base_manifest_sha256,
    )


def _create_index(
    client: QdrantClient,
    *,
    collection_name: str,
    pipeline: PipelineSpec,
    control_job_id: str,
) -> QdrantIndex:
    index = QdrantIndex(
        client,
        collection_name=collection_name,
        dense_dimension=pipeline.embedding_dimension,
        pipeline_fingerprint=pipeline.fingerprint(),
        index_revision=pipeline.index_revision,
    )
    index.prepare_staging_collection(
        control_job_id=control_job_id,
        base_manifest_sha256=None,
    )
    return index


def _scenario(tmp_path: Path) -> _GcScenario:
    client = _client()
    pipeline = _pipeline()
    fingerprint = pipeline.fingerprint()
    suffix = uuid.uuid4().hex
    prefix = f"rag-gc-{suffix}"
    alias = f"rag-gc-active-{suffix}"
    state_dir = tmp_path / "indexes"
    control_path = tmp_path / "control.sqlite3"
    control = StateStore(control_path)
    control.initialize()
    manifest_path = tmp_path / "manifests.sqlite3"
    manifests = ManifestRepository(manifest_path)
    manifests.initialize()
    published_names = tuple(
        f"{prefix}-published-{ordinal}" for ordinal in range(4)
    )
    published_indexes: list[QdrantIndex] = []
    for ordinal, collection_name in enumerate(published_names):
        job_id = "job_" + f"{ordinal + 1:032x}"
        identity = CollectionStateIdentity(
            control_job_id=job_id,
            pipeline_fingerprint=fingerprint,
            base_manifest_sha256=None,
        )
        index = _create_index(
            client,
            collection_name=collection_name,
            pipeline=pipeline,
            control_job_id=job_id,
        )
        _create_state(state_dir, collection_name, identity)
        snapshot = index.create_snapshot()
        assert snapshot.checksum is not None
        manifests.stage(
            _manifest(collection_name, pipeline, ordinal),
            snapshot_name=snapshot.name,
            snapshot_checksum=snapshot.checksum,
        )
        index.switch_alias(alias)
        manifests.activate(collection_name)
        published_indexes.append(index)
    active = published_names[-1]
    extra_snapshot = published_indexes[-1].create_snapshot().name

    failed_job = control.create_job(
        idempotency_key=f"gc-failed:{suffix}",
        kind=JobKind.FULL,
        pipeline_fingerprint=fingerprint,
    )
    claimed = control.claim_next_job(
        worker_id="gc-test-worker",
        now=datetime.now(UTC),
        lease_seconds=60,
    )
    assert claimed is not None
    assert claimed.job_id == failed_job.job_id
    control.finish_job(
        job_id=failed_job.job_id,
        worker_id="gc-test-worker",
        error_code="TEST_FAILED",
    )
    failed = _target_name(prefix, fingerprint, failed_job.job_id)
    failed_identity = CollectionStateIdentity(
        control_job_id=failed_job.job_id,
        pipeline_fingerprint=fingerprint,
        base_manifest_sha256=None,
    )
    _create_index(
        client,
        collection_name=failed,
        pipeline=pipeline,
        control_job_id=failed_job.job_id,
    )
    _create_state(state_dir, failed, failed_identity)

    orphan_job_id = "job_" + uuid.uuid4().hex
    orphan = _target_name(prefix, fingerprint, orphan_job_id)
    orphan_identity = CollectionStateIdentity(
        control_job_id=orphan_job_id,
        pipeline_fingerprint=fingerprint,
        base_manifest_sha256=None,
    )
    _create_index(
        client,
        collection_name=orphan,
        pipeline=pipeline,
        control_job_id=orphan_job_id,
    )
    _create_state(state_dir, orphan, orphan_identity)

    unknown = f"foreign-collection-{suffix}"
    _create_index(
        client,
        collection_name=unknown,
        pipeline=pipeline,
        control_job_id="job_" + uuid.uuid4().hex,
    )
    collections = (*published_names, failed, orphan, unknown)
    collector = IndexGarbageCollector(
        client=client,
        manifests=ReadOnlyManifestRepository(manifest_path),
        control=ReadOnlyJobStore(control_path),
        config=GarbageCollectorConfig(
            alias_name=alias,
            index_state_dir=state_dir,
            collection_prefix=prefix,
            dense_dimension=pipeline.embedding_dimension,
            pipeline_fingerprint=fingerprint,
            index_revision=pipeline.index_revision,
        ),
    )
    return _GcScenario(
        collector=collector,
        client=client,
        control=control,
        manifests=manifests,
        alias=alias,
        active=active,
        retired_kept=(published_names[2], published_names[1]),
        retired_expired=published_names[0],
        failed=failed,
        orphan=orphan,
        unknown=unknown,
        state_dir=state_dir,
        extra_snapshot=extra_snapshot,
        collections=collections,
    )


def _cleanup(scenario: _GcScenario) -> None:
    for collection_name in scenario.collections:
        if scenario.client.collection_exists(collection_name):
            scenario.client.delete_collection(collection_name)


def test_index_gc_dry_run_preserves_active_rollback_and_unknown(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)
    try:
        plan = scenario.collector.plan()
        stable_ids = {item.stable_id for item in plan.items}

        assert f"collection:{scenario.retired_expired}" in stable_ids
        assert f"collection:{scenario.failed}" in stable_ids
        assert f"collection:{scenario.orphan}" in stable_ids
        assert f"state:{scenario.failed}" in stable_ids
        assert f"state:{scenario.orphan}" in stable_ids
        assert (
            f"snapshot:{scenario.active}:{scenario.extra_snapshot}"
            in stable_ids
        )
        for collection_name in (
            scenario.active,
            *scenario.retired_kept,
            scenario.unknown,
        ):
            assert f"collection:{collection_name}" not in stable_ids
            assert scenario.client.collection_exists(collection_name)
        assert all(
            item.kind
            in {
                GarbageItemKind.COLLECTION,
                GarbageItemKind.STATE,
                GarbageItemKind.SNAPSHOT,
            }
            for item in plan.items
        )
        assert scenario.client.collection_exists(scenario.failed)
        assert _state_path(
            scenario.state_dir,
            scenario.failed,
        ).is_file()
    finally:
        _cleanup(scenario)


def test_index_gc_apply_is_idempotent_and_checks_control_drift(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)
    try:
        plan = scenario.collector.plan()
        first = scenario.collector.apply(plan)
        second = scenario.collector.apply(plan)

        assert all(result.status == "deleted" for result in first.results)
        assert all(
            result.status == "already_absent"
            for result in second.results
        )
        assert not scenario.client.collection_exists(scenario.failed)
        assert not _state_path(
            scenario.state_dir,
            scenario.failed,
        ).exists()
        assert scenario.client.collection_exists(scenario.active)
        assert scenario.client.collection_exists(scenario.retired_kept[0])
        assert scenario.client.collection_exists(scenario.retired_kept[1])
        assert scenario.client.collection_exists(scenario.unknown)

        repeated_plan = scenario.collector.plan()
        assert repeated_plan.items == ()
    finally:
        _cleanup(scenario)


def test_index_gc_refuses_pending_jobs_and_alias_race(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    try:
        pending = scenario.control.create_job(
            idempotency_key="gc-pending",
            kind=JobKind.FULL,
            pipeline_fingerprint=_pipeline().fingerprint(),
        )
        assert pending.state == JobState.PENDING
        with pytest.raises(RuntimeError, match="pending 或 running"):
            scenario.collector.plan()

        claimed = scenario.control.claim_next_job(
            worker_id="gc-pending-worker",
            now=datetime.now(UTC),
            lease_seconds=60,
        )
        assert claimed is not None
        scenario.control.finish_job(
            job_id=claimed.job_id,
            worker_id="gc-pending-worker",
            error_code="TEST_DONE",
        )
        plan = scenario.collector.plan()
        QdrantIndex(
            scenario.client,
            collection_name=scenario.unknown,
            dense_dimension=3,
            pipeline_fingerprint=_pipeline().fingerprint(),
            index_revision=_pipeline().index_revision,
        ).switch_alias(scenario.alias)

        with pytest.raises(RuntimeError, match="GC_CONTROL_DRIFT"):
            scenario.collector.apply(plan)
        assert scenario.client.collection_exists(scenario.failed)
    finally:
        _cleanup(scenario)


def test_index_gc_does_not_delete_state_after_collection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(tmp_path)
    original_delete = scenario.client.delete_collection
    try:
        plan = scenario.collector.plan()

        def fail_one_collection(
            collection_name: str,
            timeout: int | None = None,
            **kwargs: object,
        ) -> bool:
            if collection_name == scenario.failed:
                raise OSError("test delete failure")
            return original_delete(
                collection_name,
                timeout=timeout,
                **kwargs,
            )

        monkeypatch.setattr(
            scenario.client,
            "delete_collection",
            fail_one_collection,
        )
        report = scenario.collector.apply(plan)
        statuses = {
            result.stable_id: result.status for result in report.results
        }

        assert statuses[f"collection:{scenario.failed}"] == "delete_failed"
        assert statuses[f"state:{scenario.failed}"] == "still_referenced"
        assert _state_path(scenario.state_dir, scenario.failed).is_file()

        monkeypatch.setattr(
            scenario.client,
            "delete_collection",
            original_delete,
        )
        retried = scenario.collector.apply(plan)
        retried_statuses = {
            result.stable_id: result.status for result in retried.results
        }
        assert retried_statuses[f"collection:{scenario.failed}"] == "deleted"
        assert retried_statuses[f"state:{scenario.failed}"] == "deleted"
    finally:
        monkeypatch.setattr(
            scenario.client,
            "delete_collection",
            original_delete,
        )
        _cleanup(scenario)


def test_index_gc_cli_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, str]] = []

    def record_gc(*, apply: bool, collection_prefix: str) -> int:
        calls.append((apply, collection_prefix))
        return 0

    monkeypatch.setattr(cli, "_run_index_gc", record_gc)

    assert cli.main(["index-gc"]) == 0
    assert cli.main(
        ["index-gc", "--apply", "--collection-prefix", "custom"]
    ) == 0
    assert calls == [(False, "rag-docx"), (True, "custom")]
