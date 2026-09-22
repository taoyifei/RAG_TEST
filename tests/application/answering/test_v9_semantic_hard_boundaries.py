"""独立反例核对当前 Atom 的序数、主体绑定与矩阵不能覆盖的硬边界。"""

from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.application.answering.atom_semantics import current_atom_analysis
from rag_app.application.answering.request_relation import (
    RequestRelationStatus,
    decide_request_relation,
)
from rag_app.application.retrieval.semantics import parse_query_semantics
from rag_app.core.models import QueryAnalysis, QuerySemantics
from rag_app.core.models.query_plan import (
    AtomConstraintKind,
    AtomStatus,
    QueryConstraint,
)
from tests.application.answering.relation_review_fixtures import (
    fixed_review_generator,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _answer_with_pack,
)
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _draft,
    _evidence,
    _plan,
)


@pytest.mark.parametrize("provide_analysis", [False, True])
def test_current_atom_preserves_ordinal_with_equivalent_analysis(
    provide_analysis: bool,
) -> None:
    question = "安全员职责的第二条是什么？"
    atom = _plan(question).atoms[0]
    semantics = parse_query_semantics(question)
    assert semantics.ordinal == 2
    analysis = (
        QueryAnalysis(
            original_query=question,
            normalized_query=question,
            semantics=semantics,
            conversation_fingerprint="sha256:" + "0" * 64,
        )
        if provide_analysis
        else None
    )
    derived = current_atom_analysis(atom, analysis)
    assert derived.semantics.ordinal == 2


def test_request_relation_cannot_join_two_subjects_in_one_sentence() -> None:
    atom = _plan("甲部门保存记录").atoms[0]
    decision = decide_request_relation(
        current_atom_analysis(atom, None),
        "甲部门负责审批，乙部门负责保存记录。",
    )
    assert decision.status is not RequestRelationStatus.SUPPORTED


@pytest.mark.parametrize(
    "matrix_status", [AtomStatus.MISSING, AtomStatus.SUPPORTED]
)
def test_service_matrix_cannot_approve_explicit_other_stage(
    matrix_status: AtomStatus,
) -> None:
    source = "研发阶段甲部门负责现场交付。"
    plan = _plan("首单交付阶段甲部门职责")
    evidence = _evidence(source)
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", source, "A1", "S1"),),
        plan,
    )
    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        (
            (
                matrix_status,
                ("S1",) if matrix_status is AtomStatus.SUPPORTED else (),
            ),
        ),
    )
    assert outcome.accepted_claim_count == 0
    assert outcome.published_claim_count == 0
    assert outcome.answer is None


def test_current_atom_does_not_inherit_other_stage_for_same_role() -> None:
    atom = (
        _plan("甲部门")
        .atoms[0]
        .model_copy(
            update={
                "original_fragment": "首单交付阶段甲部门的职责有哪些？",
                "constraints": (
                    QueryConstraint(
                        kind=AtomConstraintKind.DATE_TIME,
                        value="首单交付阶段",
                    ),
                ),
            }
        )
    )
    analysis = QueryAnalysis(
        original_query="研发阶段甲部门有哪些职责，首单交付阶段又有哪些职责？",
        normalized_query="研发阶段甲部门有哪些职责，首单交付阶段又有哪些职责？",
        semantics=QuerySemantics(target="甲部门", context_qualifier="研发阶段"),
        conversation_fingerprint="sha256:" + "0" * 64,
    )
    derived = current_atom_analysis(atom, analysis)
    assert derived.semantics.context_qualifier != "研发阶段"


def test_batch_review_success_clears_final_rejection_counts() -> None:
    plan = _plan("这事咋操作呀？")
    evidence = _evidence("管理员负责核对记录。", "管理员负责保存记录。")
    draft = _draft(
        tuple(
            _claim(f"C{n}", item.citation_text, "A1", item.support_id)
            for n, item in enumerate(evidence, start=1)
        ),
        plan,
    )
    generator = fixed_review_generator(
        draft,
    )
    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )
    assert outcome.accepted_claim_count == 2
    assert outcome.published_claim_count == 2
    assert outcome.claim_rejection_codes == ()
    assert outcome.claim_rejection_diagnostics == ()
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert len(outcome.calls) == 2
    assert outcome.raw_failures == ()


def test_mixed_review_removes_only_the_accepted_claim_diagnostic() -> None:
    plan = _plan("这事咋操作呀？")
    evidence = _evidence("管理员负责核对记录。", "管理员负责保存记录。")
    draft = _draft(
        tuple(
            _claim(f"C{n}", item.citation_text, "A1", item.support_id)
            for n, item in enumerate(evidence, start=1)
        ),
        plan,
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= 2
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])
        if calls == 1:
            payload = {
                "claims": [
                    {
                        "atom_id": item.atom_id,
                        "text": item.text,
                        "refs": [
                            unit["unit_id"]
                            for unit in data["read_units"]
                            if any(
                                support.quote in unit["text"]
                                or unit["text"] in support.quote
                                for support in item.supports
                            )
                        ],
                    }
                    for item in draft.natural_claims
                ]
            }
        else:
            payload = {
                "results": [
                    {
                        "claim_id": item["claim_id"],
                        "status": "supported" if index == 1 else "unknown",
                    }
                    for index, item in enumerate(data["candidates"])
                ]
            }
        return httpx.Response(
            200,
            json={
                "model": "synthetic",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(payload, ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 80,
                    "total_tokens": 280,
                },
            },
        )

    generator = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic",
            egress_allowed=True,
            structured_output_mode="response_format",
        ),
        http_client=ProviderHttpClient(
            "https://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1,
        ),
        api_key_resolver=lambda: "",
    )
    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )
    assert outcome.accepted_claim_count == 1
    assert outcome.relation_review_calls == 1
    assert outcome.claim_rejection_codes == (("SEMANTIC_UNKNOWN", 1),)
    assert outcome.claim_rejection_diagnostics == ()


@pytest.mark.parametrize(
    "question,source,document",
    [
        ("甲部门要归档吗？", "乙部门需要归档。", "归档规范.docx"),
        ("根据甲手册，费用可以报销吗？", "费用可以报销。", "乙手册.docx"),
    ],
)
def test_yes_no_action_match_does_not_erase_role_or_source(
    question: str,
    source: str,
    document: str,
) -> None:
    plan = _plan(question)
    evidence = tuple(
        item.model_copy(
            update={"display_name": document, "source_label": document}
        )
        for item in _evidence(source)
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", source, "A1", "S1"),),
        plan,
    )
    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )
    assert outcome.accepted_claim_count == 0
    assert outcome.published_claim_count == 0
    assert outcome.answer is None


@pytest.mark.parametrize(
    "question,source,document",
    [
        ("甲部门要归档吗？", "甲部门需要归档。", "归档规范.docx"),
        ("根据甲手册，费用可以报销吗？", "费用可以报销。", "甲手册.docx"),
    ],
)
def test_yes_no_matching_role_and_source_remain_answerable(
    question: str,
    source: str,
    document: str,
) -> None:
    plan = _plan(question)
    evidence = tuple(
        item.model_copy(
            update={"display_name": document, "source_label": document}
        )
        for item in _evidence(source)
    )
    decision = decide_request_relation(
        current_atom_analysis(plan.atoms[0], None),
        source,
    )
    assert decision.status in {
        RequestRelationStatus.SUPPORTED,
        RequestRelationStatus.UNDETERMINED,
    }
    draft = _draft(
        (_claim("C1", source, "A1", "S1"),),
        plan,
    )
    generator = fixed_review_generator(
        draft,
    )
    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )
    assert outcome.accepted_claim_count == 1
    assert outcome.published_claim_count == 1
    assert outcome.answer is not None
    assert source in outcome.answer
    review_calls = 1
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert len(outcome.prepared_packets) == 1 + review_calls
    assert all(
        packet.evidence_level == "TRANSPORT_SENT"
        for packet in outcome.prepared_packets
    )


@pytest.mark.parametrize(
    "question,source,document",
    [
        ("甲部门要归档吗？", "乙部门需要归档。", "归档规范.docx"),
        ("根据甲手册，费用可以报销吗？", "费用可以报销。", "乙手册.docx"),
    ],
)
@pytest.mark.parametrize("punctuation", ["？", ""])
def test_semantic_contradiction_rejects_explicit_clause_scope(
    question: str,
    source: str,
    document: str,
    punctuation: str,
) -> None:
    question = question.rstrip("？") + punctuation
    plan = _plan(question)
    evidence = tuple(
        item.model_copy(
            update={"display_name": document, "source_label": document}
        )
        for item in _evidence(source)
    )
    draft = _draft((_claim("C1", source, "A1", "S1"),), plan)
    generator = fixed_review_generator(
        draft,
        statuses=("contradicted",),
    )
    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )
    assert outcome.accepted_claim_count == 0
    assert outcome.published_claim_count == 0
    assert outcome.answer is None
    assert outcome.relation_review_calls <= 1
    assert all(
        packet.evidence_level == "TRANSPORT_SENT"
        for packet in outcome.prepared_packets
    )
