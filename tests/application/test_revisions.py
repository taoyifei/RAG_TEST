from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.errors import Conflict, ProviderUnavailable, ValidationFailed
from rag_app.core.identifiers import deterministic_id, document_version_id
from rag_app.core.models import DocumentRef
from tests.adapters.parsers.docx_fixtures import (
    HEADING,
    TABLE,
    build_docx,
)
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _document(
    project_id: str, knowledge_base_id: str, name: str, content: bytes
) -> IngestionDocument:
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id(
                "doc", project_id, knowledge_base_id, name
            ),
            display_name=f"{name}.docx",
        ),
        content=content,
        media_type=_MEDIA_TYPE,
    )


def test_document_version_identity_and_scope_are_enforced(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    try:
        content = build_docx(TABLE)
        document = _document(project_id, knowledge_base_id, "one", content)
        runtime.control.upsert_document(document.document)
        digest = hashlib.sha256(content).hexdigest()
        version_id = document_version_id(document.document.document_id, digest)
        with pytest.raises(ValidationFailed):
            runtime.control.put_document_version(
                document.document.document_id,
                deterministic_id("dver", "wrong"),
                digest,
                f"sha256:{digest}",
                len(content),
                _MEDIA_TYPE,
            )
        other_kb = deterministic_id("kb", project_id, "other")
        runtime.control.put_knowledge_base(
            other_kb,
            project_id,
            "Other KB",
            profile_id=runtime.components.profile.profile_id,
        )
        with pytest.raises(Conflict):
            runtime.control.upsert_document(
                document.document.model_copy(
                    update={"knowledge_base_id": other_kb}
                )
            )
        assert version_id != document_version_id(
            deterministic_id("doc", "other"), digest
        )
    finally:
        runtime.close()


def test_gc_plan_rejects_active_revision_drift(tmp_path: Path) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    try:
        first = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(
                    project_id,
                    knowledge_base_id,
                    "first",
                    build_docx(TABLE),
                ),
            ),
            idempotency_key="first",
            budgets=runtime.default_budgets(),
        )
        plan = runtime.garbage_collector.plan(
            protected_retired_count=0,
            grace_before=datetime.now(UTC).isoformat(),
        )
        second = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(
                    project_id,
                    knowledge_base_id,
                    "second",
                    build_docx(TABLE + TABLE),
                ),
            ),
            idempotency_key="second",
            budgets=runtime.default_budgets(),
        )
        with pytest.raises(ValidationFailed, match="漂移"):
            runtime.garbage_collector.apply(plan.plan_id)
        assert (
            runtime.control.active_revision_id(knowledge_base_id)
            == second.revision_id
        )
        assert first.revision_id != second.revision_id
    finally:
        runtime.close()


def test_validation_failure_keeps_previous_active_revision(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    try:
        first = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(
                    project_id,
                    knowledge_base_id,
                    "good",
                    build_docx(TABLE),
                ),
            ),
            idempotency_key="good",
            budgets=runtime.default_budgets(),
        )
        with pytest.raises(ValidationFailed):
            runtime.builder.build_and_activate(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                documents=(
                    _document(
                        project_id,
                        knowledge_base_id,
                        "bad",
                        build_docx(HEADING),
                    ),
                ),
                idempotency_key="bad",
                budgets=runtime.default_budgets(),
            )
        assert (
            runtime.control.active_revision_id(knowledge_base_id)
            == first.revision_id
        )
    finally:
        runtime.close()


def test_retryable_build_resumes_same_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = _document(
        project_id,
        knowledge_base_id,
        "retry",
        build_docx(TABLE),
    )
    provider = runtime.components.embedding_primary
    original_embed = provider.embed

    def fail_once(request: object) -> object:
        del request
        raise ProviderUnavailable("injected", stage="test.retry")

    try:
        monkeypatch.setattr(provider, "embed", fail_once)
        with pytest.raises(ProviderUnavailable):
            runtime.builder.build_and_activate(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                documents=(document,),
                idempotency_key="retryable",
                budgets=runtime.default_budgets(),
            )
        monkeypatch.setattr(provider, "embed", original_embed)
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(document,),
            idempotency_key="retryable",
            budgets=runtime.default_budgets(),
            attempt=2,
        )
        assert (
            runtime.control.active_revision_id(knowledge_base_id)
            == result.revision_id
        )
        assert runtime.control.job_summary(result.job_id)["attempt"] == 2
    finally:
        runtime.close()
