from __future__ import annotations

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    ChunkRole,
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryKind,
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
