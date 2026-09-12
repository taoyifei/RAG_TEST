from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.adapters.lexical import DeterministicCjkBigramAnalyzer
from rag_app.adapters.stores.sqlite_fts5 import build_fts_v2_query
from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    DocumentRef,
    KnowledgeBaseScope,
    LexicalSearchRequest,
    RequestedAnswerType,
    RetrievalPolicy,
    StructuralSearchRequest,
)
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _heading(level: int, text: str) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _document(
    project_id: str,
    knowledge_base_id: str,
    name: str,
    text: str,
) -> IngestionDocument:
    return _document_blocks(
        project_id,
        knowledge_base_id,
        name,
        _paragraph(text),
    )


def _document_blocks(
    project_id: str,
    knowledge_base_id: str,
    name: str,
    blocks: str,
) -> IngestionDocument:
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id("doc", "fts-v2", name),
            display_name=f"{name}.docx",
        ),
        content=build_docx(blocks),
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


def test_structural_channel_ranks_document_heading_and_body(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    heading = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>目的</w:t></w:r></w:p>"
    )
    selected = _document_blocks(
        project_id,
        knowledge_base_id,
        "蓝熊工作规范",
        heading + _paragraph("统一设备交接记录，并保留复核结果。"),
    )
    noise = _document_blocks(
        project_id,
        knowledge_base_id,
        "白鹭工作规范",
        heading + _paragraph("规范例行巡检安排。"),
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(selected, noise),
            idempotency_key="structural-document-heading",
            budgets=runtime.default_budgets(),
        )
        revision = runtime.control.revision_vector_spec(
            result.revision_id
        ).revision

        hits = runtime.components.lexical_store.search_structural_candidates(
            StructuralSearchRequest(
                revision=revision,
                query="蓝熊工作规范的目的是什么",
                target="蓝熊工作规范",
                relation="目的",
                answer_type=RequestedAnswerType.PURPOSE,
                source_qualifier="蓝熊工作规范",
                limit=5,
            )
        )

        assert hits
        assert hits[0].document_id == selected.document.document_id
        assert hits[0].channel == "structural:canonical-v1"
        assert hits[0].match_type == "STRUCTURAL_SECTION_HEADING_BODY"
        assert all(
            hit.document_id == selected.document.document_id for hit in hits
        )
    finally:
        runtime.close()


def test_flat_numbered_heading_context_is_indexed_and_round_trips(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    long_duties = "".join(
        _paragraph(
            f"{label}）负责公开合成质量目标、资源协调、交付复核与改进闭环。" * 5
        )
        for label in ("a", "b", "c", "d")
    )
    selected = _document_blocks(
        project_id,
        knowledge_base_id,
        "合成岗位职责",
        _paragraph("4 部门职责")
        + _paragraph("4.1 合成总经理")
        + long_duties
        + _paragraph("4.2 合成财务")
        + _paragraph("在合成总经理领导下核对公开合成账目。"),
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(selected,),
            idempotency_key="flat-heading-context",
            budgets=runtime.default_budgets(),
        )
        spec = runtime.control.revision_vector_spec(result.revision_id)

        hits = runtime.components.lexical_store.search(
            LexicalSearchRequest(
                revision=spec.revision,
                query="合成总经理",
                limit=20,
            )
        )
        manager_chunks = [
            hit.chunk
            for hit in hits
            if hit.chunk.heading_path[-1:] == ("4.1 合成总经理",)
        ]

        assert manager_chunks
        assert hits[0].chunk.heading_path[-1:] == ("4.1 合成总经理",)
        assert any(
            "合成总经理" not in chunk.citation_text for chunk in manager_chunks
        )
        assert all(chunk.context_dependencies for chunk in manager_chunks)
        assert all(
            len(chunk.context_dependencies) == len(chunk.heading_path)
            for chunk in manager_chunks
        )
    finally:
        runtime.close()


def test_structural_channel_accepts_generic_document_suffix_in_qualifier(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    heading = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>术语</w:t></w:r></w:p>"
    )
    selected = _document_blocks(
        project_id,
        knowledge_base_id,
        "蓝熊工作模式",
        heading + _paragraph("快速验证是指用于确认方案可行性的简化活动。"),
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(selected,),
            idempotency_key="structural-generic-document-suffix",
            budgets=runtime.default_budgets(),
        )
        revision = runtime.control.revision_vector_spec(
            result.revision_id
        ).revision

        hits = runtime.components.lexical_store.search_structural_candidates(
            StructuralSearchRequest(
                revision=revision,
                query="蓝熊工作模式文档里快速验证是什么",
                target="快速验证",
                relation="定义",
                answer_type=RequestedAnswerType.DEFINITION,
                source_qualifier="蓝熊工作模式文档",
                limit=5,
            )
        )

        assert hits
        assert all(
            hit.document_id == selected.document.document_id for hit in hits
        )
    finally:
        runtime.close()


def test_structural_channel_closes_split_role_table_header_and_target_row(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    long_duty = "核对公开合成用例与缺陷闭环记录。" * 28
    table = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        "<w:tr><w:tc>"
        + _paragraph("角色名称")
        + "</w:tc><w:tc>"
        + _paragraph("主要工作职责说明")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + _paragraph("合成协调员")
        + "</w:tc><w:tc>"
        + _paragraph(long_duty)
        + _paragraph("组织公开合成验收并归档记录。")
        + "</w:tc></w:tr></w:tbl>"
    )
    selected = _document_blocks(
        project_id,
        knowledge_base_id,
        "蓝熊交付项目规范",
        _heading(1, "角色与职责") + table,
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(selected,),
            idempotency_key="structural-split-role-table",
            budgets=runtime.default_budgets(),
        )
        revision = runtime.control.revision_vector_spec(
            result.revision_id
        ).revision

        hits = runtime.components.lexical_store.search_structural_candidates(
            StructuralSearchRequest(
                revision=revision,
                query="做蓝熊交付项目时，合成协调员平时主要管哪些事",
                target="合成协调员",
                relation="职责",
                answer_type=RequestedAnswerType.DUTIES,
                context_qualifier="蓝熊交付项目",
                limit=24,
            )
        )
        scope = KnowledgeBaseScope(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        snapshot = runtime.control.active_query_snapshot(
            scope,
            serving_fingerprint=runtime.components.serving_fingerprint,
            retrieval_policy=RetrievalPolicy(),
        )
        hydrated = runtime.control.hydrate_chunks(
            snapshot, tuple(hit.chunk_id for hit in hits)
        )
        by_id = {item.chunk.chunk_id: item.chunk for item in hydrated}
        header_hits = [
            hit
            for hit in hits
            if "主要工作职责说明" in by_id[hit.chunk_id].citation_text
        ]

        assert header_hits
        assert all(
            hit.match_type == "STRUCTURAL_TABLE_ROW" for hit in header_hits
        )
        assert (
            sum(hit.match_type == "STRUCTURAL_TABLE_ROW" for hit in hits) >= 3
        )
    finally:
        runtime.close()


def test_structural_channel_expands_unique_root_title_to_stage_headings(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    blocks = _paragraph("蓝熊软件全流程规范") + _heading(1, "全流程管控要求")
    for number, title in enumerate(
        ("准入与启动", "设计与开发", "验收与归档"), 1
    ):
        blocks += _heading(2, f"3.{number} {title}")
        blocks += _paragraph(f"{title}阶段执行公开合成要求。")
    selected = _document_blocks(
        project_id,
        knowledge_base_id,
        "蓝熊产品说明",
        blocks,
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(selected,),
            idempotency_key="structural-root-title-stages",
            budgets=runtime.default_budgets(),
        )
        revision = runtime.control.revision_vector_spec(
            result.revision_id
        ).revision

        hits = runtime.components.lexical_store.search_structural_candidates(
            StructuralSearchRequest(
                revision=revision,
                query="蓝熊软件全流程规范都分哪些阶段",
                target="蓝熊软件全流程规范",
                relation="主要阶段",
                answer_type=RequestedAnswerType.ENUMERATION,
                limit=5,
            )
        )

        stage_hits = [
            hit for hit in hits if hit.match_type == "STRUCTURAL_STAGE_HEADING"
        ]
        assert len(stage_hits) == 3
        assert len({hit.chunk_id for hit in stage_hits}) == 3
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
