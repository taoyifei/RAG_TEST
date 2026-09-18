"""最小自然 Claim 协议、逐条拒绝和有限回答保留。"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from rag_app.adapters.providers.aliyun_chat import (
    ChatCompletion,
    ChatMessage,
    ChatUsage,
    _natural_answer_draft,
    _NaturalDraftPayload,
)
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatConfig,
    openai_compatible_chat_payload,
)
from rag_app.core.models import ProviderCall
from rag_app.core.models.query_plan import (
    GROUNDED_CLAIM_SCHEMA_REVISION,
    AtomStatus,
)
from rag_app.core.models.retrieval import NaturalClaim
from rag_app.core.ports import GenerationRequest
from tests.application.answering.test_natural_grounded_answer import (
    _answer,
    _claim,
    _draft,
    _evidence,
    _matrix,
    _plan,
)


def _completion(payload: dict[str, object]) -> ChatCompletion:
    return ChatCompletion(
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


def test_model_schema_contains_only_single_atom_claims() -> None:
    assert set(_NaturalDraftPayload.model_fields) == {"claims"}
    assert set(NaturalClaim.model_fields) == {
        "atom_id",
        "text",
        "support_ids",
    }


def test_configured_generation_uses_one_json_schema_protocol() -> None:
    messages = (ChatMessage(role="user", content="甲设备的虚构问题"),)
    payload = openai_compatible_chat_payload(
        messages,
        OpenAICompatibleChatConfig(
            model="synthetic-model",
            structured_output_mode="response_format",
        ),
        json_schema=_NaturalDraftPayload.model_json_schema(),
        schema_revision=GROUNDED_CLAIM_SCHEMA_REVISION,
    )

    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == (
        GROUNDED_CLAIM_SCHEMA_REVISION
    )
    assert "structured_outputs" not in payload
    assert "guided_json" not in payload


def test_model_cannot_report_coverage_or_repeat_quote() -> None:
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
    valid = {
        "claims": [
            {
                "atom_id": "A1",
                "text": "甲部门保存记录 14 天。",
                "support_ids": ["S1"],
            }
        ]
    }
    draft = _natural_answer_draft(_completion(valid), request)

    assert draft.natural_claims[0].atom_id == "A1"
    assert draft.atom_coverage == ()
    assert draft.missing_atoms == ()
    for extra in (
        {"atom_coverage": [{"atom_id": "A1", "status": "SUPPORTED"}]},
        {"missing_atoms": []},
    ):
        with pytest.raises(ValueError):
            _natural_answer_draft(_completion({**valid, **extra}), request)


def test_bad_claim_does_not_delete_valid_claim_after_repair_failure() -> None:
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
        _draft(
            (
                _claim("C1", "甲部门保存记录 14 天。", "A1", first_id),
                _claim("C2", "乙部门审核记录 4 天。", "A2", second_id),
            ),
            plan,
        ),
        ValueError("repair contract invalid"),
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is not None
    assert "甲部门保存记录 14 天" in outcome.answer
    assert "乙部门审核记录 4 天" not in outcome.answer
    assert outcome.reason_code == "LIMITED_ANSWER"
    assert outcome.atom_coverage == (("A1", "SUPPORTED"), ("A2", "MISSING"))
    assert outcome.claim_rejection_codes == (("CLAIM_NUMBER_MISMATCH", 1),)
    assert outcome.repair_calls == 1
    repair = generator.generate.call_args_list[1].args[0]
    assert repair.repair_atom_ids == ("A2",)
    assert tuple(item.support_id for item in repair.evidence) == (second_id,)


def test_invalid_json_does_not_trigger_second_generation() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    generator = Mock()
    generator.generate.side_effect = ValueError("invalid json")

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.answer is None
    assert outcome.repair_calls == 0
    assert generator.generate.call_count == 1
