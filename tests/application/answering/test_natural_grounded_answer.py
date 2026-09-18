"""WB08R-03 自然事实、逐原子支持与局部修复的离线回归。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from rag_app.adapters.providers.aliyun_chat import (
    ChatCompletion,
    ChatUsage,
    _natural_answer_draft,
    _natural_messages,
    message_token_estimate,
)
from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
)
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    AnswerDraft,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    ProviderCall,
    QueryAnalysis,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
    make_query_plan,
)
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
from rag_app.core.ports import GenerationRequest
from tests.application.retrieval.test_descriptive_answers import (
    _POLICY,
    _candidates,
    _paragraph,
)
from tests.application.retrieval.test_evidence_table_coordinates import _context


def _evidence(*sentences: str) -> tuple[EvidenceItem, ...]:
    """生成带真实 SourceSpan 的合成 DOCX 证据。"""
    return EvidenceAssembler().assemble(
        _candidates("".join(_paragraph(item) for item in sentences)), _POLICY
    )


def _plan(
    *targets: str, shape: AtomAnswerShape = AtomAnswerShape.FACT
) -> QueryPlan:
    """构造只含通用 Atom 的单次计划。"""
    atoms = tuple(
        QueryAtom(
            atom_id=f"A{index}",
            target=target,
            relation="规定",
            answer_shape=shape,
            original_fragment=f"{target}的规定",
        )
        for index, target in enumerate(targets, 1)
    )
    return make_query_plan(
        standalone_query="、".join(targets),
        intent="FACT",
        effort="DEEP" if len(atoms) > 1 else "DIRECT",
        atoms=atoms,
        reason_code="SYNTHETIC",
        planner_called=len(atoms) > 1,
    )


def _matrix(
    plan: QueryPlan,
    mappings: tuple[tuple[AtomStatus, tuple[str, ...]], ...],
) -> AtomSupportMatrix:
    """由明确的 Atom-to-Support 映射构造支持矩阵。"""
    return AtomSupportMatrix(
        atoms=tuple(
            AtomSupport(
                atom_id=atom.atom_id,
                status=status,
                supporting_support_ids=ids,
            )
            for atom, (status, ids) in zip(plan.atoms, mappings, strict=True)
        )
    )


def _draft(claims: tuple[NaturalClaim, ...], _plan: QueryPlan) -> AnswerDraft:
    """模拟带逐字 Quote 的自然事实草稿，不提供覆盖或 Claim ID。"""
    return AnswerDraft(
        text="\n".join(item.text for item in claims) or "未找到明确规定。",
        cited_evidence_ids=tuple(
            support.support_id for item in claims for support in item.supports
        ),
        natural_claims=claims,
        generation_mode="natural",
    )


def _claim(
    claim_id: str,
    text: str,
    atom_id: str,
    support_id: str,
    quote: str | None = None,
) -> NaturalClaim:
    """保留合成样本标签；模型协议本身不含 Claim ID。"""
    assert claim_id.startswith("C")
    return NaturalClaim(
        atom_id=atom_id,
        text=text,
        supports=(ClaimSupport(support_id=support_id, quote=quote or text),),
    )


@dataclass(frozen=True)
class _AnswerOptions:
    """可选的模拟流回调与共享分析。"""

    on_claim: Callable[..., None] | None = None
    analysis: QueryAnalysis | None = None


def _answer(
    generator: Mock,
    evidence: tuple[EvidenceItem, ...],
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    *,
    options: _AnswerOptions | None = None,
) -> GroundedOutcome:
    options = options or _AnswerOptions()
    return GroundedAnsweringService(generator).answer(
        plan.standalone_query,
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=plan,
        atom_support_matrix=matrix,
        analysis=options.analysis,
        on_claim=options.on_claim,
        cancellation=Mock(is_cancelled=Mock(return_value=False)),
    )


def test_supported_natural_paraphrase_keeps_server_quote() -> None:
    evidence = _evidence(
        "甲部门负责人负责核对交付标准，协调维护资源并组织最终验收。"
    )
    plan = _plan("甲部门负责人")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "甲部门负责人负责核对标准、协调资源和组织验收。",
                "A1",
                "S1",
                "甲部门负责人负责核对交付标准，协调维护资源并组织最终验收。",
            ),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer == (
        "甲部门负责人负责核对标准、协调资源和组织验收。 [S1]"
    )
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.published_support_ids == ("S1",)
    assert outcome.accepted_support_ids == ("S1",)
    assert outcome.accepted_claim_count == 1
    assert outcome.published_claim_count == 1
    assert generator.generate.call_count == 1


def test_changed_number_is_not_published_after_local_repair() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "甲部门保存记录 15 天。",
                "A1",
                "S1",
                "甲部门保存记录 14 天。",
            ),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is None
    assert outcome.reason_code == "CLAIM_NOT_SUPPORTED"
    assert outcome.atom_coverage == (("A1", "MISSING"),)
    assert outcome.repair_calls == 0
    assert outcome.claim_rejection_codes == (("CLAIM_NUMBER_MISMATCH", 1),)
    assert generator.generate.call_count == 1


def test_changed_negation_is_not_published() -> None:
    evidence = _evidence("甲部门不得自行销毁记录。")
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "甲部门可以自行销毁记录。",
                "A1",
                "S1",
                "甲部门不得自行销毁记录。",
            ),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is None
    assert outcome.reason_code == "CLAIM_NOT_SUPPORTED"
    assert outcome.repair_calls == 0
    assert outcome.claim_rejection_codes == (("CLAIM_MODALITY_MISMATCH", 1),)


def test_permission_cannot_be_rewritten_as_obligation() -> None:
    evidence = _evidence("甲部门可以延后提交。")
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1", "甲部门必须延后提交。", "A1", "S1", "甲部门可以延后提交。"
            ),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is None
    assert outcome.reason_code == "CLAIM_NOT_SUPPORTED"


def test_v7_adapter_rejects_quote_outside_supports() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    request = GenerationRequest(
        query=plan.standalone_query,
        evidence=evidence,
        citation_protocol="support-id-v2-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
    )
    payload = {
        "claims": [
            {
                "atom_id": "A1",
                "text": "甲部门保存记录 14 天。",
                "supports": [
                    {"support_id": "S1", "quote": "甲部门保存记录 14 天。"}
                ],
                "quote": "甲部门保存记录 14 天。",
            }
        ],
    }
    completion = ChatCompletion(
        content=json.dumps(payload, ensure_ascii=False),
        model="synthetic-model",
        usage=ChatUsage(),
        call=ProviderCall(
            provider_id="synthetic",
            operation="generation",
            call_count=1,
            retry_count=0,
            elapsed_ms=1,
        ),
    )

    with pytest.raises(ValueError):
        _natural_answer_draft(completion, request)


def test_repair_prompt_contains_only_missing_atom_and_its_evidence() -> None:
    evidence = _evidence("甲部门保存记录 14 天。", "乙部门审核记录 3 天。")
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲部门", "乙部门")
    matrix = _matrix(
        plan,
        (
            (AtomStatus.SUPPORTED, (by_text["甲部门保存记录 14 天。"],)),
            (AtomStatus.SUPPORTED, (by_text["乙部门审核记录 3 天。"],)),
        ),
    )
    repair_evidence = tuple(
        item
        for item in evidence
        if item.support_id == by_text["乙部门审核记录 3 天。"]
    )
    request = GenerationRequest(
        query=plan.standalone_query,
        evidence=repair_evidence,
        citation_protocol="support-id-v2-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
        repair_atom_ids=("A2",),
        accepted_claim_ids=("C1",),
    )

    messages = _natural_messages(request)
    payload = json.loads(messages[1].content)

    assert "question" not in payload
    assert [item["atom_id"] for item in payload["atoms"]] == ["A2"]
    assert [item["support_id"] for item in payload["evidence"]] == [
        by_text["乙部门审核记录 3 天。"]
    ]
    assert payload["accepted_claim_ids"] == ["C1"]


def test_natural_prompt_omits_internal_coordinates() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    request = GenerationRequest(
        query=plan.standalone_query,
        evidence=evidence,
        citation_protocol="support-id-v2-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
    )

    payload = json.loads(_natural_messages(request)[1].content)
    projected = payload["evidence"][0]

    assert projected["text"] == evidence[0].citation_text
    assert projected["support_id"] == "S1"
    assert "anchors" not in projected["source_structure"]
    assert "document_version_id" not in projected["source_structure"]
    assert evidence[0].source_spans


def test_natural_prompt_prunes_complete_groups_within_budget() -> None:
    """多个完整来源组超预算时，保留一个完整组并整组裁剪其余组。"""
    evidence = _evidence(
        *(f"第{index}条规定：" + "甲部门按流程记录。" * 8 for index in range(6))
    )
    grouped = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": f"egrp_{index // 2:032x}",
                        "group_complete": True,
                        "group_member_index": index % 2 + 1,
                        "group_member_count": 2,
                    }
                )
            }
        )
        for index, item in enumerate(evidence)
    )
    plan = _plan("甲部门")
    matrix = _matrix(
        plan,
        ((AtomStatus.SUPPORTED, tuple(item.support_id for item in grouped)),),
    )
    request = GenerationRequest(
        query=plan.standalone_query,
        evidence=grouped,
        model_evidence_candidates=grouped,
        citation_protocol="support-id-v2-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
    )
    full_estimate = message_token_estimate(_natural_messages(request))

    messages = _natural_messages(request, max_input_tokens=full_estimate - 1)
    selected = {
        item["support_id"]
        for item in json.loads(messages[1].content)["evidence"]
    }

    assert message_token_estimate(messages) <= full_estimate - 1
    assert {item.support_id for item in grouped[:2]} <= selected
    for start in (0, 2, 4):
        members = {item.support_id for item in grouped[start : start + 2]}
        assert members <= selected or members.isdisjoint(selected)


def test_natural_prompt_protects_named_table_row_when_budget_is_tight() -> None:
    """较晚的同名表格行不能被较早的无关正文挤出模型输入。"""
    evidence = _evidence(
        "通用模式的维护说明。" * 12,
        "通用模式的归档说明。" * 12,
        "快捷模式",
        "输入：提交任务说明和验收标准。",
        "启动：经会议评审通过后启动。",
    )
    grouped = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": (
                            "egrp_unrelated" if index < 2 else "egrp_named_row"
                        ),
                        "evidence_group_type": (
                            "PARAGRAPH_GROUP"
                            if index < 2
                            else "TABLE_ROW_GROUP"
                        ),
                        "group_complete": True,
                        "group_member_index": (
                            index + 1 if index < 2 else index - 1
                        ),
                        "group_member_count": 2 if index < 2 else 3,
                    }
                )
            }
        )
        for index, item in enumerate(evidence)
    )
    plan = _plan("快捷模式输入和启动").model_copy(
        update={
            "original_query": "快捷模式需要哪些输入，如何启动？",
            "resolved_root_query": "快捷模式需要哪些输入，如何启动？",
        }
    )
    matrix = _matrix(plan, ((AtomStatus.MISSING, ()),))
    request = GenerationRequest(
        query=plan.standalone_query,
        evidence=grouped,
        model_evidence_candidates=grouped,
        citation_protocol="support-id-v2-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
    )
    full_estimate = message_token_estimate(_natural_messages(request))

    messages = _natural_messages(request, max_input_tokens=full_estimate - 1)
    selected = {
        item["support_id"]
        for item in json.loads(messages[1].content)["evidence"]
    }

    assert {item.support_id for item in grouped[2:]} <= selected
    assert {item.support_id for item in grouped[:2]}.isdisjoint(selected)


def test_natural_prompt_protects_direct_support_without_group() -> None:
    """直接支持即使没有闭合组，也优先保留给模型核验。"""
    evidence = _evidence(
        "无关项目的归档记录。" * 16,
        "负责人以电话报送值班人员，须在30分钟内完成。",
    )
    grouped = (
        evidence[0].model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(evidence[0].metadata),
                        "evidence_group_id": "egrp_unrelated",
                        "group_complete": True,
                    }
                )
            }
        ),
        evidence[1],
    )
    plan = _plan("报送方式和时限")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, (evidence[1].support_id,)),))
    request = GenerationRequest(
        query=plan.standalone_query,
        evidence=grouped,
        model_evidence_candidates=grouped,
        citation_protocol="support-id-v2-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
    )
    full_estimate = message_token_estimate(_natural_messages(request))

    payload = json.loads(
        _natural_messages(request, max_input_tokens=full_estimate - 1)[
            1
        ].content
    )

    assert [item["support_id"] for item in payload["evidence"]] == [
        evidence[1].support_id
    ]


def test_incomplete_enumeration_remains_limited() -> None:
    question = "哪些情况下无需审批直接归档？"
    intro = "对于以下情形，无需审批，提交后直接归档。"
    evidence = EvidenceAssembler().assemble(
        _candidates(
            _paragraph(intro)
            + _paragraph("1.已核验的设备记录")
            + _paragraph("2.主管已签字的交接单")
            + _paragraph("3.已完成复验的材料清单")
        ),
        _POLICY,
        context=_context(question),
    )
    plan = _plan("归档", shape=AtomAnswerShape.ENUMERATION)
    matrix = _matrix(
        plan,
        ((AtomStatus.SUPPORTED, tuple(item.support_id for item in evidence)),),
    )
    intro_id = next(
        item.support_id for item in evidence if item.citation_text == intro
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", intro, "A1", intro_id),), plan
    )

    outcome = _answer(
        generator,
        evidence,
        plan,
        matrix,
        options=_AnswerOptions(analysis=_context(question).analysis),
    )

    assert outcome.atom_coverage == (("A1", "PARTIAL"),)
    assert outcome.answer is not None
    assert "已检索到相关资料，但本次未能完整组织全部内容" in outcome.answer
    assert "现有资料没有明确说明" not in outcome.answer
    assert outcome.missing_atom_reasons == (("A1", "GENERATION_INCOMPLETE"),)
    assert outcome.generation_gap_count == 1
    assert outcome.repair_calls == 1
    assert "归档的规定" not in outcome.answer
    assert generator.generate.call_count == 2


def test_renderer_deduplicates_same_fact_and_support() -> None:
    evidence = _evidence("甲部门负责审核材料。")
    plan = _plan("甲部门", shape=AtomAnswerShape.DUTIES)
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    generator = Mock()
    draft = _draft(
        (
            _claim("C1", "甲部门负责审核材料。", "A1", "S1"),
            _claim("C2", "甲部门负责审核材料。", "A1", "S1"),
        ),
        plan,
    )
    generator.generate.return_value = draft
    generator.generate_stream.return_value = draft

    emitted: list[object] = []
    outcome = _answer(
        generator,
        evidence,
        plan,
        matrix,
        options=_AnswerOptions(on_claim=emitted.append),
    )

    assert outcome.answer is not None
    assert outcome.answer.count("甲部门负责审核材料。") == 1
    assert emitted == []


def test_partial_candidate_can_be_upgraded_only_after_local_validation() -> (
    None
):
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.PARTIAL, ("S1",)),))
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", "甲部门保存记录 14 天。", "A1", "S1"),), plan
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.answer == "甲部门保存记录 14 天。 [S1]"


def test_supported_and_missing_atoms_make_limited_answer() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门", "乙部门")
    matrix = _matrix(
        plan,
        ((AtomStatus.SUPPORTED, ("S1",)), (AtomStatus.MISSING, ())),
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", "甲部门保存记录 14 天。", "A1", "S1"),), plan
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is not None
    assert "当前资料能够确认的是" in outcome.answer
    assert "乙部门的规定" in outcome.answer
    assert outcome.atom_coverage == (("A1", "SUPPORTED"), ("A2", "MISSING"))
    assert generator.generate.call_count == 1


def test_missing_list_atoms_do_not_repeat_the_same_disclaimer() -> None:
    evidence = _evidence("甲类属于所问集合。")
    plan = _plan("甲类", "乙类", "丙类", shape=AtomAnswerShape.ENUMERATION)
    matrix = _matrix(
        plan,
        (
            (AtomStatus.SUPPORTED, ("S1",)),
            (AtomStatus.MISSING, ()),
            (AtomStatus.MISSING, ()),
        ),
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", "甲类属于所问集合。", "A1", "S1"),), plan
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is not None
    assert outcome.answer.count("该主题的完整列表") == 1


def test_all_missing_atoms_refuse_without_generation() -> None:
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.MISSING, ()),))
    generator = Mock()

    outcome = _answer(generator, (), plan, matrix)

    assert outcome.answer is None
    assert outcome.atom_coverage == (("A1", "MISSING"),)
    generator.generate.assert_not_called()


def test_conflicting_sources_are_displayed_without_adjudication() -> None:
    evidence = _evidence("甲部门保存记录 14 天。", "甲部门保存记录 30 天。")
    plan = _plan("甲部门")
    matrix = _matrix(
        plan,
        (
            (
                AtomStatus.CONTRADICTORY,
                tuple(item.support_id for item in evidence),
            ),
        ),
    )
    generator = Mock()

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is not None
    assert "当前资料存在不一致" in outcome.answer
    assert "14 天" in outcome.answer and "30 天" in outcome.answer
    assert outcome.atom_coverage == (("A1", "CONTRADICTORY"),)
    generator.generate.assert_not_called()


def test_repair_only_contains_omitted_supported_atom() -> None:
    evidence = _evidence("甲部门保存记录 14 天。", "乙部门审核记录 3 天。")
    by_text = {item.citation_text: item.support_id for item in evidence}
    first_id = by_text["甲部门保存记录 14 天。"]
    second_id = by_text["乙部门审核记录 3 天。"]
    plan = _plan("甲部门", "乙部门")
    matrix = _matrix(
        plan,
        (
            (AtomStatus.SUPPORTED, (first_id,)),
            (AtomStatus.SUPPORTED, (second_id,)),
        ),
    )
    generator = Mock()
    generator.generate.side_effect = (
        _draft((_claim("C1", "甲部门保存记录 14 天。", "A1", first_id),), plan),
        _draft((_claim("C2", "乙部门审核记录 3 天。", "A2", second_id),), plan),
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is not None
    assert "甲部门保存记录 14 天" in outcome.answer
    assert "乙部门审核记录 3 天" in outcome.answer
    assert outcome.repair_calls == 1
    assert generator.generate.call_count == 2
    repair_request: GenerationRequest = generator.generate.call_args_list[
        1
    ].args[0]
    assert repair_request.repair_atom_ids == ("A2",)
    assert repair_request.accepted_claim_ids == ("C1",)
    assert tuple(item.support_id for item in repair_request.evidence) == (
        second_id,
    )


def test_multi_atom_stream_keeps_claims_private_until_final() -> None:
    evidence = _evidence("甲部门保存记录 14 天。", "乙部门审核记录 3 天。")
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲部门", "乙部门")
    matrix = _matrix(
        plan,
        (
            (AtomStatus.SUPPORTED, (by_text["甲部门保存记录 14 天。"],)),
            (AtomStatus.SUPPORTED, (by_text["乙部门审核记录 3 天。"],)),
        ),
    )
    emitted: list[object] = []
    draft = _draft(
        (
            _claim(
                "C1",
                "甲部门保存记录 14 天。",
                "A1",
                by_text["甲部门保存记录 14 天。"],
            ),
            _claim(
                "C2",
                "乙部门审核记录 3 天。",
                "A2",
                by_text["乙部门审核记录 3 天。"],
            ),
        ),
        plan,
    )

    generator = Mock()
    generator.generate.return_value = draft

    outcome = _answer(
        generator,
        evidence,
        plan,
        matrix,
        options=_AnswerOptions(on_claim=emitted.append),
    )

    assert outcome.answer is not None
    assert emitted == []
    generator.generate_stream.assert_not_called()
    assert outcome.repair_calls == 0


def test_one_generic_claim_cannot_certify_multiple_atoms() -> None:
    """多个子问题不能仅凭一条笼统事实被标记为全部已回答。"""
    evidence = _evidence("甲部门保存记录 14 天。", "乙部门审核记录 3 天。")
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲部门", "乙部门")
    matrix = _matrix(
        plan,
        (
            (AtomStatus.SUPPORTED, (by_text["甲部门保存记录 14 天。"],)),
            (AtomStatus.SUPPORTED, (by_text["乙部门审核记录 3 天。"],)),
        ),
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            NaturalClaim(
                atom_id="A1",
                text="甲部门保存记录 14 天。",
                supports=tuple(
                    ClaimSupport(
                        support_id=item.support_id,
                        quote=item.citation_text,
                    )
                    for item in evidence
                ),
            ),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is None
    assert outcome.claim_rejection_codes == (("CLAIM_SUPPORT_NOT_OWNED", 1),)
    assert outcome.atom_coverage == (("A1", "MISSING"), ("A2", "MISSING"))
    assert outcome.repair_calls == 0


def test_procedure_renderer_uses_source_order() -> None:
    evidence = _evidence("先登记申请。", "再审核材料。")
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("申请流程", shape=AtomAnswerShape.PROCEDURE)
    matrix = _matrix(
        plan,
        ((AtomStatus.SUPPORTED, tuple(item.support_id for item in evidence)),),
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim("C1", "再审核材料。", "A1", by_text["再审核材料。"]),
            _claim("C2", "先登记申请。", "A1", by_text["先登记申请。"]),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is not None
    assert outcome.answer.splitlines()[0].startswith("1. 先登记申请。")
    assert outcome.answer.splitlines()[1].startswith("2. 再审核材料。")
