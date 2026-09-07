"""连续提交与并发 Worker 不得丢失知识库的既有成员。"""

from pathlib import Path

import pytest

from rag_app.composition.p09_runtime import P09RuntimeHooks, build_p09_runtime
from tests.sdk.test_p09_sdk import _DOCX_MEDIA_TYPE, _PROFILE, _document, _wait


@pytest.mark.parametrize("max_workers", [1, 3])
def test_queued_uploads_merge_current_members_when_claimed(
    tmp_path: Path, max_workers: int
) -> None:
    with build_p09_runtime(
        _PROFILE, data_dir=tmp_path, max_job_workers=max_workers
    ) as runtime:
        project = runtime.sdk.create_project("并行项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "并行库")
        scope = (project.project_id, kb.knowledge_base_id)
        # 故意在 Worker 启动前冻结三个旧快照，稳定复现快速连续上传。
        jobs = [
            runtime.lifecycle.create_document(
                *scope,
                display_name=f"资料{number}.docx",
                content=_document(f"合成信息条目{number}"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key=f"upload-{number}",
            )
            for number in range(3)
        ]
        for job in jobs:
            runtime.jobs.submit(job.job_id)
        completed = [_wait(runtime, job) for job in jobs]
        assert [job.state.value for job in completed] == ["succeeded"] * 3
        active = runtime.control.active_documents(kb.knowledge_base_id)
        assert {item.document_id for item, _, _ in active} == {
            job.document_id for job in jobs
        }
        assert all(
            doc.active_index_revision_id is not None
            for doc in runtime.sdk.list_documents(*scope)
        )
        repeated = runtime.sdk.create_document_version(
            *scope,
            str(jobs[-1].document_id),
            content=_document("合成信息条目2"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="same-version-after-merge",
        )
        assert repeated.job_id == completed[-1].job_id
        assert repeated.state.value == "succeeded"


def test_two_runtimes_preserve_members_and_durable_kb_exclusion(
    tmp_path: Path,
) -> None:
    hooks = P09RuntimeHooks(recover_jobs=False)
    with (
        build_p09_runtime(_PROFILE, data_dir=tmp_path, hooks=hooks) as first,
        build_p09_runtime(_PROFILE, data_dir=tmp_path, hooks=hooks) as second,
    ):
        project = first.sdk.create_project("跨实例项目")
        kb = first.sdk.create_knowledge_base(project.project_id, "相同库")
        scope = (project.project_id, kb.knowledge_base_id)
        jobs = [
            runtime.lifecycle.create_document(
                *scope,
                display_name=f"并发{number}.docx",
                content=_document(f"跨实例资料{number}"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key=f"shared-{number}",
            )
            for number, runtime in enumerate((first, second))
        ]
        first.jobs.submit(jobs[0].job_id)
        second.jobs.submit(jobs[1].job_id)
        assert _wait(first, jobs[0]).state.value == "succeeded"
        assert _wait(second, jobs[1]).state.value == "succeeded"
        assert {
            item.document_id
            for item, _, _ in first.control.active_documents(
                kb.knowledge_base_id
            )
        } == {job.document_id for job in jobs}


def test_claim_serializes_one_kb_but_allows_another_kb(tmp_path: Path) -> None:
    hooks = P09RuntimeHooks(recover_jobs=False)
    with (
        build_p09_runtime(_PROFILE, data_dir=tmp_path, hooks=hooks) as first,
        build_p09_runtime(_PROFILE, data_dir=tmp_path, hooks=hooks) as second,
    ):
        project = first.sdk.create_project("占用项目")
        kbs = [
            first.sdk.create_knowledge_base(project.project_id, name)
            for name in ("甲库", "乙库")
        ]
        jobs = [
            first.lifecycle.create_document(
                project.project_id,
                kbs[number // 2].knowledge_base_id,
                display_name=f"占用{number}.docx",
                content=_document("占用资料"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key=f"claim-{number}",
            )
            for number in range(3)
        ]
        assert first.store.claim_ingestion(jobs[0].job_id) is not None
        assert second.store.claim_ingestion(jobs[1].job_id) is None
        assert second.store.claim_ingestion(jobs[2].job_id) is not None
        for job in (jobs[0], jobs[2]):
            first.store.finish_ingestion(job.job_id, succeeded=False)
        assert second.store.claim_ingestion(jobs[1].job_id) is not None
        second.store.finish_ingestion(jobs[1].job_id, succeeded=False)
