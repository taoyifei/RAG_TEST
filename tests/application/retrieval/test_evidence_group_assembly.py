"""EvidenceGroup 进入现有引用和支持集后的边界测试。"""

from __future__ import annotations

from pytest import MonkeyPatch

from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.application.retrieval.evidence_groups import (
    GroupCandidate,
    build_evidence_groups,
)
from rag_app.core.models import (
    ChunkRole,
    EvidenceItem,
    EvidenceSelectionContext,
    QueryAnalysis,
    QueryKind,
    QuerySemantics,
    RankedChunk,
    RequestedAnswerType,
    RetrievalPolicy,
)
from rag_app.core.models.common import freeze_json_object
from tests.application.retrieval.helpers import make_ranked_chunk


def _procedure() -> tuple[tuple[RankedChunk, ...], GroupCandidate]:
    """构造首尾闭合且带章节导语的两个原文步骤。"""
    first = make_ranked_chunk(
        1,
        "申请人提交材料。",
        role=ChunkRole.LIST,
        neighbor_group_id="steps",
        next_chunk_id=f"chunk_{2:032x}",
    )
    second = make_ranked_chunk(
        2,
        "经办人三日内核验。",
        role=ChunkRole.LIST,
        neighbor_group_id="steps",
        previous_chunk_id=f"chunk_{1:032x}",
    )
    members = tuple(_with_heading(item) for item in (first, second))
    groups = build_evidence_groups(
        members,
        max_groups=2,
        max_member_chunks=4,
        rerank_text_char_limit=1000,
    )
    assert len(groups) == 1 and groups[0].complete
    return members, groups[0]


def _with_heading(candidate: RankedChunk) -> RankedChunk:
    """在合成来源上保留原章节路径。"""
    chunk = candidate.hydrated.chunk.model_copy(
        update={"heading_path": ("办理流程",)}
    )
    return candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )


def _policy(*, token_budget: int = 20) -> RetrievalPolicy:
    """让两条短引用刚好受本测试的预算约束。"""
    return RetrievalPolicy(
        evidence_token_budget=token_budget,
        max_evidence_items=2,
        per_document_cap=2,
        per_section_cap=2,
    )


def _procedure_context() -> EvidenceSelectionContext:
    """构造不依赖分类器规则的结构型查询上下文。"""
    question = "办理流程有哪些步骤？"
    return EvidenceSelectionContext(
        analysis=QueryAnalysis(
            original_query=question,
            normalized_query=question,
            conversation_fingerprint=f"sha256:{'0' * 64}",
            semantics=QuerySemantics(
                target="办理流程",
                relation="步骤",
                answer_type=RequestedAnswerType.PROCEDURE,
            ),
        ),
        query_kind=QueryKind.COMPLEX,
        rerank_mode="fixture",
    )


def _fact_context() -> EvidenceSelectionContext:
    """单一事实可由经逐 span 校验的局部来源支持。"""
    question = "申请人提交什么？"
    return EvidenceSelectionContext(
        analysis=QueryAnalysis(
            original_query=question,
            normalized_query=question,
            conversation_fingerprint=f"sha256:{'1' * 64}",
            semantics=QuerySemantics(
                target="申请人",
                relation="提交",
                answer_type=RequestedAnswerType.FACT,
            ),
        ),
        query_kind=QueryKind.SIMPLE_FACT,
        rerank_mode="fixture",
    )


def _supported(candidate: EvidenceItem) -> EvidenceItem:
    """仅为支持集筛选测试标记已由原文关系校验通过。"""
    return candidate.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    **dict(candidate.metadata),
                    "answer_support": {
                        "status": "SUPPORTED",
                        "supporting_span_ids": [],
                    },
                }
            )
        }
    )


def test_complete_group_preserves_original_citations_and_legacy_path() -> None:
    members, group = _procedure()
    assembler = EvidenceAssembler()

    selected = assembler.assemble_sets(members, _policy(), groups=(group,))
    legacy = assembler.assemble_sets(members, _policy())

    selected_quotes = tuple(
        item.citation_text for item in selected.answer_support_set
    )
    assert selected_quotes == (
        "申请人提交材料。",
        "经办人三日内核验。",
    )
    assert tuple(item.citation_text for item in legacy.answer_support_set) == (
        "申请人提交材料。",
        "经办人三日内核验。",
    )
    assert all(
        "evidence_group_id" not in dict(item.metadata)
        for item in legacy.model_evidence_candidates
    )
    for index, item in enumerate(selected.model_evidence_candidates, 1):
        metadata = dict(item.metadata)
        assert metadata["evidence_group_id"] == group.group_id
        assert metadata["evidence_group_type"] == "PROCEDURE_GROUP"
        assert metadata["group_member_index"] == index
        assert metadata["group_member_count"] == 2
        assert metadata["group_complete"] is True
        assert metadata["group_completeness_reason"] == "COMPLETE"
        assert item.source_spans[0].source_start_char == 0


def test_budget_limited_group_is_only_a_model_candidate() -> None:
    members, group = _procedure()

    selected = EvidenceAssembler().assemble_sets(
        members,
        _policy(token_budget=3),
        groups=(group,),
    )

    assert len(selected.model_evidence_candidates) == 1
    assert selected.answer_support_set == ()
    metadata = dict(selected.model_evidence_candidates[0].metadata)
    assert metadata["group_complete"] is False
    assert metadata["group_completeness_reason"] == (
        "EVIDENCE_MEMBER_NOT_SELECTED;EVIDENCE_SOURCE_SPAN_NOT_SELECTED"
    )
    assert selected.rejected_candidate_reasons == (
        (members[0].hydrated.chunk.chunk_id, "INCOMPLETE_EVIDENCE_GROUP"),
    )


def test_procedure_support_requires_every_group_member(
    monkeypatch: MonkeyPatch,
) -> None:
    members, group = _procedure()
    assembler = EvidenceAssembler()
    raw = assembler.assemble(members, _policy())
    supported = tuple(_supported(item) for item in raw)
    monkeypatch.setattr(
        assembler,
        "_assemble_candidates",
        lambda *_args, **_kwargs: supported,
    )

    complete = assembler.assemble_sets(
        members,
        _policy(),
        context=_procedure_context(),
        groups=(group,),
    )
    assert tuple(item.chunk_id for item in complete.answer_support_set) == (
        group.group.member_chunk_ids
    )

    partial_support = (supported[0], raw[1])
    monkeypatch.setattr(
        assembler,
        "_assemble_candidates",
        lambda *_args, **_kwargs: partial_support,
    )
    incomplete = assembler.assemble_sets(
        members,
        _policy(),
        context=_procedure_context(),
        groups=(group,),
    )
    assert len(incomplete.model_evidence_candidates) == 2
    assert incomplete.answer_support_set == ()


def test_fact_support_keeps_valid_span_without_complete_group(
    monkeypatch: MonkeyPatch,
) -> None:
    """事实问句不因其他成员未装入而丢弃已验证的引用。"""
    members, group = _procedure()
    assembler = EvidenceAssembler()
    raw = assembler.assemble(members, _policy())
    monkeypatch.setattr(
        assembler,
        "_assemble_candidates",
        lambda *_args, **_kwargs: (_supported(raw[0]),),
    )

    selected = assembler.assemble_sets(
        members,
        _policy(),
        context=_fact_context(),
        groups=(group,),
    )

    assert len(selected.answer_support_set) == 1
    assert selected.answer_support_set[0].citation_text == "申请人提交材料。"
    assert (
        dict(selected.answer_support_set[0].metadata)["group_complete"] is False
    )
