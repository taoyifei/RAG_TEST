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
from rag_app.core.models import (
    FieldCandidate,
    FieldResolution,
    ProviderCall,
    ResolvedQueryView,
    SearchRequest,
)
from rag_app.core.models.query_plan import QueryAtom
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
    defaults = qualification.default_field_resolutions(
        prepared.query_plan,
        prepared.candidates,
        prepared.query_view,
    )
    assert all(
        item.status is not qualification.FieldResolutionStatus.EXACT
        for item in defaults
    )
    contracts = tuple(
        qualification.build_field_resolution_contract(
            qualification.FieldResolutionContractContext(
                query_view=prepared.query_view,
                atoms=(atom,),
                candidates=tuple(
                    item
                    for item in prepared.candidates
                    if item.atom_id == atom.atom_id
                ),
            )
        )
        for atom in prepared.atoms
    )
    assert {len(contract.atoms) for contract in contracts} == {1}
    assert {len(contract.candidates) for contract in contracts} == {16}
    assert all(
        qualification.render_wire_schema(contract, _profile())
        for contract in contracts
    )

    deterministic = qualification._prepare_case(cases[-2])
    deterministic_defaults = qualification.default_field_resolutions(
        deterministic.query_plan,
        deterministic.candidates,
        deterministic.query_view,
    )
    assert tuple(item.status for item in deterministic_defaults) == (
        qualification.FieldResolutionStatus.EXACT,
        qualification.FieldResolutionStatus.EXACT,
    )


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


def test_local_exact_case_passes_without_provider_call() -> None:
    prepared = qualification._prepare_case(
        qualification._qualification_cases()[-2]
    )
    resolutions = qualification.default_field_resolutions(
        prepared.query_plan,
        prepared.candidates,
        prepared.query_view,
    )
    outcome = FieldResolutionOutcome(
        resolutions=resolutions,
        execution_state=FieldResolutionExecutionState.SUCCEEDED,
        reason_code="FIELD_RESOLUTION_LOCAL_EXACT",
    )

    passed, failures = qualification._case_passes(
        prepared,
        outcome,
        _profile(),
    )

    assert passed
    assert failures == ()


class _PerAtomModel:
    """记录逐 Atom 完整候选，并可注入一个现场失败。"""

    field_resolution_total_deadline_seconds = 15.0

    def __init__(self, *, failed_atom_id: str | None = None) -> None:
        self.failed_atom_id = failed_atom_id
        self.calls: list[tuple[str, int, float | None]] = []

    def resolve_fields(
        self,
        request: SearchRequest,
        candidates: tuple[FieldCandidate, ...],
        *,
        query_view: ResolvedQueryView,
        atoms: tuple[QueryAtom, ...],
        timeout_seconds: float | None = None,
    ) -> FieldResolutionOutcome:
        del request
        atom = atoms[0]
        atom_id = atom.atom_id
        self.calls.append((atom_id, len(candidates), timeout_seconds))
        provider_call = ProviderCall(
            provider_id="unit",
            operation="query.interpret",
            call_count=1,
            retry_count=0,
            elapsed_ms=10,
            attempt_count=1,
            status_category="SUCCESS",
        )
        if atom_id == self.failed_atom_id:
            return FieldResolutionOutcome(
                calls=(provider_call,),
                execution_state=(
                    FieldResolutionExecutionState.TRANSPORT_FAILED
                ),
                reason_code="FIELD_RESOLUTION_PROVIDER_UNAVAILABLE",
                attempted=True,
                failure_category="READ_TIMEOUT",
                transport_timeout_ms=12000,
            )
        expected = next(
            item
            for item in candidates
            if item.candidate_id.endswith(str((int(atom_id[1:]) - 1) * 16 + 1))
        )
        fragment = atom.original_fragment
        start = query_view.business_query.index(fragment)
        resolution = FieldResolution(
            atom_id=atom_id,
            status=qualification.FieldResolutionStatus.SUPPORTED_PARAPHRASE,
            candidate_ids=(expected.candidate_id,),
            query_span_start=start,
            query_span_end=start + len(fragment),
            query_fragment=fragment,
            query_view_digest=canonical_sha256(
                query_view.model_dump(mode="json")
            ),
            reason_code="SCHEMA_AWARE_INTERPRETATION_V2",
        )
        return FieldResolutionOutcome(
            resolutions=(resolution,),
            calls=(provider_call,),
            execution_state=FieldResolutionExecutionState.SUCCEEDED,
            reason_code="FIELD_RESOLUTION_ACCEPTED",
            attempted=True,
            input_tokens=100,
            output_tokens=32,
            finish_reason="stop",
            transport_timeout_ms=12000,
        )


def test_max_shape_is_split_without_dropping_candidates() -> None:
    prepared = qualification._prepare_case(
        qualification._qualification_cases()[-1]
    )
    model = _PerAtomModel()

    outcome, probes = qualification._qualified_case_outcome(
        model,  # type: ignore[arg-type]
        prepared,
        _profile(),
    )
    passed, failures = qualification._case_passes(
        prepared,
        outcome,
        _profile(),
    )

    assert passed
    assert failures == ()
    assert tuple((atom_id, count) for atom_id, count, _ in model.calls) == (
        ("A1", 16),
        ("A2", 16),
        ("A3", 16),
        ("A4", 16),
    )
    assert len(probes) == 4
    assert all(
        timeout is not None and timeout > 0 for *_, timeout in model.calls
    )


def test_per_atom_failure_keeps_other_successes() -> None:
    prepared = qualification._prepare_case(
        qualification._qualification_cases()[-1]
    )
    model = _PerAtomModel(failed_atom_id="A2")

    outcome, probes = qualification._qualified_case_outcome(
        model,  # type: ignore[arg-type]
        prepared,
        _profile(),
    )
    by_atom = {item.atom_id: item for item in outcome.resolutions}

    assert outcome.execution_state is (
        FieldResolutionExecutionState.TRANSPORT_FAILED
    )
    assert len(probes) == 4
    assert by_atom["A2"].status is (
        qualification.FieldResolutionStatus.AMBIGUOUS
    )
    assert all(
        by_atom[atom_id].status
        is qualification.FieldResolutionStatus.SUPPORTED_PARAPHRASE
        for atom_id in ("A1", "A3", "A4")
    )


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
