"""回答模型 claims-only 协议的结构与解析边界。"""

from __future__ import annotations

import json
from typing import cast

import pytest

from rag_app.model_contracts import (
    answer_request,
    answer_response_format,
    parse_answer_response,
    repair_answer_request,
)


def _object(value: object) -> dict[str, object]:
    """断言 JSON 值为 object 并收窄类型。"""
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    """断言 JSON 值为 array 并收窄类型。"""
    assert isinstance(value, list)
    return cast(list[object], value)


def _claim(index: int, *, support_count: int = 1) -> dict[str, object]:
    return {
        "text": f"要求{index}",
        "support_ids": [
            f"E{index}:S{support_index + 1}"
            for support_index in range(support_count)
        ],
    }


def test_answer_schema_is_bounded_claims_only_object() -> None:
    response_format = answer_response_format()
    json_schema = _object(response_format["json_schema"])
    schema = _object(json_schema["schema"])

    assert set(_object(schema["properties"])) == {"claims"}
    assert schema["required"] == ["claims"]
    assert schema["additionalProperties"] is False
    assert all(
        keyword not in json.dumps(schema, ensure_ascii=False)
        for keyword in ('"anyOf"', '"oneOf"', '"if"', '"then"')
    )

    claims = _object(_object(schema["properties"])["claims"])
    assert claims["maxItems"] == 4
    claim = _object(claims["items"])
    claim_properties = _object(claim["properties"])
    assert _object(claim_properties["text"])["maxLength"] == 240
    supports = _object(claim_properties["support_ids"])
    assert supports["minItems"] == 1
    assert supports["maxItems"] == 3
    support = _object(supports["items"])
    assert support["type"] == "string"
    assert support["minLength"] == 1


@pytest.mark.parametrize("claim_count", range(5))
def test_parser_accepts_zero_to_five_claims(claim_count: int) -> None:
    content = json.dumps(
        {"claims": [_claim(index) for index in range(claim_count)]},
        ensure_ascii=False,
    )

    parsed = parse_answer_response(content)

    assert len(_array(parsed["claims"])) == claim_count


@pytest.mark.parametrize(
    "payload",
    (
        {"claims": [], "status": "refused"},
        {"claims": [], "refusal_reason": "没有证据"},
        {
            "claims": [],
            "status": "answered",
            "refusal_reason": None,
        },
    ),
)
def test_parser_rejects_removed_top_level_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="INVALID_TOP_LEVEL_SCHEMA"):
        parse_answer_response(json.dumps(payload, ensure_ascii=False))


def test_parser_rejects_claim_and_support_count_overflow() -> None:
    with pytest.raises(ValueError, match="TOO_MANY_CLAIMS"):
        parse_answer_response(
            json.dumps(
                {"claims": [_claim(index) for index in range(5)]},
                ensure_ascii=False,
            )
        )
    with pytest.raises(ValueError, match="TOO_MANY_SUPPORTS"):
        parse_answer_response(
            json.dumps(
                {"claims": [_claim(1, support_count=4)]},
                ensure_ascii=False,
            )
        )


def test_parser_rejects_claim_and_quote_length_overflow() -> None:
    claim = _claim(1)
    claim["text"] = "要" * 241
    with pytest.raises(ValueError, match="CLAIM_TOO_LONG"):
        parse_answer_response(
            json.dumps({"claims": [claim]}, ensure_ascii=False)
        )

    claim = _claim(1)
    support_ids = _array(claim["support_ids"])
    support_ids[0] = ""
    with pytest.raises(ValueError, match="EMPTY_SUPPORT_FIELD"):
        parse_answer_response(
            json.dumps({"claims": [claim]}, ensure_ascii=False)
        )


def test_answer_and_repair_prompts_freeze_claims_only_rules() -> None:
    first = answer_request(
        "概括主要要求",
        evidence_bundle={"evidence_units": []},
        max_output_tokens=768,
    )
    assert "最多输出请求指定数量" in first.messages[0].content
    assert '{"claims":[]}' in first.messages[0].content
    assert "不能因为无法完整覆盖全部步骤而返回空" in (
        first.messages[0].content
    )
    assert "support_ids" in first.messages[0].content
    assert "不得输出 status、refusal_reason" in first.messages[0].content
    assert first.user_payload["question_intent"] == "LIST"
    assert first.user_payload["max_claims"] == 4
    assert first.user_payload["allow_partial_answer"] is True
    assert (
        first.user_payload[
            "empty_only_if_no_evidence_supports_any_material_part"
        ]
        is True
    )
    assert first.user_payload["inspect_all_evidence"] is True

    repaired = repair_answer_request(
        first,
        validation_error="INVALID_SUPPORT_ID",
        invalid_output='{"claims":[]}',
        max_output_tokens=384,
    )
    repair_payload = json.loads(repaired.messages[1].content)
    assert repair_payload["validation_error"] == "INVALID_SUPPORT_ID"
    assert "unit_id" in repair_payload["repair_instruction"]
    assert repaired.response_format == first.response_format


@pytest.mark.parametrize(
    ("question", "expected_intent"),
    (
        ("需求变更需要经过哪些审批步骤？", "PROCEDURE"),
        ("需要准备哪些材料？", "LIST"),
        ("什么是需求基线？", "DEFINITION"),
        ("介绍一下需求基线。", "DEFINITION"),
    ),
)
def test_answer_request_exposes_deterministic_question_intent(
    question: str,
    expected_intent: str,
) -> None:
    request = answer_request(
        question,
        evidence_bundle={"evidence_units": []},
        max_output_tokens=512,
    )

    assert request.user_payload["question_intent"] == expected_intent
