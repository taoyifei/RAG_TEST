"""文档删除的本地持久化及检索隔离回归。"""

from pathlib import Path

import pytest

from rag_app.composition.p09_runtime import P09RuntimeHooks, build_p09_runtime
from rag_app.core.errors import JobCancelled, NotFound, RevisionStateError
from rag_app.core.models import DocumentRef
from tests.sdk.test_p09_sdk import _DOCX_MEDIA_TYPE, _PROFILE, _document, _wait


def test_failed_document_delete_is_immediate_and_idempotent(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("删除项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "删除库")
        scope = (project.project_id, kb.knowledge_base_id)
        job = runtime.lifecycle.create_document(
            *scope,
            display_name="失败.docx",
            content=_document("失败前的合成内容"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="failed",
        )
        runtime.store.finish_ingestion(
            job.job_id, succeeded=False, error_code="INVALID_DOCUMENT"
        )
        document_id = str(job.document_id)
        assert (
            runtime.sdk.get_document(*scope, document_id).current_version_id
            is None
        )
        deleted = runtime.sdk.delete_document(*scope, document_id)
        assert deleted.status.value == "deleted"
        assert runtime.sdk.list_documents(*scope) == ()
        assert runtime.sdk.delete_document(*scope, document_id) == deleted
        with pytest.raises(NotFound):
            runtime.sdk.get_document(*scope, document_id)
        with pytest.raises(RevisionStateError):
            runtime.store.mark_version_ready(
                document_id, str(job.document_version_id)
            )
        connections = runtime.retrieval_runtime.persistence.connections
        with connections.transaction() as db:
            row = db.execute(
                "SELECT deleted_at FROM documents WHERE document_id=?",
                (document_id,),
            ).fetchone()
            assert row["deleted_at"] is not None
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        assert runtime.sdk.list_documents(*scope) == ()
        assert (
            runtime.sdk.delete_document(*scope, document_id).status.value
            == "deleted"
        )


def test_document_without_any_version_can_be_deleted(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("无版本项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "无版本库")
        scope = (project.project_id, kb.knowledge_base_id)
        document_id = "doc_" + "1" * 32
        runtime.control.upsert_document(
            DocumentRef(
                project_id=project.project_id,
                knowledge_base_id=kb.knowledge_base_id,
                document_id=document_id,
                display_name="未完成登记.docx",
            )
        )
        assert runtime.sdk.list_document_versions(*scope, document_id) == ()
        assert (
            runtime.sdk.delete_document(*scope, document_id).status.value
            == "deleted"
        )
        assert runtime.sdk.list_documents(*scope) == ()


def test_deleted_document_denies_source_but_preserves_same_named_shared_blob(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("共享项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "共享库")
        scope = (project.project_id, kb.knowledge_base_id)
        jobs = [
            _wait(
                runtime,
                runtime.sdk.create_document(
                    *scope,
                    display_name="同名.docx",
                    content=_document("同源合成内容"),
                    media_type=_DOCX_MEDIA_TYPE,
                    idempotency_key=key,
                ),
            )
            for key in ("one", "two")
        ]
        assert all(job.state.value == "succeeded" for job in jobs)
        versions = [
            runtime.sdk.list_document_versions(*scope, str(job.document_id))[0]
            for job in jobs
        ]
        runtime.sdk.delete_document(*scope, str(jobs[0].document_id))
        assert [
            doc.document_id for doc in runtime.sdk.list_documents(*scope)
        ] == [jobs[1].document_id]
        with pytest.raises(NotFound):
            runtime.sdk.read_artifact(
                *scope,
                str(jobs[0].document_id),
                versions[0].document_version_id,
                versions[0].source_artifact_id,
            )
        assert runtime.sdk.read_artifact(
            *scope,
            str(jobs[1].document_id),
            versions[1].document_version_id,
            versions[1].source_artifact_id,
        ).content


def test_unversioned_document_is_not_presented_as_indexed(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        project = runtime.sdk.create_project("状态项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "状态库")
        scope = (project.project_id, kb.knowledge_base_id)
        ready = _wait(
            runtime,
            runtime.sdk.create_document(
                *scope,
                display_name="正常.docx",
                content=_document("正常索引内容"),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="ready",
            ),
        )
        assert ready.state.value == "succeeded"
        queued = runtime.lifecycle.create_document(
            *scope,
            display_name="排队.docx",
            content=_document("尚未索引内容"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="queued",
        )
        doc = runtime.sdk.get_document(*scope, str(queued.document_id))
        assert doc.current_version_id is None
        assert doc.active_index_revision_id is None
        runtime.sdk.delete_document(*scope, str(queued.document_id))
        assert runtime.sdk.get_job(queued.job_id).state.value == "cancelled"


def test_delete_cancels_other_job_with_frozen_deleted_member(
    tmp_path: Path,
) -> None:
    hooks = P09RuntimeHooks(recover_jobs=False)
    with build_p09_runtime(_PROFILE, data_dir=tmp_path, hooks=hooks) as runtime:
        project = runtime.sdk.create_project("快照取消项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "快照库")
        scope = (project.project_id, kb.knowledge_base_id)
        first = runtime.lifecycle.create_document(
            *scope,
            display_name="旧资料.docx",
            content=_document("旧资料内容"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="old",
        )
        runtime.lifecycle.run_ingestion(first.job_id)
        assert runtime.sdk.get_job(first.job_id).state.value == "succeeded"
        second = runtime.lifecycle.create_document(
            *scope,
            display_name="新资料.docx",
            content=_document("新资料内容"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="new",
        )
        assert runtime.store.claim_ingestion(second.job_id) is not None
        token = runtime.control.acquire_revision_lease(
            second.revision_id, second.job_id
        )
        runtime.sdk.delete_document(*scope, str(first.document_id))
        assert runtime.sdk.get_job(second.job_id).state.value == "cancelled"
        lease = runtime.control.revision_lease(second.revision_id)
        assert lease is not None and lease["fencing_token"] == token
        with pytest.raises(JobCancelled, match="取消"):
            runtime.control.assert_job_active(second.job_id)
        runtime.store.finish_ingestion(second.job_id, succeeded=True)
        assert runtime.sdk.get_job(second.job_id).state.value == "cancelled"
        assert runtime.control.active_documents(kb.knowledge_base_id) == ()
        runtime.control.release_revision_lease(second.revision_id)
