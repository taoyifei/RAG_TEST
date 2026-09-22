"""V14.1 C5 字段解析单一合同的正例、负例与约束移交门禁。"""

from __future__ import annotations

import json

import pytest

from rag_app.adapters.providers.field_resolution_wire import (
    FieldResolutionContract,
    FieldResolutionContractContext,
    FieldResolutionWireError,
    build_field_resolution_contract,
    render_wire_schema,
    validate_field_response,
)
from rag_app.adapters.providers.structured_contract import (
    wb08r_structured_output_profile,
)
from rag_app.application.retrieval.source_scope import (
    resolve_query_source_context,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import FieldCandidate, FieldResolutionStatus
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from rag_app.core.ports.evidence_source import CatalogDocument


def _candidate(candidate_id: str, atom_id: str, column: int) -> FieldCandidate:
    return FieldCandidate(
        candidate_id=candidate_id,
        atom_id=atom_id,
        fact_id=canonical_sha256((candidate_id, "fact")),
        document_id=deterministic_id("doc", "field-wire"),
        document_version_id=deterministic_id("dver", "field-wire"),
        table_key=canonical_sha256("field-wire-table"),
        row_index=1,
        value_column_index=column,
        target_label="需求快验",
        field_label=f"字段{column}",
        value_preview=f"字段{column}的安全预览",
        dependency_support_ids=(f"S{column}",),
    )


def _profile(*, allow_unique_items: bool = False) -> object:
    return wb08r_structured_output_profile(
        profile_revision="field-wire-unit-v1",
        service_identity_sha256=canonical_sha256("service"),
        model="unit-model",
        chat_template_revision="unit-template",
        mode="response_format",
        grammar_backend="xgrammar-unit",
        deadline_ms=8000,
        field_resolution_output_tokens=512,
        qualification_evidence_sha256=canonical_sha256("evidence"),
        allow_unique_items=allow_unique_items,
    )


def _contract(
    query: str = "需求快验开始前必须准备哪些材料？",
    *,
    two_atoms: bool = False,
) -> FieldResolutionContract:
    query_view = resolve_query_source_context(
        query,
        (),
        registry_revision="field-wire-unit-v1",
    ).query_view
    atoms = (
        QueryAtom(
            atom_id="A1",
            target="需求快验",
            relation="准备材料",
            answer_shape=AtomAnswerShape.FACT,
            original_fragment=("甲的输入" if two_atoms else query),
        ),
        *(
            (
                QueryAtom(
                    atom_id="A2",
                    target="需求快验",
                    relation="交付结果",
                    answer_shape=AtomAnswerShape.FACT,
                    original_fragment="乙的输出",
                ),
            )
            if two_atoms
            else ()
        ),
    )
    candidates = (
        _candidate("F1", "A1", 1),
        _candidate("F2", "A1", 2),
        _candidate("F3", "A1", 3),
        *(
            (_candidate("F4", "A2", 4), _candidate("F5", "A2", 5))
            if two_atoms
            else ()
        ),
    )
    return build_field_resolution_contract(
        FieldResolutionContractContext(
            query_view=query_view,
            atoms=atoms,
            candidates=candidates,
        )
    )


def _content(
    *,
    status: str = "SUPPORTED_PARAPHRASE",
    candidates: list[str] | None = None,
    query_fragment: str = "准备哪些材料",
    atom_id: str = "A1",
) -> str:
    return json.dumps(
        {
            "r": [
                {
                    "a": atom_id,
                    "s": status,
                    "c": ["F1"] if candidates is None else candidates,
                    "q": query_fragment,
                }
            ]
        },
        ensure_ascii=False,
    )


def test_contract_drives_schema_and_valid_response() -> None:
    contract = _contract()
    schema = render_wire_schema(contract, _profile())  # type: ignore[arg-type]
    candidate_schema = schema["properties"]["r"]["items"]["properties"]["c"]  # type: ignore[index]
    query_schema = schema["properties"]["r"]["items"]["properties"]["q"]  # type: ignore[index]

    assert "uniqueItems" not in candidate_schema
    assert query_schema["maxLength"] == 160
    result = validate_field_response(_content(), contract)
    assert result[0].status is FieldResolutionStatus.SUPPORTED_PARAPHRASE
    assert result[0].candidate_ids == ("F1",)
    assert result[0].query_fragment == "准备哪些材料"
    assert result[0].span_basis == "BUSINESS_QUERY"
    assert result[0].query_view_digest == contract.query_view_digest
    start = contract.business_query.index("准备哪些材料")
    assert (result[0].query_span_start, result[0].query_span_end) == (
        start,
        start + len("准备哪些材料"),
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (
            '{"r":[{"a":"A1","a":"A1","s":"NOT_FOUND","c":[],"q":"材料"}]}',
            "FIELD_RESPONSE_INVALID_JSON",
        ),
        (
            '{"r":[],"r":[]}',
            "FIELD_RESPONSE_INVALID_JSON",
        ),
        (
            _content(candidates=["F1", "F1"]),
            "FIELD_RESPONSE_DUPLICATE_CANDIDATE",
        ),
        (_content(candidates=["F9"]), "FIELD_RESPONSE_CANDIDATE_OUT_OF_SCOPE"),
        (_content(atom_id="A2"), "FIELD_RESPONSE_ATOM_SET_MISMATCH"),
        (
            _content(query_fragment="不存在的片段"),
            "FIELD_RESPONSE_QUERY_SPAN_NOT_UNIQUE",
        ),
        ('{"r":[', "FIELD_RESPONSE_INVALID_JSON"),
        (
            '{"r":[{"a":"A1","s":"NOT_FOUND","c":[],"q":"材料","extra":true}]}',
            "FIELD_RESPONSE_SCHEMA_INVALID",
        ),
    ),
)
def test_invalid_wire_shapes_fail_closed(payload: str, reason: str) -> None:
    with pytest.raises(FieldResolutionWireError) as captured:
        validate_field_response(payload, _contract())
    assert captured.value.reason_code == reason


def test_cross_atom_candidate_and_duplicate_atom_are_rejected() -> None:
    contract = _contract("甲的输入是什么，乙的输出是什么？", two_atoms=True)
    cross_atom = json.dumps(
        {
            "r": [
                {"a": "A1", "s": "RELATED_FIELD", "c": ["F4"], "q": "甲的输入"},
                {"a": "A2", "s": "RELATED_FIELD", "c": ["F4"], "q": "乙的输出"},
            ]
        },
        ensure_ascii=False,
    )
    duplicate_atom = json.dumps(
        {
            "r": [
                {"a": "A1", "s": "RELATED_FIELD", "c": ["F1"], "q": "甲的输入"},
                {"a": "A1", "s": "RELATED_FIELD", "c": ["F2"], "q": "甲的输入"},
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(FieldResolutionWireError) as cross:
        validate_field_response(cross_atom, contract)
    with pytest.raises(FieldResolutionWireError) as duplicate:
        validate_field_response(duplicate_atom, contract)

    assert cross.value.reason_code == "FIELD_RESPONSE_CANDIDATE_OUT_OF_SCOPE"
    assert duplicate.value.reason_code == "FIELD_RESPONSE_DUPLICATE_ATOM"


def test_repeated_query_fragment_does_not_use_first_find() -> None:
    contract = _contract("材料和材料")
    with pytest.raises(FieldResolutionWireError) as captured:
        validate_field_response(
            _content(status="NOT_FOUND", candidates=[], query_fragment="材料"),
            contract,
        )
    assert captured.value.reason_code == "FIELD_RESPONSE_QUERY_SPAN_NOT_UNIQUE"


def test_business_query_span_maps_back_to_verified_original_slice() -> None:
    query = "《开发中心三种工作模式》中，需求快验准备哪些材料？"
    query_view = resolve_query_source_context(
        query,
        (
            CatalogDocument(
                document_id=deterministic_id("doc", "source-span"),
                document_version_id=deterministic_id("dver", "source-span"),
                chunk_id="chunk_00000000000000000000000000000001",
                title="开发中心三种工作模式.docx",
                metadata=(),
            ),
        ),
        registry_revision="field-wire-source-v1",
    ).query_view
    atom = QueryAtom(
        atom_id="A1",
        target="需求快验",
        relation="准备材料",
        answer_shape=AtomAnswerShape.FACT,
        original_fragment=query_view.business_query,
    )
    contract = build_field_resolution_contract(
        FieldResolutionContractContext(
            query_view=query_view,
            atoms=(atom,),
            candidates=(
                _candidate("F1", "A1", 1),
                _candidate("F2", "A1", 2),
            ),
        )
    )

    result = validate_field_response(_content(), contract)[0]

    assert result.query_span_start == query_view.business_query.index(
        "准备哪些材料"
    )
    assert result.original_query_span_start == query.index("准备哪些材料")
    assert (
        query[result.original_query_span_start : result.original_query_span_end]
        == "准备哪些材料"
    )


def test_local_status_property_never_accepts_more_than_business_contract() -> (
    None
):
    """穷举状态与候选数量，确认移交后本地接收集不扩大。"""
    contract = _contract()
    for status in (
        "SUPPORTED_PARAPHRASE",
        "RELATED_FIELD",
        "AMBIGUOUS",
        "NOT_FOUND",
    ):
        for count in range(4):
            candidates = [f"F{index}" for index in range(1, count + 1)]
            should_accept = (
                (
                    status in {"SUPPORTED_PARAPHRASE", "RELATED_FIELD"}
                    and count == 1
                )
                or (status == "AMBIGUOUS" and count >= 2)
                or (status == "NOT_FOUND" and count == 0)
            )
            try:
                validate_field_response(
                    _content(status=status, candidates=candidates), contract
                )
            except FieldResolutionWireError:
                accepted = False
            else:
                accepted = True
            assert accepted is should_accept, (status, count)


def test_unique_items_is_only_transferred_when_profile_declares_it() -> None:
    contract = _contract()
    local_schema = render_wire_schema(contract, _profile())  # type: ignore[arg-type]
    backend_schema = render_wire_schema(
        contract,
        _profile(allow_unique_items=True),  # type: ignore[arg-type]
    )
    local_candidates = local_schema["properties"]["r"]["items"]["properties"][
        "c"
    ]  # type: ignore[index]
    backend_candidates = backend_schema["properties"]["r"]["items"][
        "properties"
    ]["c"]  # type: ignore[index]

    assert "uniqueItems" not in local_candidates
    assert backend_candidates["uniqueItems"] is True
    with pytest.raises(FieldResolutionWireError):
        validate_field_response(_content(candidates=["F1", "F1"]), contract)
