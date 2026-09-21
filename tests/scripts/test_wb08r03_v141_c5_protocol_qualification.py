"""C5 真实协议资格脚本的离线形状与判定门禁。"""

from __future__ import annotations

import json
from pathlib import Path

from rag_app.adapters.providers.structured_contract import (
    StructuredOutputCapabilityProfile,
    wb08r_structured_output_profile,
)
from rag_app.application.retrieval.adaptive import (
    FieldResolutionExecutionState,
    FieldResolutionOutcome,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import FieldResolution, ProviderCall
from scripts import wb08r03_v141_c5_protocol_qualification as qualification


def _profile() -> StructuredOutputCapabilityProfile:
    return wb08r_structured_output_profile(
        profile_revision="qualification-script-unit-v1",
        service_identity_sha256=canonical_sha256("service"),
        model="Qwen/Unit",
        chat_template_revision="template-v1",
        mode="response_format",
        grammar_backend="xgrammar-unit",
        deadline_ms=8000,
        field_resolution_output_tokens=512,
        qualification_evidence_sha256=canonical_sha256("evidence"),
    )


def test_qualification_cases_cover_all_required_shapes() -> None:
    cases = qualification._qualification_cases()

    assert tuple(item.case_id for item in cases) == (
        "PARAPHRASE",
        "AMBIGUOUS",
        "NOT_FOUND",
        "TWO_ATOMS",
        "MAX_SHAPE",
    )
    assert len(cases[-1].atoms) == 4
    assert {len(atom.candidates) for atom in cases[-1].atoms} == {16}
    prepared = qualification._prepare_case(cases[-1])
    assert len(prepared.candidates) == 64
    assert len(prepared.contract.atoms) == 4


def test_qualification_truth_checks_status_identity_and_query_anchor() -> None:
    prepared = qualification._prepare_case(
        qualification._qualification_cases()[0]
    )
    expected = prepared.case.atoms[0]
    selected_id = next(
        candidate_id
        for candidate_id, identity in (
            prepared.identities_by_candidate_id.items()
        )
        if identity == expected.expected_candidate_identities[0]
    )
    fragment = "提交资料"
    start = prepared.query_view.business_query.index(fragment)
    resolution = FieldResolution(
        atom_id="A1",
        status=expected.expected_status,
        candidate_ids=(selected_id,),
        query_span_start=start,
        query_span_end=start + len(fragment),
        query_fragment=fragment,
        query_view_digest=prepared.contract.query_view_digest,
        reason_code="SCHEMA_AWARE_INTERPRETATION_V2",
    )
    outcome = FieldResolutionOutcome(
        resolutions=(resolution,),
        calls=(
            ProviderCall(
                provider_id="openai-compatible",
                operation="query.interpret",
                call_count=1,
                retry_count=0,
                elapsed_ms=10,
                attempt_count=1,
                status_category="SUCCESS",
            ),
        ),
        execution_state=FieldResolutionExecutionState.SUCCEEDED,
        reason_code="FIELD_RESOLUTION_ACCEPTED",
        attempted=True,
        output_tokens=32,
        finish_reason="stop",
    )

    passed, failures = qualification._case_passes(prepared, outcome, _profile())

    assert passed
    assert failures == ()


def test_reconstructed_v1_ab_changes_only_unique_items() -> None:
    """历史请求重建必须逐字段保持 v1，只移动唯一性约束。"""
    prepared = qualification._prepare_case(
        qualification._qualification_cases()[0]
    )
    original = qualification._original_wire_schema(
        prepared, allow_unique_items=True
    )
    transferred = qualification._original_wire_schema(
        prepared, allow_unique_items=False
    )
    expected_after_transfer = json.loads(json.dumps(original))
    candidate_schema = expected_after_transfer["properties"]["r"]["items"][
        "properties"
    ]["c"]
    del candidate_schema["uniqueItems"]

    assert transferred == expected_after_transfer
    query_schema = original["properties"]["r"]["items"]["properties"]["q"]
    assert query_schema == {"type": "string", "minLength": 1}
    user_payload = json.loads(
        qualification._original_messages(prepared)[1].content
    )
    assert set(user_payload) == {"question", "candidates"}
    assert "query_view_digest" not in user_payload


def test_real_probe_phases_cannot_be_implicitly_combined(
    tmp_path: Path,
) -> None:
    output = tmp_path / "unused"

    try:
        qualification.run(
            phase="qualified",
            output_dir=output,
            acknowledgement="wrong",
        )
    except ValueError as error:
        assert str(error) == "REAL_PROBE_ACK_REQUIRED"
    else:
        raise AssertionError("缺少显式确认时不应进入真实探针。")

    try:
        qualification.run(
            phase="qualified",
            output_dir=output,
            acknowledgement="wb08r03-v141-c5-real-probe-v1",
            fixture=tmp_path / "private.json",
        )
    except ValueError as error:
        assert str(error) == "RECONSTRUCTED_FIXTURE_PHASE_MISMATCH"
    else:
        raise AssertionError("资格阶段不得夹带原协议诊断 fixture。")

    try:
        qualification.run(
            phase="reconstructed-transferred",
            output_dir=output,
            acknowledgement="wb08r03-v141-c5-real-probe-v1",
        )
    except ValueError as error:
        assert str(error) == "RECONSTRUCTED_FIXTURE_PHASE_MISMATCH"
    else:
        raise AssertionError("约束移交诊断必须显式提供同一重建 fixture。")

    assert not output.exists()
