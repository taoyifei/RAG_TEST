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
