"""Planner v3 只能引用受信 Span，约束由服务端重建。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.context_resolution import build_input_spans
from rag_app.application.retrieval.minimal_plan import (
    MinimalPlanPayload,
    MinimalPlanValidationError,
    _trusted_answer_shape,
    build_query_atoms,
    planner_json_schema,
)
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.core.models.query_plan import AtomAnswerShape, fallback_query_plan


def _request(text: str, *, context: tuple[str, ...] = ()) -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=deterministic_id("prj", "minimal-plan"),
            knowledge_base_id=deterministic_id("kb", "minimal-plan"),
        ),
        text=text,
        conversation_context=context,
    )


def _payload(*atoms: dict[str, object]) -> MinimalPlanPayload:
    return MinimalPlanPayload.model_validate(
        {
            "intent": "COMPOUND" if len(atoms) > 1 else "SINGLE",
            "clarification_reason": None,
            "atoms": atoms,
        }
    )


def _atom(
    clauses: tuple[str, ...],
    target: str,
    relation: str,
    shape: str = "FACT",
) -> dict[str, object]:
    return {
        "fragment_span_ids": clauses,
        "target_span_id": target,
        "relation_span_id": relation,
        "answer_shape": shape,
    }


def _build(payload: MinimalPlanPayload, request: SearchRequest) -> object:
    return build_query_atoms(
        payload, build_input_spans(request), QueryAnalyzer().analyze(request)
    )


def test_original_fragments_and_server_constraints_remain_intact() -> None:
    request = _request("根据《甲制度V2》，乙不得超过5天提交吗？")
    atom = _build(
        _payload(_atom(("Q.C1", "Q.C2"), "Q.T1", "Q.R2", "DURATION")),
        request,
    )[0]
    assert atom.atom_id == "A1"
    assert atom.source_qualifier == "甲制度V2"
    assert {
        (item.kind.value, item.value, item.unit) for item in atom.constraints
    } == {
        ("VERSION", "V2", None),
        ("DURATION", "5", "天"),
        ("NEGATION", "不得", None),
        ("SOURCE", "甲制度V2", None),
    }


@pytest.mark.parametrize("bad_id", ("Q.C99", "Q.T99", "P3.T1"))
def test_unanchored_or_invented_planner_field_rejects_entire_plan(
    bad_id: str,
) -> None:
    request = _request("甲什么时候提交，乙多久审核？")
    data = _atom(("Q.C1", "Q.C2"), "Q.T1", "Q.R1")
    if bad_id.startswith("Q.C"):
        data["fragment_span_ids"] = (bad_id,)
    else:
        data["target_span_id"] = bad_id
    with pytest.raises(MinimalPlanValidationError) as failure:
        _build(_payload(data), request)
    assert failure.value.code == "PLANNER_UNKNOWN_SPAN_REFERENCE"


def test_missing_independent_clause_rejects_entire_plan() -> None:
    request = _request("甲什么时候提交，乙多久审核？")
    with pytest.raises(MinimalPlanValidationError) as failure:
        _build(_payload(_atom(("Q.C1",), "Q.T1", "Q.R1")), request)
    assert failure.value.code == "PLANNER_CLAUSE_UNCOVERED"


def test_shared_fragment_can_describe_two_distinct_relations() -> None:
    request = _request("甲先审核什么再准备什么？")
    atoms = _build(
        _payload(
            _atom(("Q.C1",), "Q.T1", "Q.R2"),
            _atom(("Q.C1",), "Q.T1", "Q.R3"),
        ),
        request,
    )
    assert len(atoms) == 2
    assert atoms[0].original_fragment == atoms[1].original_fragment
    assert atoms[0].relation != atoms[1].relation


def test_recent_context_relation_is_not_substituted_for_current_relation() -> (
    None
):
    request = _request("乙呢？", context=("上一问：甲什么时候提交？",))
    spans = build_input_spans(request)
    assert all(
        span.text != "甲什么时候提交" or span.turn != "CURRENT"
        for span in spans
    )
    with pytest.raises(MinimalPlanValidationError) as failure:
        _build(_payload(_atom(("Q.C1",), "P1.T1", "P1.R1")), request)
    assert failure.value.code == "PLANNER_INVALID_SPAN_KIND"


def test_planner_wire_schema_uses_compact_ids_and_preserves_old_parser() -> (
    None
):
    payload = _payload(_atom(("Q.C1",), "Q.T1", "Q.R1"))
    wire = payload.model_dump(by_alias=True)
    assert set(wire) == {"i", "c", "a"}
    assert set(wire["a"][0]) == {"f", "t", "r", "s"}
    assert MinimalPlanPayload.model_validate(wire) == payload
    assert set(MinimalPlanPayload.model_json_schema()["properties"]) == {
        "i",
        "c",
        "a",
    }


def test_planner_schema_references_only_trusted_ids_of_each_kind() -> None:
    spans = build_input_spans(
        _request("乙什么时候提交？", context=("上一问：甲由谁审核？",))
    )
    schema = planner_json_schema(spans)
    assert set(schema["properties"]) == {"i", "a"}
    assert "c" not in schema["required"]
    atom = schema["$defs"]["MinimalAtomPayload"]["properties"]
    by_kind = {
        "f": ("CLAUSE", "CURRENT"),
        "t": ("TARGET", None),
        "r": ("RELATION", "CURRENT"),
    }
    for field, (kind, turn) in by_kind.items():
        allowed = (
            atom[field]["items"]["enum"]
            if field == "f"
            else atom[field]["enum"]
        )
        assert allowed
        assert allowed == [
            span.span_id
            for span in spans
            if span.kind.value == kind and (turn is None or span.turn == turn)
        ]
    assert "Q.R1" not in atom["f"]["items"]["enum"]
    assert "P1.C1" not in atom["f"]["items"]["enum"]
    assert all(
        span.turn == "CURRENT"
        for span in spans
        if span.span_id in atom["r"]["enum"]
    )


def test_trusted_root_keeps_modifier_without_hiding_question_clauses() -> None:
    request = _request("甲方案从申请到结束后，如何报送，多久完成？")
    atoms = _build(
        _payload(
            _atom(("Q.C2",), "Q.T1", "Q.R2", "PROCEDURE"),
            _atom(("Q.C3",), "Q.T1", "Q.R3", "DURATION"),
        ),
        request,
    )
    assert len(atoms) == 2
    assert all(
        "甲方案从申请到结束后" in atom.original_fragment for atom in atoms
    )


def test_planner_cannot_clarify_a_server_resolved_question() -> None:
    with pytest.raises(ValueError):
        MinimalPlanPayload.model_validate(
            {"i": "CLARIFICATION", "c": "AMBIGUOUS_REFERENCE", "a": []}
        )


def test_source_modifier_is_inherited_without_model_repeating_its_clause() -> (
    None
):
    request = _request("根据《甲制度V2》，乙需要多久提交？")
    atom = _build(
        _payload(_atom(("Q.C2",), "Q.T1", "Q.R2", "DURATION")),
        request,
    )[0]
    assert "甲制度V2" in atom.original_fragment
    assert atom.source_qualifier == "甲制度V2"


def test_planner_cannot_weaken_server_duties_shape_to_fact() -> None:
    request = _request("甲团队在试运行阶段需要承担哪些主要职责？")

    atom = _build(
        _payload(_atom(("Q.C1",), "Q.T1", "Q.R1", "FACT")),
        request,
    )[0]

    assert atom.answer_shape.value == "DUTIES"


def test_declarative_lead_in_is_context_for_colloquial_question() -> None:
    request = _request("我刚考了证，钱能放明年报不？")
    schema = planner_json_schema(build_input_spans(request))
    fragment_ids = schema["$defs"]["MinimalAtomPayload"]["properties"]["f"][
        "items"
    ]["enum"]
    atom = _build(
        _payload(_atom(("Q.C2",), "Q.T2", "Q.R2")),
        request,
    )[0]

    assert fragment_ids == ["Q.C2"]
    assert atom.original_fragment == "我刚考了证 钱能放明年报不"
    assert all(item.kind.value != "NEGATION" for item in atom.constraints)


@pytest.mark.parametrize(
    ("fragment", "expected"),
    (
        ("设计文档由谁确认", AtomAnswerShape.RESPONSIBLE_PARTY),
        ("几天内反馈", AtomAnswerShape.DURATION),
        ("采购应答要留几天", AtomAnswerShape.DURATION),
        ("流程有几个步骤", AtomAnswerShape.COUNT),
    ),
)
def test_each_natural_subquestion_overrides_wrong_planner_count(
    fragment: str, expected: AtomAnswerShape
) -> None:
    analysis = QueryAnalyzer().analyze(_request(fragment))

    assert _trusted_answer_shape(
        fragment,
        AtomAnswerShape.COUNT,
        analysis,
        single_atom=False,
    ) is expected


def test_responsible_party_question_keeps_one_listed_object() -> None:
    question = "哪些部门负责制定考核指标的考核标准、考核分数和考核频次？"
    spans = build_input_spans(_request(question))
    targets = [span.text for span in spans if span.kind.value == "TARGET"]

    assert targets == [question.rstrip("？")]
    assert _trusted_answer_shape(
        question,
        AtomAnswerShape.FACT,
        QueryAnalyzer().analyze(_request(question)),
        single_atom=False,
    ) is AtomAnswerShape.RESPONSIBLE_PARTY


@pytest.mark.parametrize(
    "question",
    (
        "设计文档由谁确认、几天内反馈？",
        "设计文档几天内反馈、由谁确认？",
    ),
)
def test_parallel_interrogatives_split_without_changing_question_order(
    question: str,
) -> None:
    clauses = [
        span.text
        for span in build_input_spans(_request(question))
        if span.kind.value == "CLAUSE"
    ]

    assert len(clauses) == 2
    assert any("谁确认" in clause for clause in clauses)
    assert any("几天内反馈" in clause for clause in clauses)


def test_planner_overlapping_fragments_keep_each_question_independent() -> None:
    request = _request("设计文档由谁确认、几天内反馈？")
    atoms = _build(
        _payload(
            _atom(("Q.C1", "Q.C2"), "Q.T1", "Q.R1", "COUNT"),
            _atom(("Q.C1", "Q.C2"), "Q.T1", "Q.R2", "COUNT"),
        ),
        request,
    )

    assert [atom.original_fragment for atom in atoms] == [
        "设计文档由谁确认",
        "几天内反馈",
    ]
    assert [atom.answer_shape for atom in atoms] == [
        AtomAnswerShape.RESPONSIBLE_PARTY,
        AtomAnswerShape.DURATION,
    ]


def test_planner_single_atom_cannot_collapse_two_natural_questions() -> None:
    request = _request("设计文档几天内反馈、由谁确认？")
    atoms = _build(
        _payload(
            _atom(("Q.C1", "Q.C2"), "Q.T1", "Q.R1", "COUNT")
        ),
        request,
    )

    assert [atom.original_fragment for atom in atoms] == [
        "设计文档几天内反馈",
        "由谁确认",
    ]
    assert [atom.answer_shape for atom in atoms] == [
        AtomAnswerShape.DURATION,
        AtomAnswerShape.RESPONSIBLE_PARTY,
    ]
    assert all(atom.target == "设计文档" for atom in atoms)


def test_who_performs_an_explicit_action_is_a_party_question() -> None:
    analysis = QueryAnalyzer().analyze(_request("设计文档由谁确认？"))

    assert analysis.semantics.target == "设计文档"
    assert analysis.semantics.relation == "确认"
    assert analysis.semantics.answer_type.value == "RESPONSIBLE_PARTY"


def test_single_duration_question_does_not_fall_back_to_generic_fact() -> None:
    analysis = QueryAnalyzer().analyze(_request("采购应答要留几天？"))
    plan = fallback_query_plan(
        analysis,
        effort="DIRECT",
        reason_code="TEST",
        planner_called=False,
    )

    assert analysis.semantics.target == "采购应答"
    assert plan.atoms[0].answer_shape is AtomAnswerShape.DURATION
