"""自然轮次独立于旧 Claim 历史的加密、范围与来源复核。"""

from __future__ import annotations

from pathlib import Path

from rag_app.application.answering.natural_answer import (
    NaturalAnswerResult,
    NaturalReference,
)
from rag_app.core.identifiers import new_id
from rag_app.core.models import KnowledgeBaseScope
from tests.api.test_query_history import _upload
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)


def test_natural_history_is_scoped_encrypted_and_revoked(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        document_id = _upload(harness, project_id, knowledge_base_id)
        scope = KnowledgeBaseScope(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        with harness.runtime.connections.transaction() as connection:
            row = connection.execute(
                "SELECT kb.active_revision_id, c.chunk_id, "
                "c.document_version_id FROM knowledge_bases kb "
                "JOIN chunks c ON c.revision_id=kb.active_revision_id "
                "WHERE kb.knowledge_base_id=? AND c.document_id=? LIMIT 1",
                (knowledge_base_id, document_id),
            ).fetchone()
        assert row is not None
        trace_id = new_id("trace")
        result = NaturalAnswerResult(
            trace_id=trace_id,
            engine_id="wk-standard-pc-v1",
            answer="维护周期为 14 天。[S1]",
            reason_code="ANSWERED",
            references=(
                NaturalReference(
                    alias="S1",
                    document_id=document_id,
                    document_version_id=str(row["document_version_id"]),
                    document_title="公共设备手册.docx",
                    chunk_ids=(str(row["chunk_id"]),),
                    source_spans=(),
                    citation_basis="original",
                    source_complete=True,
                    excerpt="设备 MX-41 的维护周期为 14 天。",
                ),
            ),
            cited_aliases=("S1",),
            citation_status="valid",
            active_index_revision_id=str(row["active_revision_id"]),
            index_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            rerank_execution_mode="rerank",
            finish_reason="stop",
        )
        question = "自然会话的 MX-41 维护周期？"
        store = harness.runtime.conversations
        assert store.commit_natural(
            scope, "natural-case", question, result, owner_id="owner-a"
        )
        assert not store.commit_natural(
            scope, "natural-case", question, result, owner_id="owner-a"
        )
        own = store.natural_turns(scope, "natural-case", owner_id="owner-a")
        assert len(own) == 1
        sessions = store.natural_sessions(scope, owner_id="owner-a")
        assert [item.conversation_id for item in sessions] == ["natural-case"]
        assert sessions[0].title == question
        assert store.natural_sessions(scope, owner_id="owner-b") == ()
        assert own[0].answer == result.answer
        assert own[0].validation_level == "citation_binding_only"
        assert (
            store.natural_turns(scope, "natural-case", owner_id="owner-b") == ()
        )
        assert (
            "仅用于理解指代"
            in store.context(scope, "natural-case", owner_id="owner-a")[0]
        )
        assert (
            question.encode()
            not in harness.runtime.connections.database_path.read_bytes()
        )
        invalid = result.model_copy(
            update={
                "trace_id": new_id("trace"),
                "answer": None,
                "draft": "无效引用的草稿。[S999]",
                "references": (),
                "citation_status": "invalid",
                "reason_code": "CITATION_INVALID",
            }
        )
        assert not store.commit_natural(
            scope, "natural-case", "无效草稿", invalid, owner_id="owner-a"
        )
        harness.runtime.sdk.delete_document(
            project_id, knowledge_base_id, document_id
        )
        stale = store.natural_turn(
            scope, "natural-case", trace_id, owner_id="owner-a"
        )
        assert stale is not None
        assert stale.status == "SOURCE_UNAVAILABLE"
        assert stale.answer is None
        assert stale.references == ()
        assert (
            store.clear(scope, "natural-case", owner_id="owner-a").deleted_turns
            == 1
        )
    finally:
        harness.close()
