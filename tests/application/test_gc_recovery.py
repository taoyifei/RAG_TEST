from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import DocumentRef
from rag_app.core.ports import BlobPutResult, BlobWriteRequest
from tests.adapters.parsers.docx_fixtures import TABLE, build_docx
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _document(
    project_id: str,
    knowledge_base_id: str,
    name: str,
) -> IngestionDocument:
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id("doc", "gc-recovery", name),
            display_name=f"{name}.docx",
        ),
        content=build_docx(TABLE),
        media_type=_MEDIA_TYPE,
    )


def test_revision_gc_resumes_after_vector_delete_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    try:
        retired = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(project_id, knowledge_base_id, "retired"),
            ),
            idempotency_key="gc-retired",
            budgets=runtime.default_budgets(),
        )
        runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(project_id, knowledge_base_id, "active"),
            ),
            idempotency_key="gc-active",
            budgets=runtime.default_budgets(),
        )
        plan = runtime.garbage_collector.plan(
            protected_retired_count=0,
            grace_before=(datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        )
        vector_store = runtime.components.vector_store
        original_delete = vector_store.delete_revision
        crashed = False

        def delete_then_crash(spec: object) -> None:
            nonlocal crashed
            original_delete(spec)  # type: ignore[arg-type]
            if not crashed:
                crashed = True
                raise RuntimeError("injected post-vector crash")

        monkeypatch.setattr(vector_store, "delete_revision", delete_then_crash)
        with pytest.raises(RuntimeError, match="post-vector crash"):
            runtime.garbage_collector.apply(plan.plan_id)
        monkeypatch.setattr(vector_store, "delete_revision", original_delete)

        runtime.garbage_collector.apply(plan.plan_id)

        assert not runtime.control.gc_revision_exists(retired.revision_id)
        item = next(
            row
            for row in runtime.control.gc_plan_items(plan.plan_id)
            if row["item_id"] == retired.revision_id
        )
        assert item["state"] == "completed"
    finally:
        runtime.close()


def test_failed_terminal_revision_is_collected_after_retention(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    try:
        failed = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(project_id, knowledge_base_id, "failed"),
            ),
            idempotency_key="gc-failed",
            budgets=runtime.default_budgets(),
        )
        runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(project_id, knowledge_base_id, "replacement"),
            ),
            idempotency_key="gc-replacement",
            budgets=runtime.default_budgets(),
        )
        with runtime.control._connections.transaction(
            write=True
        ) as connection:
            connection.execute(
                "UPDATE index_revisions SET state='failed_terminal' "
                "WHERE index_revision_id=?",
                (failed.revision_id,),
            )
        plan = runtime.garbage_collector.plan(
            protected_retired_count=1,
            grace_before=(datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        )

        runtime.garbage_collector.apply(plan.plan_id)

        assert not runtime.control.gc_revision_exists(failed.revision_id)
    finally:
        runtime.close()


def test_physical_only_blob_is_quarantined_in_catalog_evidence(
    tmp_path: Path,
) -> None:
    runtime, _, _ = runtime_with_kb(tmp_path)
    content = b"synthetic interrupted blob"
    digest = hashlib.sha256(content).hexdigest()
    blob_id = f"sha256:{digest}"
    try:
        outcome = runtime.components.blob_store.put_if_absent(
            BlobWriteRequest(
                blob_id=blob_id,
                content_sha256=digest,
                media_type="application/octet-stream",
                content=content,
            )
        )
        assert outcome is BlobPutResult.CREATED

        result = runtime.garbage_collector.reconcile_filesystem()

        assert result["physical_only"] == (blob_id,)
        assert runtime.components.blob_store.exists(blob_id)
        row = next(
            item
            for item in runtime.control.blob_reconciliation_rows()
            if item["artifact_id"] == blob_id
        )
        assert row["observed_state"] == "physical_only"
        assert row["action_state"] == "quarantined"
    finally:
        runtime.close()
