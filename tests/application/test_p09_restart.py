from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

from rag_app.composition.p09_runtime import build_p09_runtime
from rag_app.core.models import Job
from tests.adapters.parsers.docx.fixtures import build_package

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _wait(runtime: object, job: Job) -> Job:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = runtime.sdk.get_job(job.job_id)  # type: ignore[attr-defined]
        if current.state.value not in {"queued", "running"}:
            return current
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内结束：{job.job_id}")


def test_job_and_revision_status_survive_runtime_restart(
    tmp_path: Path,
) -> None:
    content = build_package("<w:p><w:r><w:t>重启后仍可恢复</w:t></w:r></w:p>")
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("项目")
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id,
            "知识库",
        )
        job = _wait(
            runtime,
            runtime.sdk.create_document(
                project.project_id,
                knowledge_base.knowledge_base_id,
                display_name="恢复.docx",
                content=content,
                media_type=_MEDIA_TYPE,
                idempotency_key="restart-job",
            ),
        )
        second_job = _wait(
            runtime,
            runtime.sdk.create_document_version(
                project.project_id,
                knowledge_base.knowledge_base_id,
                job.document_id,
                content=build_package(
                    "<w:p><w:r><w:t>重启后的新版本</w:t></w:r></w:p>"
                ),
                media_type=_MEDIA_TYPE,
                idempotency_key="restart-job-v2",
            ),
        )
        plan = runtime.retrieval_runtime.persistence.garbage_collector.plan(
            protected_retired_count=0,
            grace_before=(datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        )
        runtime.sdk.delete_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            job.document_id,
        )

    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as reopened:
        recovered = reopened.sdk.get_job(second_job.job_id)
        document = reopened.sdk.get_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            job.document_id,
        )

        assert recovered.state.value == "succeeded"
        assert recovered.revision_id == second_job.revision_id
        assert document.active_index_revision_id == second_job.revision_id
        assert document.status.value == "deleting"
        assert reopened.sdk.health().pending_gc_items > 0
        assert reopened.control.gc_plan_items(plan.plan_id)
        assert (
            reopened.control.revision_lease(second_job.revision_id) is not None
        )


def test_queued_job_is_recovered_and_completed_after_restart(
    tmp_path: Path,
) -> None:
    runtime = build_p09_runtime(_PROFILE, data_dir=tmp_path)
    project = runtime.sdk.create_project("恢复项目")
    knowledge_base = runtime.sdk.create_knowledge_base(
        project.project_id, "恢复知识库"
    )
    queued = runtime.lifecycle.create_document(
        project.project_id,
        knowledge_base.knowledge_base_id,
        display_name="queued.docx",
        content=build_package(
            "<w:p><w:r><w:t>跨重启持久队列</w:t></w:r></w:p>"
        ),
        media_type=_MEDIA_TYPE,
        idempotency_key="queued-before-restart",
    )
    assert queued.state.value == "queued"
    runtime.close()

    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as reopened:
        recovered = _wait(reopened, queued)
        document = reopened.sdk.get_document(
            project.project_id,
            knowledge_base.knowledge_base_id,
            str(recovered.document_id),
        )
        assert recovered.state.value == "succeeded"
        assert document.current_version_id == recovered.document_version_id
