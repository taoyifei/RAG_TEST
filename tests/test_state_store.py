import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.state import JobKind, JobState, OcrResult, StateStore, VersionState
from rag_app.state.models import CollectionStateIdentity

_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def test_state_store_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)

    pragmas = store.pragmas()

    assert pragmas["journal_mode"] == "wal"
    assert pragmas["foreign_keys"] == 1
    assert pragmas["synchronous"] == 2


def test_job_idempotency_returns_original_job(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = store.create_job(
        idempotency_key="incremental:input-sha",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    duplicate = store.create_job(
        idempotency_key="incremental:input-sha",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )

    assert duplicate == first
    assert store.count_jobs() == 1


def test_expired_running_job_is_reclaimed_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = store.create_job(
        idempotency_key="full:manifest-sha",
        kind=JobKind.FULL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    started_at = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
    first_claim = store.claim_next_job(
        worker_id="worker-before-crash",
        now=started_at,
        lease_seconds=30,
    )

    assert first_claim is not None
    assert first_claim.job_id == job.job_id
    assert first_claim.state == JobState.RUNNING
    assert (
        store.claim_next_job(
            worker_id="other-worker",
            now=started_at + timedelta(seconds=20),
            lease_seconds=30,
        )
        is None
    )

    reopened = StateStore(store.path)
    reopened.initialize()
    recovered = reopened.claim_next_job(
        worker_id="worker-after-restart",
        now=started_at + timedelta(seconds=31),
        lease_seconds=30,
    )

    assert recovered is not None
    assert recovered.job_id == job.job_id
    assert recovered.lease_owner == "worker-after-restart"
    assert recovered.attempt == 2


def test_failed_staging_version_preserves_active_version(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job = store.create_job(
        idempotency_key="incremental:first",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    first = store.stage_source_version(
        job_id=job.job_id,
        source_path="规范.docx",
        content_sha256="a" * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    store.activate_source_version(first.source_id, first.doc_version)
    update_job = store.create_job(
        idempotency_key="incremental:second",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    update = store.stage_source_version(
        job_id=update_job.job_id,
        source_path="规范.docx",
        content_sha256="b" * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    store.fail_source_version(
        update.source_id,
        update.doc_version,
        error_code="EMBEDDING_TIMEOUT",
    )

    active = store.get_active_source(first.source_id)
    failed = store.get_source_version(update.source_id, update.doc_version)

    assert active is not None
    assert active.doc_version == first.doc_version
    assert failed.state == VersionState.FAILED
    assert failed.error_code == "EMBEDDING_TIMEOUT"


def test_unique_content_hash_recognizes_rename_without_new_source(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job = store.create_job(
        idempotency_key="incremental:rename-base",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    staged = store.stage_source_version(
        job_id=job.job_id,
        source_path="旧名称.docx",
        content_sha256="a" * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    store.activate_source_version(staged.source_id, staged.doc_version)

    renamed_source_id = store.apply_rename_if_unique(
        new_path="新名称.docx",
        content_sha256="a" * 64,
    )

    assert renamed_source_id == staged.source_id
    active = store.get_active_source(staged.source_id)
    assert active is not None
    assert active.current_path == "新名称.docx"


def test_ocr_cache_key_includes_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_ocr_result(
        OcrResult(
            media_sha256="a" * 64,
            ocr_revision="ppocr-v5-rev-a",
            state="succeeded",
            text="识别结果",
            confidence=0.98,
            error_code=None,
        )
    )

    assert (
        store.get_ocr_result("a" * 64, "ppocr-v5-rev-a") is not None
    )
    assert store.get_ocr_result("a" * 64, "ppocr-v5-rev-b") is None


def test_same_content_cannot_silently_reuse_another_pipeline(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_job = store.create_job(
        idempotency_key="incremental:pipeline-a",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    first = store.stage_source_version(
        job_id=first_job.job_id,
        source_path="规范.docx",
        content_sha256="a" * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    store.activate_source_version(first.source_id, first.doc_version)
    changed_pipeline = "sha256:" + "e" * 64
    second_job = store.create_job(
        idempotency_key="full:pipeline-b",
        kind=JobKind.FULL,
        pipeline_fingerprint=changed_pipeline,
    )

    with pytest.raises(ValueError, match="新 collection"):
        store.stage_source_version(
            job_id=second_job.job_id,
            source_path="规范.docx",
            content_sha256="a" * 64,
            pipeline_fingerprint=changed_pipeline,
        )

    active = store.get_source_version(first.source_id, first.doc_version)
    assert active.pipeline_fingerprint == _PIPELINE_FINGERPRINT
    assert active.state == VersionState.ACTIVE


def test_job_lease_renewal_and_terminal_state_require_owner(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job = store.create_job(
        idempotency_key="incremental:lease-owner",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    now = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    claimed = store.claim_next_job(
        worker_id="worker-a",
        now=now,
        lease_seconds=30,
    )
    assert claimed is not None

    renewed = store.renew_job_lease(
        job_id=job.job_id,
        worker_id="worker-a",
        now=now + timedelta(seconds=10),
        lease_seconds=30,
    )
    assert renewed.lease_expires_at == now + timedelta(seconds=40)

    with pytest.raises(LookupError, match="租约"):
        store.finish_job(
            job_id=job.job_id,
            worker_id="worker-b",
            error_code=None,
        )

    store.finish_job(
        job_id=job.job_id,
        worker_id="worker-a",
        error_code=None,
    )
    finished = store.get_job(job.job_id)
    assert finished.state == JobState.SUCCEEDED
    assert finished.lease_owner is None


def test_source_id_hint_is_used_once_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job = store.create_job(
        idempotency_key="full:source-hint",
        kind=JobKind.FULL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    hinted_source_id = "src_" + "a" * 32

    staged = store.stage_source_version(
        job_id=job.job_id,
        source_path="规范.docx",
        content_sha256="a" * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        source_id_hint=hinted_source_id,
    )

    assert staged.source_id == hinted_source_id
    store.activate_source_version(staged.source_id, staged.doc_version)
    second_job = store.create_job(
        idempotency_key="full:source-hint-conflict",
        kind=JobKind.FULL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    with pytest.raises(ValueError, match="source ID hint"):
        store.stage_source_version(
            job_id=second_job.job_id,
            source_path="其他.docx",
            content_sha256="b" * 64,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
            source_id_hint=hinted_source_id,
        )


def test_collection_state_clone_is_consistent_and_job_bound(
    tmp_path: Path,
) -> None:
    source = _store(tmp_path / "source")
    job = source.create_job(
        idempotency_key="clone:base",
        kind=JobKind.FULL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    version = source.stage_source_version(
        job_id=job.job_id,
        source_path="规范.docx",
        content_sha256="a" * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    source.activate_source_version(version.source_id, version.doc_version)
    expected = source.list_active_sources()
    target_path = tmp_path / "target.sqlite3"
    identity = CollectionStateIdentity(
        control_job_id="job_clone_target",
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        base_manifest_sha256="b" * 64,
    )
    writer = sqlite3.connect(source.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            """
            INSERT INTO sources (
                source_id, current_path, state, updated_at
            ) VALUES (?, ?, 'staging', ?)
            """,
            (
                "src_" + "f" * 32,
                "未提交.docx",
                datetime.now(UTC).isoformat(),
            ),
        )

        target = StateStore.clone_collection_state(
            source_path=source.path,
            target_path=target_path,
            identity=identity,
            expected_sources=expected,
        )
    finally:
        writer.rollback()
        writer.close()

    assert target.list_active_sources() == expected
    target.require_collection_identity(
        control_job_id=identity.control_job_id,
        pipeline_fingerprint=identity.pipeline_fingerprint,
        base_manifest_sha256=identity.base_manifest_sha256,
    )
    repeated = StateStore.clone_collection_state(
        source_path=source.path,
        target_path=target_path,
        identity=identity,
        expected_sources=expected,
    )
    assert repeated.list_active_sources() == expected
    with sqlite3.connect(target_path) as connection:
        connection.execute(
            """
            UPDATE sources SET current_path = ?
            WHERE source_id = ?
            """,
            ("drifted.docx", expected[0].source_id),
        )
    with pytest.raises(RuntimeError, match="活动来源"):
        StateStore.clone_collection_state(
            source_path=source.path,
            target_path=target_path,
            identity=identity,
            expected_sources=expected,
        )
    with pytest.raises(ValueError, match="staging 身份"):
        StateStore.clone_collection_state(
            source_path=source.path,
            target_path=target_path,
            identity=CollectionStateIdentity(
                control_job_id="job_other",
                pipeline_fingerprint=_PIPELINE_FINGERPRINT,
                base_manifest_sha256="b" * 64,
            ),
            expected_sources=expected,
        )
