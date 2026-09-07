from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.adapters.lexical import DeterministicCjkBigramAnalyzer
from rag_app.adapters.stores.sqlite_fts5 import build_fts_v2_query
from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import DocumentRef, LexicalSearchRequest
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _document(
    project_id: str,
    knowledge_base_id: str,
    name: str,
    text: str,
) -> IngestionDocument:
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id("doc", "fts-v2", name),
            display_name=f"{name}.docx",
        ),
        content=build_docx(_paragraph(text)),
        media_type=_MEDIA_TYPE,
    )


def test_cjk_document_and_query_analysis_are_symmetric_and_bounded() -> None:
    analyzer = DeterministicCjkBigramAnalyzer(max_query_characters=32)

    document = analyzer.analyze_document("青岛啤酒采购流程 ABC-１２３")
    query = analyzer.analyze_query("青岛啤酒 ABC-123")
    expression = build_fts_v2_query(query)

    assert "青岛" in document.tokens
    assert "岛啤" in document.tokens
    assert "啤酒" in document.tokens
    assert "abc-123" in document.tokens
    assert '"青岛啤酒"' in expression
    assert " OR " in expression
    assert " AND " in expression
    assert build_fts_v2_query(analyzer.analyze_query('" OR *'))
    with pytest.raises(ValueError, match="字符数超过上限"):
        analyzer.analyze_query("过" * 33)


@pytest.mark.parametrize(
    "query",
    ("青岛啤酒", "数据安全", "审批时限", "ABC-123"),
)
def test_fts_v2_matches_cjk_phrases_and_identifier(
    tmp_path: Path,
    query: str,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    target = _document(
        project_id,
        knowledge_base_id,
        "target",
        "青岛啤酒采购流程。企业数据安全管理办法。"
        "合同审批时限为三个工作日。设备型号 ABC-123。",
    )
    noise = _document(
        project_id,
        knowledge_base_id,
        "noise",
        "生产安全与人员管理分别执行，设备型号 XYZ-900。",
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(target, noise),
            idempotency_key=f"fts-v2-{query}",
            budgets=runtime.default_budgets(),
        )
        spec = runtime.control.revision_vector_spec(result.revision_id)

        hits = runtime.components.lexical_store.search(
            LexicalSearchRequest(
                revision=spec.revision,
                query=query,
                limit=10,
            )
        )

        assert hits
        assert hits[0].chunk.version.document_id == target.document.document_id
    finally:
        runtime.close()


def test_cjk_group_does_not_degrade_to_unbounded_single_character_or(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    target = _document(
        project_id,
        knowledge_base_id,
        "contiguous",
        "企业数据安全管理办法。",
    )
    noise = _document(
        project_id,
        knowledge_base_id,
        "separated",
        "生产安全与人员管理分别执行。",
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(target, noise),
            idempotency_key="fts-v2-controlled-or",
            budgets=runtime.default_budgets(),
        )
        spec = runtime.control.revision_vector_spec(result.revision_id)

        hits = runtime.components.lexical_store.search(
            LexicalSearchRequest(
                revision=spec.revision,
                query="安全管理",
                limit=10,
            )
        )

        assert {hit.chunk.version.document_id for hit in hits} == {
            target.document.document_id
        }
    finally:
        runtime.close()


def test_deleted_lexical_hit_is_removed_before_limit(tmp_path: Path) -> None:
    """已删除的首名不能占用窗口；直接 Store 也必须返回次名活动文档。"""
    runtime, project, kb = runtime_with_kb(tmp_path)
    documents = tuple(
        _document(project, kb, name, "巡检要求记录完整。")
        for name in ("first", "second")
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project,
            knowledge_base_id=kb,
            documents=documents,
            idempotency_key="deleted-before-limit",
            budgets=runtime.default_budgets(),
        )
        request = LexicalSearchRequest(
            revision=runtime.control.revision_vector_spec(
                result.revision_id
            ).revision,
            query="巡检要求",
            limit=1,
        )
        store = runtime.components.lexical_store
        first = store.search_candidates(request)[0]
        with runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE documents SET deleted_at='2026-01-01T00:00:00Z', "
                "lifecycle_status='deleted' WHERE document_id=?",
                (first.document_id,),
            )
        candidates = store.search_candidates(request)
        direct = store.search(request)
        assert len(candidates) == len(direct) == 1
        assert candidates[0].document_id != first.document_id
        assert direct[0].chunk.version.document_id == candidates[0].document_id
    finally:
        runtime.close()
