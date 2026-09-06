"""unit_synthetic：冻结属性关系、标签单位和合法相邻来源组合。"""

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
from tests.application.retrieval.test_evidence_table_coordinates import _table

_FAMILIES = (
    ("温控柜", "采购价格", "3500元", "数量为3台", "干燥柜"),
    ("资料馆", "建筑面积", "125.5m²", "长度为30米", "展览馆"),
    ("巡检员", "联系电话", "021-76543210", "负责每日巡检", "维修员"),
    ("报修申请", "受理角色", "工程主管", "已经收到", "采购申请"),
    ("恒湿柜", "温度", "15℃", "数量为2台", "恒湿箱"),
    ("财务凭据", "保管期限", "30天", "按编号分类", "业务凭据"),
)


def _query(subject: str, attribute: str) -> str:
    if attribute == "受理角色":
        return f"{subject}应找谁？"
    return f"{subject}{attribute}是多少？"


def _statement(subject: str, attribute: str, value: str) -> str:
    if attribute == "受理角色":
        return f"{subject}由{value}受理。"
    return f"{subject}{attribute}为{value}。"


def _context(query: str) -> EvidenceSelectionContext:
    return EvidenceSelectionContext(
        analysis=QueryAnalyzer().analyze(
            SearchRequest(
                scope=KnowledgeBaseScope(
                    project_id=f"prj_{'a' * 32}",
                    knowledge_base_id=f"kb_{'b' * 32}",
                ),
                text=query,
            )
        ),
        query_kind=QueryKind.SIMPLE_FACT,
        rerank_mode="lexical_overlap",
    )


def _evaluate(
    query: str, candidates: tuple[RankedChunk, ...]
) -> tuple[tuple[EvidenceItem, ...], ConfidenceStatus]:
    context = _context(query)
    policy = RetrievalPolicy()
    evidence = EvidenceAssembler().assemble(candidates, policy, context=context)
    decision = ConfidenceEvaluator().evaluate(
        context.analysis,
        context.query_kind,
        candidates,
        evidence,
        (),
        policy=policy,
        rerank_mode=context.rerank_mode,
    )
    return evidence, decision.status


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize("scenario", ("present", "missing", "other", "unknown"))
def test_six_families_require_the_requested_objects_actual_attribute(
    family: tuple[str, str, str, str, str],
    scenario: str,
) -> None:
    subject, attribute, value, missing, other = family
    quote = {
        "present": _statement(subject, attribute, value),
        "missing": f"{subject}{missing}。",
        "other": f"{subject}{missing}；" + _statement(other, attribute, value),
        "unknown": f"{subject}{attribute}未提供。",
    }[scenario]

    evidence, status = _evaluate(
        _query(subject, attribute), (make_ranked_chunk(1, quote),)
    )

    assert (status is ConfidenceStatus.ANSWERABLE) is (scenario == "present")
    if scenario == "present":
        assert [item.citation_text for item in evidence] == [quote]
    else:
        assert not evidence


@pytest.mark.parametrize(
    ("query", "quote"),
    (
        ("登记员电话是多少？", "登记员邮箱为help@example.invalid。"),
        ("登记员邮箱是什么？", "登记员电话为020-65432109。"),
        ("登记员分机号是多少？", "登记员邮箱为desk@example.invalid。"),
        ("卷扬机采购价格是多少？", "卷扬机采购价格为45℃。"),
        ("档案库建筑面积是多少？", "档案库建筑面积为80米。"),
        ("保温柜温度是多少？", "保温柜温度为15kg。"),
        ("卷扬机采购价格是多少？", "卷扬机采购价格不是800元。"),
        ("档案库建筑面积是多少？", "档案库建筑面积不是800平方米。"),
    ),
)
def test_incompatible_value_types_and_negated_values_are_not_answers(
    query: str, quote: str
) -> None:
    evidence, status = _evaluate(query, (make_ranked_chunk(1, quote),))
    assert status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
    assert not evidence


@pytest.mark.parametrize(
    ("subject", "header", "value", "query"),
    (
        ("灌装机", "采购价格（元）", "4600", "灌装机采购价格是多少？"),
        ("培训馆", "建筑面积（m²）", "268", "培训馆建筑面积是多少？"),
        ("质检员", "分机号", "8247", "质检员分机号是多少？"),
        ("值守员", "邮箱", "help@example.invalid", "值守员邮箱是什么？"),
        ("保鲜柜", "温度（℃）", "6", "保鲜柜温度是多少？"),
        ("维修申请", "受理角色", "设备主管", "维修申请应找谁？"),
    ),
)
def test_table_headers_rows_and_values_jointly_support_without_rewriting_source(
    subject: str, header: str, value: str, query: str
) -> None:
    header_chunk = _table(1, rows=(("对象", header),), repeated=True)
    row_chunk = _table(2, rows=((subject, value),), first_row=1)

    evidence, status = _evaluate(query, (header_chunk, row_chunk))

    assert status is ConfidenceStatus.ANSWERABLE
    assert value in {item.citation_text for item in evidence}
    _assert_real_source_ranges(evidence, (header_chunk, row_chunk))


def _assert_real_source_ranges(
    evidence: tuple[EvidenceItem, ...], candidates: tuple[RankedChunk, ...]
) -> None:
    chunks = {
        item.hydrated.chunk.chunk_id: item.hydrated.chunk for item in candidates
    }
    for item in evidence:
        chunk = chunks[item.chunk_id]
        for actual in item.source_spans:
            original = next(
                span
                for span in chunk.source_spans
                if span.node_id == actual.node_id
                and span.structural_path == actual.structural_path
            )
            assert actual.source_start_char == original.source_start_char
            assert actual.source_end_char == original.source_end_char
            assert actual.source_anchor == original.source_anchor
            assert (
                item.citation_text
                == chunk.citation_text[
                    original.chunk_start_char : original.chunk_end_char
                ]
            )


class _Source:
    def __init__(self, values: tuple[HydratedChunk, ...]) -> None:
        self.values = values

    def hydrate_chunks(
        self, snapshot: ActiveRevisionQuerySnapshot, chunk_ids: tuple[str, ...]
    ) -> tuple[HydratedChunk, ...]:
        del snapshot
        return tuple(
            item for item in self.values if item.chunk.chunk_id in chunk_ids
        )


def _linked_pair(
    subject: str, attribute: str, value: str
) -> tuple[RankedChunk, RankedChunk]:
    first = make_ranked_chunk(1, subject, next_chunk_id=f"chunk_{2:032x}")
    second = make_ranked_chunk(
        2, _statement("", attribute, value), previous_chunk_id=f"chunk_{1:032x}"
    )
    return first, second


@pytest.mark.parametrize(
    ("subject", "attribute", "value", "missing", "other"), _FAMILIES
)
def test_legal_linked_subject_and_attribute_spans_support_jointly(
    subject: str, attribute: str, value: str, missing: str, other: str
) -> None:
    del missing, other
    first, second = _linked_pair(subject, attribute, value)
    outcome = NeighborExpander(
        cast(EvidenceSourcePort, _Source((second.hydrated,)))
    ).expand(
        cast(ActiveRevisionQuerySnapshot, object()),
        (first,),
        "same_group",
        RetrievalPolicy(),
    )
    assert outcome.degraded_reason_codes == ()

    evidence, status = _evaluate(_query(subject, attribute), outcome.candidates)

    assert status is ConfidenceStatus.ANSWERABLE
    assert {item.chunk_id for item in evidence} == {
        first.hydrated.chunk.chunk_id,
        second.hydrated.chunk.chunk_id,
    }
    _assert_real_source_ranges(evidence, (first, second))


@pytest.mark.parametrize(
    "boundary", ("scope", "revision", "document", "entity")
)
def test_context_cannot_borrow_another_identitys_attribute(
    boundary: str,
) -> None:
    first, second = _linked_pair("净水机", "采购价格", "950元")
    chunk = second.hydrated.chunk
    if boundary == "scope":
        chunk = chunk.model_copy(update={"knowledge_base_id": f"kb_{'9' * 32}"})
    elif boundary == "revision":
        chunk = chunk.model_copy(
            update={"index_revision_id": f"irev_{'9' * 32}"}
        )
    elif boundary == "document":
        chunk = chunk.model_copy(
            update={
                "version": make_ranked_chunk(
                    3, "其他", document_number=9
                ).hydrated.chunk.version
            }
        )
    else:
        chunk = make_ranked_chunk(
            2, "另一台碎纸机采购价格为950元。"
        ).hydrated.chunk.model_copy(
            update={"previous_chunk_id": first.hydrated.chunk.chunk_id}
        )
    second = second.model_copy(
        update={"hydrated": second.hydrated.model_copy(update={"chunk": chunk})}
    )

    evidence, status = _evaluate("净水机采购价格是多少？", (first, second))

    assert status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
    assert not evidence


@pytest.mark.parametrize("boundary", ("scope", "revision"))
def test_table_header_and_value_cannot_join_across_scope_or_revision(
    boundary: str,
) -> None:
    header = _table(1, rows=(("对象", "联系电话"),))
    row = _table(2, rows=(("装配员", "021-87654320"),), first_row=1)
    field = "knowledge_base_id" if boundary == "scope" else "index_revision_id"
    value = f"kb_{'9' * 32}" if boundary == "scope" else f"irev_{'9' * 32}"
    chunk = row.hydrated.chunk.model_copy(update={field: value})
    row = row.model_copy(
        update={"hydrated": row.hydrated.model_copy(update={"chunk": chunk})}
    )

    evidence, status = _evaluate("装配员联系电话是多少？", (header, row))

    assert status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
    assert not evidence


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize(
    "seed_mode",
    ("dense", "lexical", "mixed", "context_only", "unrelated_mixed"),
)
def test_joint_support_requires_its_own_seed_without_promoting_context(
    family: tuple[str, str, str, str, str], seed_mode: str
) -> None:
    subject, attribute, value, _, _ = family
    first, second = _linked_pair(subject, attribute, value)
    dense = make_ranked_chunk(1, subject, channel="dense:primary")
    if seed_mode == "dense":
        contributions = dense.contributions
    elif seed_mode == "lexical":
        contributions = first.contributions
    elif seed_mode == "mixed":
        contributions = (*dense.contributions, *first.contributions)
    else:
        contributions = ()
    first = first.model_copy(
        update={
            "contributions": contributions,
            "rerank_rank": 1 if contributions else None,
            "rerank_score": 0.87 if contributions else None,
            "expansion_reason": None if contributions else "SECTION_SIBLING",
            "expansion_seed_ids": () if contributions else (f"chunk_{9:032x}",),
        }
    )
    space = "primary:fake-semantic:joint-support:3:l2:1"
    policy = RetrievalPolicy(
        dense_semantic_enabled=True,
        dense_semantic_calibration_state="CONTROLLED_TEST_ONLY",
        dense_calibrated_vector_spaces=(space,),
    )
    context = _context(_query(subject, attribute)).model_copy(
        update={
            "rerank_mode": "provider",
            "selected_slot": "primary",
            "selected_vector_space": space,
        }
    )
    outcome = NeighborExpander(
        cast(EvidenceSourcePort, _Source((second.hydrated,)))
    ).expand(
        cast(ActiveRevisionQuerySnapshot, object()),
        (first,),
        "same_group",
        policy,
    )
    assert outcome.degraded_reason_codes == ()
    candidates = outcome.candidates
    expanded = candidates[1]
    assert not expanded.contributions
    assert expanded.rerank_rank is None
    assert expanded.rerank_score is None
    assert not semantic_candidate_allowed(expanded, policy, context)
    if seed_mode in {"dense", "mixed"}:
        assert semantic_candidate_allowed(first, policy, context)
    if seed_mode == "unrelated_mixed":
        noise = make_ranked_chunk(8, "园林绿化按季度维护。", document_number=9)
        noise = noise.model_copy(
            update={
                "contributions": (*dense.contributions, *noise.contributions),
                "rerank_rank": 1,
                "rerank_score": 0.92,
            }
        )
        candidates = (noise, *candidates)
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

    expected = seed_mode in {"dense", "lexical", "mixed"}
    assert (decision.status is ConfidenceStatus.ANSWERABLE) is expected
    if expected:
        assert {item.chunk_id for item in evidence} == {
            first.hydrated.chunk.chunk_id,
            second.hydrated.chunk.chunk_id,
        }
        _assert_real_source_ranges(evidence, (first, second))
