"""已有 Active 中任一文档的同内容重传不应复用别的目标 Job。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from rag_app.composition.p09_runtime import build_p09_runtime
from rag_app.core.errors import NotFound
from rag_app.core.models import Job
from tests.sdk.test_p09_sdk import _DOCX_MEDIA_TYPE, _PROFILE, _document, _wait


def test_unchanged_earlier_document_uses_own_completed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("同内容重传项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "合成资料库")
        scope = (project.project_id, kb.knowledge_base_id)
        content = [
            _document(f"合成独立文档条目 {number}") for number in range(3)
        ]
        jobs = [
            _wait(
                runtime,
                runtime.sdk.create_document(
                    *scope,
                    display_name=f"资料{number}.docx",
                    content=data,
                    media_type=_DOCX_MEDIA_TYPE,
                    idempotency_key=f"initial-{number}",
                ),
            )
            for number, data in enumerate(content)
        ]
        active = runtime.control.active_revision_id(kb.knowledge_base_id)
        original_builder_job = runtime.sdk.get_job(jobs[-1].job_id)
        builder = Mock(side_effect=AssertionError("同内容不得重新构建"))
        monkeypatch.setattr(
            runtime.retrieval_runtime.persistence.builder,
            "build_and_activate",
            builder,
        )
        repeated = _wait(
            runtime,
            runtime.sdk.create_document_version(
                *scope,
                str(jobs[0].document_id),
                content=content[0],
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="same-active-other-target",
            ),
        )
        assert repeated.state.value == "succeeded"
        assert repeated.safe_error == "相同内容已在当前索引，无需重建"
        assert repeated.revision_id == active
        assert repeated.document_id == jobs[0].document_id
        assert repeated.document_version_id == jobs[0].document_version_id
        assert repeated.job_id != original_builder_job.job_id
        assert (
            runtime.sdk.get_job(original_builder_job.job_id)
            == original_builder_job
        )
        assert (
            runtime.control.active_revision_id(kb.knowledge_base_id) == active
        )
        assert len(runtime.control.active_documents(kb.knowledge_base_id)) == 3
        assert (
            len(
                runtime.sdk.list_document_versions(
                    *scope, str(jobs[0].document_id)
                )
            )
            == 1
        )
        again = runtime.sdk.create_document_version(
            *scope,
            str(jobs[0].document_id),
            content=content[0],
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="same-active-another-key",
        )
        assert again.job_id == repeated.job_id
        builder.assert_not_called()


@pytest.mark.parametrize("has_active_version", [False, True])
def test_unpublished_or_deleted_version_is_never_an_unchanged_receipt(
    tmp_path: Path,
    has_active_version: bool,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("未发布版本项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "状态隔离库")
        scope = (project.project_id, kb.knowledge_base_id)
        content = _document("尚未发布的合成文档版本。")
        original = runtime.lifecycle.create_document(
            *scope,
            display_name="状态隔离.docx",
            content=content,
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="initial-version",
        )
        pending = original
        if has_active_version:
            runtime.jobs.submit(original.job_id)
            assert _wait(runtime, original).state.value == "succeeded"
            content = _document("当前索引以外的待发布新版本。")
            pending = runtime.lifecycle.create_document_version(
                *scope,
                str(original.document_id),
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="pending-new-version",
            )
        active = runtime.control.active_revision_id(kb.knowledge_base_id)
        repeated = runtime.lifecycle.create_document_version(
            *scope,
            str(original.document_id),
            content=content,
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="repeat-pending-version",
        )
        assert repeated.job_id == pending.job_id
        assert repeated.state.value == "queued"
        assert repeated.safe_error is None
        assert (
            runtime.control.active_revision_id(kb.knowledge_base_id) == active
        )
        runtime.sdk.delete_document(*scope, str(original.document_id))
        with pytest.raises(NotFound):
            runtime.lifecycle.create_document_version(
                *scope,
                str(original.document_id),
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="repeat-deleted-version",
            )


def test_concurrent_unchanged_uploads_do_not_consume_or_rebind_pending_queue(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(
        _PROFILE, data_dir=tmp_path, max_pending_jobs=1
    ) as runtime:
        project = runtime.sdk.create_project("并发同内容项目")
        kb = runtime.sdk.create_knowledge_base(
            project.project_id, "并发同内容库"
        )
        scope = (project.project_id, kb.knowledge_base_id)
        content = _document("同源字节的不同逻辑文档。")
        original = [
            _wait(
                runtime,
                runtime.sdk.create_document(
                    *scope,
                    display_name=f"同内容{number}.docx",
                    content=content,
                    media_type=_DOCX_MEDIA_TYPE,
                    idempotency_key=f"original-{number}",
                ),
            )
            for number in range(2)
        ]
        active = runtime.control.active_revision_id(kb.knowledge_base_id)
        pending = runtime.lifecycle.create_document(
            *scope,
            display_name="继续排队.docx",
            content=_document("排队成员不能丢失。"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="keep-pending",
        )

        def repeat(number: int) -> Job:
            target = original[number % 2]
            return runtime.sdk.create_document_version(
                *scope,
                str(target.document_id),
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key=f"repeat-{number}",
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            repeated = list(executor.map(repeat, range(8)))
        assert all(job.state.value == "succeeded" for job in repeated)
        assert all(job.revision_id == active for job in repeated)
        assert len({job.job_id for job in repeated}) == 2
        assert runtime.sdk.get_job(pending.job_id).state.value == "queued"
        assert (
            runtime.control.active_revision_id(kb.knowledge_base_id) == active
        )
        runtime.jobs.submit(pending.job_id)
        assert _wait(runtime, pending).state.value == "succeeded"
        assert len(runtime.control.active_documents(kb.knowledge_base_id)) == 3
