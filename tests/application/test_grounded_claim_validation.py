"""引用有效不代表事实成立：对象、数字、否定和来源结构的反例。"""

from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    validate_grounded_draft,
)
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    AnswerClaim,
    AnswerDraft,
    ClaimSupport,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    ProviderCall,
    RetrievalPolicy,
)
from tests.application.retrieval.test_descriptive_answers import (
    _candidates,
    _paragraph,
)


@pytest.mark.parametrize(
    "claim,code",
    [
        ("乙部门负责人负责归档，保存 14 天。", "CLAIM_OBJECT_CHANGED"),
        ("甲部门负责人负责归档，保存 15 天。", "CLAIM_NUMBER_UNSUPPORTED"),
        ("甲部门负责人负责归档，保存 14 元。", "CLAIM_NUMBER_UNSUPPORTED"),
        (
            "甲部门负责人负责归档，保存 14 天。可以自行销毁记录。",
            "CLAIM_NEGATION_CHANGED",
        ),
        ("甲部门负责人负责采购天文望远镜。", "CLAIM_TEXT_UNSUPPORTED"),
    ],
)
def test_claim_ids_and_real_quote_do_not_authorize_changed_facts(
    claim: str, code: str
) -> None:
    text = "甲部门负责人负责归档，保存 14 天。不得自行销毁记录。"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(text)), RetrievalPolicy()
    )
    assert evidence
    draft = AnswerDraft(
        text=claim,
        cited_evidence_ids=(evidence[0].support_id,),
        claims=(
            AnswerClaim(
                text=claim,
                supports=(
                    ClaimSupport(support_id=evidence[0].support_id, quote=text),
                ),
            ),
        ),
        generation_mode="llm",
    )
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == code


def test_grounded_paraphrase_can_combine_same_source_role_and_action() -> None:
    text = "甲部门负责人负责核对交付标准，协调维护资源并组织最终验收。"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(text)), RetrievalPolicy()
    )
    claim = "甲部门负责人负责核对标准、协调资源和组织验收。"
    draft = AnswerDraft(
        text=claim,
        cited_evidence_ids=(evidence[0].support_id,),
        claims=(
            AnswerClaim(
                text=claim,
                supports=(
                    ClaimSupport(support_id=evidence[0].support_id, quote=text),
                ),
            ),
        ),
        generation_mode="llm",
    )
    validate_grounded_draft(draft, evidence)


@pytest.mark.parametrize(
    "text,claim,code",
    [
        (
            "甲部门负责人负责归档，保存 14 天。",
            "甲部门负责人负责归档，保存 4 天。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
        (
            "甲部门负责人不得自行销毁记录。",
            "甲部门负责人无需自行销毁记录。",
            "CLAIM_NEGATION_CHANGED",
        ),
        (
            "甲部门负责设备维护，协调资源并组织验收。",
            "乙部门负责设备维护，协调资源并组织验收。",
            "CLAIM_OBJECT_CHANGED",
        ),
        (
            "甲部门负责人未批准设备维护申请。",
            "甲部门负责人批准设备维护申请。",
            "CLAIM_NEGATION_CHANGED",
        ),
        (
            "设备延时 14 毫秒。",
            "设备延时 14 分钟。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
        (
            "甲部门保存 14 天，乙部门保存 7 天。",
            "甲部门保存 7 天。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
        (
            "甲部门保存 14 天，检修间隔 7 天。",
            "甲部门保存 7 天。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
    ],
)
def test_grounded_hard_constraints_close_observed_counterexamples(
    text: str,
    claim: str,
    code: str,
) -> None:
    evidence, draft = _supported_draft(text, claim)
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == code


@pytest.mark.parametrize(
    "text,claim",
    [
        (
            "甲部门负责人未批准设备维护申请。",
            "甲部门负责人未批准设备维护申请。",
        ),
        ("甲部门负责人负责归档，不得自行销毁记录。", "甲部门负责人负责归档。"),
        ("甲部门负责人不得自行销毁记录。", "甲部门负责人禁止自行销毁记录。"),
        (
            "甲部门负责设备维护，协调资源并组织验收。",
            "甲部门负责设备维护、协调资源和组织验收。",
        ),
    ],
)
def test_grounded_negation_stays_bound_to_its_action_and_keeps_paraphrases(
    text: str,
    claim: str,
) -> None:
    evidence, draft = _supported_draft(text, claim)
    validate_grounded_draft(draft, evidence)


def _supported_draft(
    text: str, claim: str
) -> tuple[tuple[EvidenceItem, ...], AnswerDraft]:
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(text)), RetrievalPolicy()
    )
    return evidence, AnswerDraft(
        text=claim,
        cited_evidence_ids=(evidence[0].support_id,),
        claims=(
            AnswerClaim(
                text=claim,
                supports=(
                    ClaimSupport(support_id=evidence[0].support_id, quote=text),
                ),
            ),
        ),
        generation_mode="llm",
    )


def test_invalid_claim_gets_only_one_repair_and_preserves_both_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, invalid = _supported_draft(
        "甲部门保存 14 天。", "甲部门保存 4 天。"
    )
    call = ProviderCall(
        provider_id="synthetic",
        operation="generation",
        call_count=1,
        retry_count=0,
        elapsed_ms=1,
    )
    invalid = invalid.model_copy(update={"provider_calls": (call,)})
    generator = Mock()
    generator.generate.return_value = invalid
    service = GroundedAnsweringService(generator, Mock())
    fallback = Mock(return_value=None)
    monkeypatch.setattr(service.fallback, "answer", fallback)
    outcome = service.answer(
        "保存多久",
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
    )
    assert outcome.answer is None
    assert generator.generate.call_count == 2
    assert generator.generate.call_args_list[0].args[0].repair_reason is None
    assert (
        generator.generate.call_args_list[1].args[0].repair_reason
        == "CLAIM_NUMBER_UNSUPPORTED"
    )
    assert len(outcome.calls) == 2
    fallback.assert_called_once()
