"""OCR 加工身份隔离 Revision，并在持久 Worker 发送前检查漂移。"""

from pathlib import Path
from unittest.mock import Mock

from rag_app.composition.p09_runtime import P09RuntimeHooks, build_p09_runtime
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models.management import QueuedIngestion
from tests.sdk.test_p09_sdk import _DOCX_MEDIA_TYPE, _PROFILE, _document


def test_content_identity_changes_revision_but_preserves_document_version(
    tmp_path: Path,
) -> None:
    identity: list[str | None] = [None]
    hooks = P09RuntimeHooks(
        recover_jobs=False, content_identity=lambda _: identity[0]
    )
    with build_p09_runtime(_PROFILE, data_dir=tmp_path, hooks=hooks) as runtime:
        project = runtime.sdk.create_project("加工身份项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "加工身份库")
        scope = (project.project_id, kb.knowledge_base_id)
        content = _document("冻结内容身份，复用同一来源文档版本。")
        initial = runtime.lifecycle.create_document(
            *scope,
            display_name="内容身份.docx",
            content=content,
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="original",
        )
        runtime.lifecycle.run_ingestion(initial.job_id)
        assert runtime.store.get_job(initial.job_id).state.value == "succeeded"
        fingerprint = (
            runtime.retrieval_runtime.persistence.components.index_fingerprint
        )
        assert initial.revision_id == deterministic_id(
            "irev",
            kb.knowledge_base_id,
            (initial.document_version_id,),
            fingerprint,
        )
        identity[0] = "ocr-cache-identity-v1"
        enriched = runtime.lifecycle.create_document_version(
            *scope,
            str(initial.document_id),
            content=content,
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="with-ocr",
        )
        assert enriched.document_version_id == initial.document_version_id
        assert enriched.revision_id != initial.revision_id
        assert enriched.revision_id == deterministic_id(
            "irev",
            kb.knowledge_base_id,
            (initial.document_version_id,),
            canonical_sha256({"index": fingerprint, "content": identity[0]}),
        )
        runtime.lifecycle.run_ingestion(enriched.job_id)
        assert runtime.store.get_job(enriched.job_id).state.value == "succeeded"
        assert (
            runtime.control.active_revision_id(kb.knowledge_base_id)
            == enriched.revision_id
        )
        assert (
            runtime.retrieval_runtime.persistence.components.index_fingerprint
            == fingerprint
        )


def test_changed_content_identity_fails_before_builder_dispatch(
    tmp_path: Path,
) -> None:
    identity = ["ocr-before"]
    hooks = P09RuntimeHooks(
        recover_jobs=False, content_identity=lambda _: identity[0]
    )
    with build_p09_runtime(_PROFILE, data_dir=tmp_path, hooks=hooks) as runtime:
        project = runtime.sdk.create_project("漂移项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "漂移库")
        job = runtime.lifecycle.create_document(
            project.project_id,
            kb.knowledge_base_id,
            display_name="漂移.docx",
            content=_document("不应送入已漂移的内容流水线。"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="drift",
        )
        identity[0] = "ocr-after"
        builder = runtime.retrieval_runtime.persistence.builder
        builder.build_and_activate = Mock()  # type: ignore[method-assign]
        runtime.lifecycle.run_ingestion(job.job_id)
        builder.build_and_activate.assert_not_called()
        failed = runtime.store.get_job(job.job_id)
        assert failed.state.value == "failed_terminal"
        assert "内容加工配置已漂移" in str(failed.safe_error)
        assert runtime.control.active_revision_id(kb.knowledge_base_id) is None


def test_legacy_request_without_content_identity_still_loads(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(
        _PROFILE, data_dir=tmp_path, hooks=P09RuntimeHooks(recover_jobs=False)
    ) as runtime:
        project = runtime.sdk.create_project("兼容项目")
        kb = runtime.sdk.create_knowledge_base(project.project_id, "兼容库")
        job = runtime.lifecycle.create_document(
            project.project_id,
            kb.knowledge_base_id,
            display_name="旧队列.docx",
            content=_document("旧队列无需内容加工身份。"),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="legacy",
        )
        request = runtime.store.claim_ingestion(job.job_id)
        assert request is not None
        payload = request.model_dump()
        payload.pop("content_identity")
        assert QueuedIngestion.model_validate(payload).content_identity is None
        runtime.store.finish_ingestion(job.job_id, succeeded=False)
