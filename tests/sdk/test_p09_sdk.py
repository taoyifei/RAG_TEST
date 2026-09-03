from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep

import pytest

from rag_app.composition.p09_runtime import build_p09_runtime
from rag_app.core.errors import (
    CapabilityUnavailable,
    Conflict,
    QueueLimitExceeded,
)
from rag_app.core.models import Job
from tests.adapters.parsers.docx.fixtures import build_package

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _document(text: str) -> bytes:
    return build_package(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")


def _wait(runtime: object, job: Job) -> Job:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = runtime.sdk.get_job(job.job_id)  # type: ignore[attr-defined]
        if current.state.value not in {"queued", "running"}:
            return current
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内结束：{job.job_id}")


def test_sdk_keeps_document_version_and_rename_semantics(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("研发项目")
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id,
            "制度库",
        )
        first = _wait(
            runtime,
            runtime.sdk.create_document(
                project.project_id,
                knowledge_base.knowledge_base_id,
                display_name="制度.docx",
                content=_document("普通中文短语 财务制度"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="create-first",
            ),
        )
        before = runtime.sdk.get_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            first.document_id,
        )
        renamed = runtime.sdk.rename_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            first.document_id,
            display_name="财务制度.docx",
        )

        assert first.state.value == "succeeded"
        assert renamed.current_version_id == before.current_version_id
        assert (
            renamed.active_index_revision_id == before.active_index_revision_id
        )

        repeated = _wait(
            runtime,
            runtime.sdk.create_document_version(
                project.project_id,
                knowledge_base.knowledge_base_id,
                first.document_id,
                content=_document("普通中文短语 财务制度"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="same-revision-different-key",
            ),
        )
        assert repeated.job_id == first.job_id
        assert runtime.control.running_writer_count(first.revision_id) <= 1


def test_same_bytes_create_distinct_documents_and_reuse_artifact(
    tmp_path: Path,
) -> None:
    content = _document("共享字节但逻辑文档不同")
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("项目")
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id,
            "知识库",
        )
        first = _wait(
            runtime,
            runtime.sdk.create_document(
                project.project_id,
                knowledge_base.knowledge_base_id,
                display_name="一.docx",
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="first-document",
            ),
        )
        second = _wait(
            runtime,
            runtime.sdk.create_document(
                project.project_id,
                knowledge_base.knowledge_base_id,
                display_name="二.docx",
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="second-document",
            ),
        )
        first_version = runtime.sdk.list_document_versions(
            project.project_id,
            knowledge_base.knowledge_base_id,
            first.document_id,
        )[0]
        second_version = runtime.sdk.list_document_versions(
            project.project_id,
            knowledge_base.knowledge_base_id,
            second.document_id,
        )[0]

        assert first.document_id != second.document_id
        assert (
            first_version.document_version_id
            != second_version.document_version_id
        )
        assert (
            first_version.source_artifact_id
            == second_version.source_artifact_id
        )
        runtime.sdk.delete_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            str(first.document_id),
        )
        reused = runtime.sdk.read_artifact(
            project.project_id,
            knowledge_base.knowledge_base_id,
            str(second.document_id),
            second_version.document_version_id,
            second_version.source_artifact_id,
        )
        assert reused.content == content


def test_sdk_idempotency_conflict_and_close_are_stable(tmp_path: Path) -> None:
    runtime = build_p09_runtime(_PROFILE, data_dir=tmp_path)
    first = runtime.sdk.create_project("项目", idempotency_key="stable-project")
    repeated = runtime.sdk.create_project(
        "项目", idempotency_key="stable-project"
    )
    assert repeated.project_id == first.project_id
    with pytest.raises(Conflict, match="Idempotency-Key"):
        runtime.sdk.create_project(
            "另一个项目", idempotency_key="stable-project"
        )

    runtime.sdk.close()
    runtime.sdk.close()
    with pytest.raises(CapabilityUnavailable, match="SDK 已关闭"):
        runtime.sdk.list_projects()


def test_queued_job_can_be_cancelled_durably(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("取消项目")
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id, "取消知识库"
        )
        queued = runtime.lifecycle.create_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            display_name="cancel.docx",
            content=_document("等待取消"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="cancel-before-submit",
        )
        cancelled = runtime.sdk.cancel_job(queued.job_id)

        assert cancelled.state.value == "cancelled"
        assert runtime.store.pending_ingestion_jobs() == ()


def test_two_idempotency_keys_share_one_revision_writer(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("并发项目")
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id, "并发知识库"
        )
        baseline = _wait(
            runtime,
            runtime.sdk.create_document(
                project.project_id,
                knowledge_base.knowledge_base_id,
                display_name="baseline.docx",
                content=_document("初始版本"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="baseline",
            ),
        )

        def submit(key: str) -> Job:
            return runtime.sdk.create_document_version(
                project.project_id,
                knowledge_base.knowledge_base_id,
                str(baseline.document_id),
                content=_document("并发版本"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key=key,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = tuple(executor.map(submit, ("key-a", "key-b")))
        completed = _wait(runtime, first)

        assert first.job_id == second.job_id
        assert first.revision_id == second.revision_id
        assert completed.state.value == "succeeded"
        assert runtime.control.running_writer_count(first.revision_id) <= 1


def test_queue_limit_and_delete_cancel_are_persistent(tmp_path: Path) -> None:
    runtime = build_p09_runtime(
        _PROFILE,
        data_dir=tmp_path,
        max_pending_jobs=1,
    )
    project = runtime.sdk.create_project("队列项目")
    knowledge_base = runtime.sdk.create_knowledge_base(
        project.project_id, "队列知识库"
    )
    queued = runtime.lifecycle.create_document(
        project.project_id,
        knowledge_base.knowledge_base_id,
        display_name="queued.docx",
        content=_document("待取消作业"),
        media_type=_DOCX_MEDIA_TYPE,
        idempotency_key="queued-one",
    )
    with pytest.raises(QueueLimitExceeded, match="队列已满"):
        runtime.lifecycle.create_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            display_name="overflow.docx",
            content=_document("超过队列容量"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="queued-two",
        )

    deleted = runtime.sdk.delete_document(
        project.project_id,
        knowledge_base.knowledge_base_id,
        str(queued.document_id),
    )
    cancelled = runtime.sdk.get_job(queued.job_id)
    with runtime.retrieval_runtime.persistence.connections.transaction() as db:
        operation = db.execute(
            "SELECT state FROM lifecycle_operations WHERE document_id=?",
            (queued.document_id,),
        ).fetchone()

    assert deleted.status.value == "deleting"
    assert cancelled.state.value == "cancelled"
    assert runtime.store.pending_ingestion_jobs() == ()
    assert operation is not None and operation["state"] == "planned"
    runtime.close()


def test_retry_uses_persisted_attempt_and_completes(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("重试项目")
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id, "重试知识库"
        )
        failed = runtime.lifecycle.create_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            display_name="retry.docx",
            content=_document("持久重试"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="retry-once",
        )
        with runtime.retrieval_runtime.persistence.connections.transaction(
            write=True
        ) as db:
            db.execute(
                "UPDATE ingestion_jobs SET state='failed_retryable', "
                "stage='failed', attempt=1, retryable=1 WHERE job_id=?",
                (failed.job_id,),
            )
            db.execute(
                "UPDATE ingestion_requests SET state='failed' WHERE job_id=?",
                (failed.job_id,),
            )

        completed = _wait(runtime, runtime.sdk.retry_job(failed.job_id))

        assert completed.state.value == "succeeded"
        assert completed.attempt == 2
