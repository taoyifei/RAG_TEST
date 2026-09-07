"""英文 OCR 原文与中文温度回答的有界关系、数值和单位契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.application.answering.grounded import validate_grounded_draft
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import RetrievalPolicy
from tests.adapters.parsers.docx.fixtures import build_package
from tests.api.test_ocr_product import _wait_job
from tests.application.retrieval.test_descriptive_answers import (
    _candidates,
    _paragraph,
)
from tests.application.retrieval.test_evidence_table_coordinates import _context
from tests.application.test_grounded_claim_validation import _supported_draft
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)


@pytest.mark.parametrize("unit", ["Celsius", "degrees Celsius", "°C", "℃"])
def test_chinese_temperature_question_keeps_english_ocr_evidence(
    unit: str,
) -> None:
    original = f"SENSOR-482 temperature: 36 {unit}"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(original)),
        RetrievalPolicy(),
        context=_context("SENSOR-482的温度是多少"),
    )
    assert len(evidence) == 1
    assert evidence[0].citation_text == original
    assert dict(evidence[0].metadata)["answer_support"]["status"] == "SUPPORTED"


@pytest.mark.parametrize("unit", ["℃", "摄氏度", "°C", "Celsius"])
@pytest.mark.parametrize("subject", ["SENSOR-482的", ""])
def test_same_temperature_unit_name_can_be_localized_without_conversion(
    unit: str,
    subject: str,
) -> None:
    evidence, draft = _supported_draft(
        "SENSOR-482 temperature: 36 Celsius", f"{subject}温度为36{unit}。"
    )
    validate_grounded_draft(draft, evidence)


def test_identifier_number_boundary_keeps_literal_identifier_answers() -> None:
    evidence, draft = _supported_draft(
        "SENSOR-482 temperature: 36 Celsius", "SENSOR-482"
    )
    validate_grounded_draft(draft, evidence)


@pytest.mark.parametrize(
    "claim",
    [
        "SENSOR-482温度为37℃。",
        "SENSOR-483温度为36℃。",
        "SENSOR-482温度为36华氏度。",
        "SENSOR-482温度为36°F。",
        "SENSOR-482温度为309.15 K。",
    ],
)
def test_temperature_aliases_never_authorize_changed_numbers_scales_or_ids(
    claim: str,
) -> None:
    evidence, draft = _supported_draft(
        "SENSOR-482 temperature: 36 Celsius", claim
    )
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == "CLAIM_NUMBER_UNSUPPORTED"


@pytest.mark.parametrize(
    "original",
    [
        "SENSOR-483 temperature: 36 Celsius",
        "SENSOR-4820 temperature: 36 Celsius",
        "SENSOR-482 pressure: 36 Celsius",
        "SENSOR-482 temperature: unknown",
        "SENSOR-482 temperature: 36 dollars",
        "SENSOR-482 temperature: 36 Fahrenheit",
        "SENSOR-482 temperature: 36 °F",
        "SENSOR-482 temperature: not 36 Celsius",
        "SENSOR-482 temperature: unknown, 36 Celsius is a reference",
    ],
)
def test_temperature_lookup_requires_the_actual_object_relation_and_unit(
    original: str,
) -> None:
    assert not EvidenceAssembler().assemble(
        _candidates(_paragraph(original)),
        RetrievalPolicy(),
        context=_context("SENSOR-482的温度是多少"),
        allow_uncertain=True,
    )


@pytest.mark.parametrize(
    "original",
    [
        "SENSOR-482 pressure: 36 Celsius",
        "SENSOR-482 temperature: 22 Celsius, pressure: 36 Celsius",
        "SENSOR-482 temperature: not 36 Celsius",
    ],
)
def test_localized_temperature_cannot_borrow_another_attribute_quantity(
    original: str,
) -> None:
    evidence, draft = _supported_draft(original, "温度为36摄氏度。")
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == "CLAIM_NUMBER_UNSUPPORTED"


@pytest.mark.parametrize(
    "question", ["SENSOR-482的手机号是多少", "SENSOR-482价格是多少"]
)
def test_temperature_evidence_does_not_answer_missing_unrelated_attributes(
    question: str,
) -> None:
    assert not EvidenceAssembler().assemble(
        _candidates(_paragraph("SENSOR-482 temperature: 36 Celsius")),
        RetrievalPolicy(),
        context=_context(question),
        allow_uncertain=True,
    )


def test_temperature_language_still_requires_citable_spans() -> None:
    candidates = _candidates(_paragraph("SENSOR-482 temperature: 36 Celsius"))
    candidate = candidates[0]
    chunk = candidate.hydrated.chunk
    uncitable = chunk.model_copy(
        update={
            "source_spans": tuple(
                span.model_copy(update={"is_citable": False})
                for span in chunk.source_spans
            )
        }
    )
    without_source = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={"chunk": uncitable}
            )
        }
    )
    assert not EvidenceAssembler().assemble(
        (without_source,),
        RetrievalPolicy(),
        context=_context("SENSOR-482的温度是多少"),
    )


def test_product_retrieves_english_recognized_text_for_chinese_temperature(
    tmp_path: Path,
) -> None:
    """覆盖识别文字进入文档后的真实索引与检索，不模拟 OCR 质量。"""
    original = "SENSOR-482 temperature: 36 Celsius"
    harness = build_product_harness(tmp_path)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        job = harness.runtime.sdk.create_document(
            project,
            kb,
            display_name="合成识别结果.docx",
            content=build_package(_paragraph(original)),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            idempotency_key="english-temperature",
        )
        _wait_job(harness, job.job_id)
        response = harness.client.post(
            f"/api/v1/projects/{project}/knowledge-bases/{kb}:answer",
            json={"query": "SENSOR-482的温度是多少"},
            headers=harness.write_headers,
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "ANSWERABLE", result
        assert result["generation_mode"] == "extractive"
        assert original in result["answer"]
        assert result["evidence"][0]["citation_text"] == original
        assert result["diagnostics_summary"]["provider_call_count"] == 0
    finally:
        harness.close()
