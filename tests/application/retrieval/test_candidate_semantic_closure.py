"""unit_synthetic：冻结候选级准入、事实支持及扩展开关对照。"""

from __future__ import annotations

from typing import cast

import pytest

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.evidence import (
    EvidenceAssembler,
    semantic_candidate_allowed,
)
from rag_app.application.retrieval.neighbors import NeighborExpander
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    EvidenceSelectionContext,
    HydratedChunk,
    KnowledgeBaseScope,
    QueryKind,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.core.ports import EvidenceSourcePort
from tests.application.retrieval.helpers import make_ranked_chunk

_SPACE = "primary:fake-semantic:closure:3:l2:1"
_STANDBY = "standby:fake-semantic:closure:3:l2:1"
_POLICY = RetrievalPolicy(
    dense_semantic_enabled=True,
    dense_semantic_calibration_state="CONTROLLED_TEST_ONLY",
    dense_calibrated_vector_spaces=(_SPACE,),
)
_ROLE_PAIRS = (
    ("办理资料纠正应找谁？", "资料更正申请由事务主管受理。"),
    ("谁负责登记申请？", "登记申请由接待员负责。"),
    ("费用复查应找哪位工作人员？", "费用复核申请由审计员受理。"),
    ("维修申请谁审核？", "维修申请由工程主管审核。"),
    ("数据更正应找谁？", "数据更正申请由管理员负责。"),
    ("证件登记由谁办理？", "证件登记由窗口专员负责。"),
)


def _context(query: str) -> EvidenceSelectionContext:
    scope = KnowledgeBaseScope(
        project_id=f"prj_{'a' * 32}",
        knowledge_base_id=f"kb_{'b' * 32}",
    )
    return EvidenceSelectionContext(
        analysis=QueryAnalyzer().analyze(
            SearchRequest(scope=scope, text=query)
        ),
        query_kind=QueryKind.SIMPLE_FACT,
        rerank_mode="provider",
        selected_slot="primary",
        selected_vector_space=_SPACE,
    )


def _candidate(quote: str, *, mixed: bool = False) -> RankedChunk:
    candidate = make_ranked_chunk(2, quote, channel="dense:primary")
    contributions = candidate.contributions
    if mixed:
        contributions += make_ranked_chunk(2, quote).contributions
    return candidate.model_copy(
        update={
            "rerank_rank": 1,
            "rerank_score": 0.83,
            "contributions": contributions,
        }
    )


def _evaluate(
    candidates: tuple[RankedChunk, ...],
    context: EvidenceSelectionContext,
    policy: RetrievalPolicy = _POLICY,
) -> tuple[tuple[EvidenceItem, ...], ConfidenceDecision]:
    evidence = EvidenceAssembler().assemble(candidates, policy, context=context)
    decision = ConfidenceEvaluator().evaluate(
        context.analysis,
        context.query_kind,
        candidates,
        evidence,
        (),
        policy=policy,
        rerank_mode=context.rerank_mode,
        selected_vector_space=context.selected_vector_space,
    )
    return evidence, decision


@pytest.mark.parametrize(("query", "quote"), _ROLE_PAIRS)
@pytest.mark.parametrize("mixed", (False, True))
@pytest.mark.parametrize("noise_mode", ("none", "lexical", "neighbor", "both"))
def test_each_semantic_candidate_survives_unrelated_channel_and_context(
    query: str, quote: str, mixed: bool, noise_mode: str
) -> None:
    candidate = _candidate(quote, mixed=mixed)
    lexical = make_ranked_chunk(1, "本周晴间多云，河面水位稳定。")
    neighbor = make_ranked_chunk(3, "桌椅和照明设施定期清洁。").model_copy(
        update={
            "contributions": (),
            "expansion_reason": "SAME_GROUP_NEIGHBOR",
            "expansion_seed_ids": (candidate.hydrated.chunk.chunk_id,),
        }
    )
    noise = {
        "none": (),
        "lexical": (lexical,),
        "neighbor": (neighbor,),
        "both": (lexical, neighbor),
    }[noise_mode]
    context = _context(query)

    evidence, decision = _evaluate((*noise, candidate), context)

    assert semantic_candidate_allowed(candidate, _POLICY, context)
    assert [item.citation_text for item in evidence] == [quote]
    assert (
        evidence[0].source_spans[0] == candidate.hydrated.chunk.source_spans[0]
    )
    assert decision.status is ConfidenceStatus.ANSWERABLE


@pytest.mark.parametrize(
    "boundary",
    (
        "wrong_space",
        "wrong_slot",
        "foreign_calibrated_slot",
        "ambiguous_space",
        "bypass",
        "missing_rank",
        "lexical",
        "new_neighbor",
    ),
)
def test_semantic_identity_requires_actual_matching_space_and_native_rank(
    boundary: str,
) -> None:
    candidate = _candidate(_ROLE_PAIRS[0][1])
    context = _context(_ROLE_PAIRS[0][0])
    policy = _POLICY
    if boundary == "wrong_space":
        context = context.model_copy(update={"selected_vector_space": "other"})
    elif boundary == "wrong_slot":
        context = context.model_copy(update={"selected_slot": "standby"})
    elif boundary == "foreign_calibrated_slot":
        policy = policy.model_copy(
            update={"dense_calibrated_vector_spaces": (_SPACE, _STANDBY)}
        )
        context = context.model_copy(update={"selected_vector_space": _STANDBY})
    elif boundary == "ambiguous_space":
        policy = policy.model_copy(
            update={"dense_calibrated_vector_spaces": (_SPACE, _SPACE + ":new")}
        )
        context = context.model_copy(update={"selected_vector_space": None})
    elif boundary == "bypass":
        context = context.model_copy(
            update={"rerank_mode": "rerank_bypassed_provider_unavailable"}
        )
    elif boundary == "missing_rank":
        candidate = candidate.model_copy(update={"rerank_rank": None})
    elif boundary == "lexical":
        candidate = candidate.model_copy(
            update={"contributions": make_ranked_chunk(2, "合成").contributions}
        )
    else:
        # 即使外部对象错误地带上 dense/rerank，扩展标记也不能伪装原命中。
        candidate = candidate.model_copy(
            update={"expansion_reason": "SAME_GROUP_NEIGHBOR"}
        )

    assert not semantic_candidate_allowed(candidate, policy, context)


@pytest.mark.parametrize(("query", "quote"), _ROLE_PAIRS)
def test_rerank_bypass_does_not_grant_dense_only_answerability(
    query: str, quote: str
) -> None:
    context = _context(query).model_copy(
        update={"rerank_mode": "rerank_bypassed_circuit_open"}
    )
    candidate = _candidate(quote).model_copy(
        update={"rerank_rank": None, "rerank_score": None}
    )

    _, decision = _evaluate((candidate,), context)

    assert not semantic_candidate_allowed(candidate, _POLICY, context)
    assert decision.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize(("query", "quote"), _ROLE_PAIRS)
def test_mixed_candidate_can_use_real_lexical_support_during_rerank_bypass(
    query: str, quote: str
) -> None:
    context = _context(query).model_copy(
        update={"rerank_mode": "rerank_bypassed_circuit_open"}
    )
    candidate = _candidate(quote, mixed=True).model_copy(
        update={"rerank_rank": None, "rerank_score": None}
    )

    evidence, decision = _evaluate((candidate,), context)

    assert not semantic_candidate_allowed(candidate, _POLICY, context)
    assert [item.citation_text for item in evidence] == [quote]
    assert decision.status is ConfidenceStatus.ANSWERABLE


class _LinkedSource:
    def __init__(self, chunks: tuple[HydratedChunk, ...]) -> None:
        self.chunks = chunks

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        del snapshot
        return tuple(
            item for item in self.chunks if item.chunk.chunk_id in chunk_ids
        )


@pytest.mark.parametrize(("query", "quote"), _ROLE_PAIRS)
@pytest.mark.parametrize("reverse", (False, True))
def test_expansion_switch_cannot_remove_a_direct_answerable_identity(
    query: str, quote: str, reverse: bool
) -> None:
    answer = _candidate(quote)
    noise = make_ranked_chunk(
        1,
        "园林道路每周清扫一次。",
        next_chunk_id=answer.hydrated.chunk.chunk_id,
    ).model_copy(update={"rerank_rank": 2, "rerank_score": 0.11})
    chunk = answer.hydrated.chunk.model_copy(
        update={"previous_chunk_id": noise.hydrated.chunk.chunk_id}
    )
    answer = answer.model_copy(
        update={"hydrated": answer.hydrated.model_copy(update={"chunk": chunk})}
    )
    candidates = (noise, answer) if reverse else (answer, noise)
    source = _LinkedSource(tuple(item.hydrated for item in candidates))
    expander = NeighborExpander(cast(EvidenceSourcePort, source))
    context = _context(query)
    results = []
    for mode in ("none", "same_group"):
        expanded = expander.expand(
            cast(ActiveRevisionQuerySnapshot, object()),
            candidates,
            mode,
            _POLICY,
        )
        assert expanded.degraded_reason_codes == ()
        assert expanded.candidates == candidates
        evidence, decision = _evaluate(expanded.candidates, context)
        assert decision.status is ConfidenceStatus.ANSWERABLE
        results.append(evidence)
    assert results[0] == results[1]


@pytest.mark.parametrize(
    ("query", "quote"),
    (
        ("查询资料应找谁？", "河面水位由观测员登记。"),
        ("图书馆面积多大？", "图书馆位于三楼。"),
        ("打印机采购价格多少？", "打印机数量为3台。"),
        ("接待员电话是多少？", "接待申请由接待员受理。"),
        ("冷却设备温度多少？", "冷却设备品牌为南星。"),
        ("车辆载荷上限多少？", "车辆在停车场内存放。"),
    ),
)
def test_semantic_admission_never_replaces_requested_fact_support(
    query: str, quote: str
) -> None:
    candidate = _candidate(quote)
    context = _context(query)

    evidence, decision = _evaluate((candidate,), context)

    assert semantic_candidate_allowed(candidate, _POLICY, context)
    assert not evidence
    assert decision.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE


def test_remote_semantic_candidate_can_reach_model_review_only() -> None:
    query = "雨林巡护协议是什么？"
    quote = "用于核对设备标识并记录批准日期。"
    candidate = _candidate(quote)
    context = _context(query)

    selection = EvidenceAssembler().assemble_sets(
        (candidate,),
        _POLICY,
        context=context,
        include_model_candidates=True,
    )

    assert semantic_candidate_allowed(candidate, _POLICY, context)
    assert selection.answer_support_set == ()
    assert [
        item.citation_text for item in selection.model_evidence_candidates
    ] == [quote]
