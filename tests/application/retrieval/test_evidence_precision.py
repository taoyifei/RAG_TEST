from __future__ import annotations

import hashlib

import pytest

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.evidence import (
    EvidenceAssembler,
    _scope_evidence_candidates,
)
from rag_app.application.retrieval.planner import QueryPlanner
from rag_app.core.models import (
    ChunkRole,
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryKind,
    QueryVariant,
    RetrievalPolicy,
    SearchRequest,
)
from tests.application.retrieval.helpers import make_ranked_chunk

_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'1' * 32}",
    knowledge_base_id=f"kb_{'2' * 32}",
)


def test_query_aware_selection_rejects_higher_ranked_noise() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="液压压力是多少")
    )
    noise = make_ranked_chunk(1, "市场部门本周召开例会", document_number=1)
    relevant = make_ranked_chunk(
        2, "液压系统额定压力为 16 MPa", document_number=2
    )

    evidence = EvidenceAssembler().assemble(
        (noise, relevant),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert [item.chunk_id for item in evidence] == [
        relevant.hydrated.chunk.chunk_id
    ]
    assert evidence[0].citation_text == "液压系统额定压力为 16 MPa"


def test_complex_list_keeps_preceding_stage_under_evidence_cap() -> None:
    """复合阶段问题的前序来源不能被原始重排候选挤出预算。"""
    analysis = QueryAnalyzer().analyze(
        SearchRequest(
            scope=_SCOPE,
            text="项目立项要备哪些材料、走哪些步骤？",
        )
    )
    current = make_ranked_chunk(
        1, "立项申报阶段应提交项目材料。", role=ChunkRole.LIST
    )
    later = make_ranked_chunk(
        2, "立项审核阶段应检查项目材料。", role=ChunkRole.LIST
    )
    preceding = make_ranked_chunk(
        3, "立项准备阶段应编制项目材料。", role=ChunkRole.LIST
    ).model_copy(update={"expansion_reason": "SECTION_PREDECESSOR"})
    context = EvidenceSelectionContext(
        analysis=analysis,
        query_kind=QueryKind.COMPLEX,
        rerank_mode="provider",
        selected_slot=None,
    )

    selected = EvidenceAssembler().assemble_sets(
        (current, later, preceding),
        RetrievalPolicy(
            max_evidence_items=2,
            per_document_cap=2,
            per_section_cap=2,
        ),
        context=context,
        include_model_candidates=True,
    )

    assert preceding.hydrated.chunk.chunk_id in {
        item.chunk_id for item in selected.model_evidence_candidates
    }


def test_unique_quoted_document_label_scopes_similar_templates() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(
            scope=_SCOPE, text="是否有《需求阶段-会议纪要模板》可参考？"
        )
    )
    wanted = make_ranked_chunk(1, "模板目录项：需求阶段-会议纪要模板。")
    noise = make_ranked_chunk(
        2, "模板目录项：需求变更评审会议纪要模板。", document_number=3
    )
    wanted = wanted.model_copy(
        update={
            "hydrated": wanted.hydrated.model_copy(
                update={"display_name": "需求阶段-会议纪要模板.docx"}
            )
        }
    )
    noise = noise.model_copy(
        update={
            "hydrated": noise.hydrated.model_copy(
                update={"display_name": "需求变更评审会议纪要模板.docx"}
            )
        }
    )
    context = EvidenceSelectionContext(
        analysis=analysis,
        query_kind=QueryKind.SIMPLE_FACT,
        rerank_mode="provider",
        selected_slot=None,
    )

    assert _scope_evidence_candidates((noise, wanted), context) == (wanted,)


def test_exact_catalog_reference_proves_only_matching_title_exists() -> None:
    """目录项只支持精确标题的存在和引用，不证明正文与近似标题。"""
    title = "阶段甲-交接记录模板20260701"
    catalog = (
        f"模板目录项：{title}（模板）。模板正文未入库；"
        "具体填写项、示例及要求请参考原始模板。"
    )
    wanted = make_ranked_chunk(1, catalog)
    wanted = wanted.model_copy(
        update={
            "hydrated": wanted.hydrated.model_copy(
                update={"display_name": f"{title}.docx"}
            )
        }
    )
    similar = make_ranked_chunk(
        2,
        "模板目录项：阶段甲-交接记录模板20260702（模板）。"
        "模板正文未入库；具体填写项、示例及要求请参考原始模板。",
        document_number=2,
    )
    similar = similar.model_copy(
        update={
            "hydrated": similar.hydrated.model_copy(
                update={"display_name": "阶段甲-交接记录模板20260702.docx"}
            )
        }
    )

    def evidence(question: str) -> tuple:
        analysis = QueryAnalyzer().analyze(
            SearchRequest(scope=_SCOPE, text=question)
        )
        context = EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="provider",
            selected_slot=None,
        )
        return EvidenceAssembler().assemble(
            (similar, wanted), RetrievalPolicy(), context=context
        )

    for question in (
        f"是否有《{title}》可供参考？",
        f"准备相关材料时，应参考哪份模板《{title}》？",
    ):
        selected = evidence(question)
        assert len(selected) == 1
        assert selected[0].citation_text == catalog
        assert (
            dict(selected[0].metadata)["answer_support"]["support_reason"]
            == "CATALOG_TITLE_EXISTS"
        )
    assert not evidence(f"《{title}》有哪些填写字段？")
    assert not evidence("是否有《阶段甲-交接记录模板20260703》可供参考？")


def test_book_title_uses_exact_retrieval_without_forcing_answer() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="是否有《阶段甲-交接记录模板》？")
    )
    assert analysis.quoted_phrases == ("阶段甲-交接记录模板",)
    variant = QueryVariant(
        text=analysis.normalized_query,
        kind="original",
        identity="sha256:"
        + hashlib.sha256(analysis.normalized_query.encode()).hexdigest(),
    )
    plan = QueryPlanner().plan(analysis, (variant,), RetrievalPolicy())
    assert "exact" in plan.channels
    assert not plan.must_keep_exact


@pytest.mark.parametrize(
    ("query_title", "entry_title", "display_name"),
    [
        (
            "阶段甲-复盘&记录模板20260701",
            "阶段甲-复盘&记录模板20260701",
            "阶段甲-复盘&amp;记录模板20260701.docx",
        ),
        (
            "阶段甲-交接记录模板20260701",
            "阶段甲-交接记录模板20260701(原件 .doc)",
            "阶段甲-交接记录模板20260701(原件 .doc).docx",
        ),
        (
            "阶段甲-交接记录模板（模板）",
            "阶段甲-交接记录模板(模板)",
            "阶段甲-交接记录模板(模板).docx",
        ),
    ],
)
def test_catalog_title_identity_ignores_only_format_annotations(
    query_title: str, entry_title: str, display_name: str
) -> None:
    quote = (
        f"模板目录项：{entry_title}（模板）。模板正文未入库；"
        "具体填写项、示例及要求请参考原始模板。"
    )
    candidate = make_ranked_chunk(1, quote)
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={"display_name": display_name}
            )
        }
    )
    analysis = QueryAnalyzer().analyze(
        SearchRequest(
            scope=_SCOPE, text=f"是否有《{query_title}》可供参考？"
        )
    )
    selected = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="provider",
            selected_slot=None,
        ),
    )
    assert len(selected) == 1
    assert (
        dict(selected[0].metadata)["answer_support"]["support_reason"]
        == "CATALOG_TITLE_EXISTS"
    )


def test_literal_lookup_tolerates_bounded_noise_without_selecting_it() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="档案归还规则 临时噪声")
    )
    noise = make_ranked_chunk(1, "临时噪声", document_number=1)
    relevant = make_ranked_chunk(
        2,
        "档案归还规则要求双人核对。",
        document_number=2,
    )

    evidence = EvidenceAssembler().assemble(
        (noise, relevant),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert [item.chunk_id for item in evidence] == [
        relevant.hydrated.chunk.chunk_id
    ]


def test_literal_lookup_does_not_fuzz_hard_negation() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="不存在的月球库存编号")
    )
    partial = make_ranked_chunk(
        1,
        "该文档不包含火星或不存在的型号。",
    )

    evidence = EvidenceAssembler().assemble(
        (partial,),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert evidence == ()


def test_open_identifier_question_can_publish_its_exact_source() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="PRE-77 是什么")
    )
    relevant = make_ranked_chunk(1, "标题前正文 PRE-77。")

    evidence = EvidenceAssembler().assemble(
        (relevant,),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.EXACT_IDENTIFIER,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert [item.citation_text for item in evidence] == ["标题前正文 PRE-77。"]


def test_identifier_fact_requires_matching_context_for_open_relation() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="ZX-17 属于哪个唯一事实")
    )
    relevant = make_ranked_chunk(
        1,
        "同名文档甲的唯一事实 ZX-17。",
        document_number=1,
    )
    unrelated = make_ranked_chunk(
        2,
        "向量槽位的标识符为 ZX-17。",
        document_number=2,
    )

    evidence = EvidenceAssembler().assemble(
        (unrelated, relevant),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.EXACT_IDENTIFIER,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert [item.chunk_id for item in evidence] == [
        relevant.hydrated.chunk.chunk_id
    ]


def test_identifier_lookup_ignores_only_generic_search_scaffolding() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="OMIT-2026 表格记录")
    )
    relevant = make_ranked_chunk(1, "OMIT-2026 2026-09-03")

    evidence = EvidenceAssembler().assemble(
        (relevant,),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.EXACT_IDENTIFIER,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert [item.citation_text for item in evidence] == ["OMIT-2026 2026-09-03"]


def test_literal_identifier_does_not_promote_single_table_cell() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="共享记录 ZX-17")
    )
    relevant = make_ranked_chunk(
        1,
        "共享记录 ZX-17 对应绿色组件。",
        document_number=1,
    )
    partial = make_ranked_chunk(
        2,
        "ZX-17",
        document_number=2,
        role=ChunkRole.TABLE,
    )

    evidence = EvidenceAssembler().assemble(
        (relevant, partial),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.EXACT_IDENTIFIER,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert [item.chunk_id for item in evidence] == [
        relevant.hydrated.chunk.chunk_id
    ]


def test_evidence_total_and_per_chunk_caps_are_explicit() -> None:
    candidates = tuple(
        make_ranked_chunk(
            index,
            f"压力参数 {index}",
            document_number=index,
        )
        for index in range(1, 6)
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        RetrievalPolicy(max_evidence_items=2, max_evidence_items_per_chunk=1),
    )

    assert len(evidence) == 2
    assert len({item.chunk_id for item in evidence}) == 2


def test_model_candidates_do_not_let_one_chunk_consume_the_total_cap() -> None:
    first = make_ranked_chunk(1, "甲乙丙丁戊己庚辛", document_number=1)
    original_span = first.hydrated.chunk.source_spans[0]
    source_anchor = original_span.source_anchor
    assert source_anchor is not None
    spans = tuple(
        original_span.model_copy(
            update={
                "node_id": f"node_diversity_{index}",
                "source_anchor": source_anchor.model_copy(
                    update={
                        "structural_path": ("body", f"p:{index}"),
                        "ordinal": index,
                        "paragraph_index": index,
                        "source_start_char": index,
                        "source_end_char": index + 1,
                    }
                ),
                "structural_path": ("body", f"p:{index}"),
                "chunk_start_char": index,
                "chunk_end_char": index + 1,
                "source_start_char": index,
                "source_end_char": index + 1,
            }
        )
        for index in range(8)
    )
    first = first.model_copy(
        update={
            "hydrated": first.hydrated.model_copy(
                update={
                    "chunk": first.hydrated.chunk.model_copy(
                        update={"source_spans": spans}
                    )
                }
            )
        }
    )
    later = make_ranked_chunk(2, "真正答案", document_number=2)

    evidence = EvidenceAssembler().assemble(
        (first, later),
        RetrievalPolicy(
            max_evidence_items=8,
            max_evidence_items_per_chunk=8,
            per_document_cap=8,
            per_section_cap=8,
        ),
        context=EvidenceSelectionContext(
            analysis=QueryAnalyzer().analyze(
                SearchRequest(scope=_SCOPE, text="没被规则覆盖的口语问题？")
            ),
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="provider",
            selected_slot=None,
        ),
        allow_uncertain=True,
    )

    assert len(evidence) == 8
    assert later.hydrated.chunk.chunk_id in {item.chunk_id for item in evidence}
