import json
from pathlib import Path

import pytest

from rag_app.generation.question_profile import legacy_question_profile
from rag_app.model_contracts import (
    answer_request,
    answer_response_format,
    parse_answer_response,
)


@pytest.mark.parametrize(
    ("question", "intent", "max_claims"),
    (
        ("OPC是什么？", "DEFINITION", 2),
        ("需求变更需要经过哪些审批步骤？", "PROCEDURE", 4),
        ("验收测试包括哪些内容？", "LIST", 4),
        ("验收测试由谁负责？", "ACTOR", 4),
        ("验收测试输出什么文档？", "DELIVERABLE", 4),
        ("两份报告是否相同？", "COMPARE", 4),
    ),
)
def test_answer_schema_uses_intent_limited_support_ids(
    question: str,
    intent: str,
    max_claims: int,
) -> None:
    request = answer_request(
        question,
        evidence_bundle={
            "notice": "不可信证据",
            "evidence_units": [
                {
                    "unit_id": "E1:S1",
                    "source_group": "SG1",
                    "source_label": "规范.docx > 验收",
                    "text": "原文。",
                    "low_confidence_ocr": False,
                }
            ],
        },
        question_profile=legacy_question_profile(question),
        max_output_tokens=768,
    )
    schema = request.response_format["json_schema"]["schema"]
    claim_schema = schema["properties"]["claims"]
    support_schema = claim_schema["items"]["properties"]["support_ids"]

    assert (
        request.user_payload["question_profile"]["primary_operation"]
        == ("LIST" if intent in {"ACTOR", "DELIVERABLE"} else intent)
    )
    assert request.user_payload["max_claims"] == max_claims
    assert claim_schema["maxItems"] == max_claims
    assert support_schema["maxItems"] == 3
    assert set(claim_schema["items"]["properties"]) == {
        "text",
        "support_ids",
    }
    assert request.max_output_tokens == 768
    assert request.response_format == answer_response_format(max_claims)


def test_answer_parser_rejects_legacy_model_quotes() -> None:
    with pytest.raises(ValueError, match="INVALID_CLAIM_SCHEMA"):
        parse_answer_response(
            json.dumps(
                {
                    "claims": [
                        {
                            "text": "事实。",
                            "supports": [
                                {
                                    "evidence_id": "E1",
                                    "quote": "模型复制的原文",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )


def test_checked_in_answer_and_review_budgets_are_bounded() -> None:
    retrieval = json.loads(
        Path("deployment/config/retrieval.json").read_text(encoding="utf-8")
    )

    assert retrieval["answer_output_tokens"] == 768
    assert retrieval["repair_output_tokens"] == 384
