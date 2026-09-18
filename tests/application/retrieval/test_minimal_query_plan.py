"""Planner v3 只能引用受信 Span，约束由服务端重建。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.context_resolution import build_input_spans
from rag_app.application.retrieval.minimal_plan import (
    MinimalPlanPayload,
    MinimalPlanValidationError,
    build_query_atoms,
)
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import KnowledgeBaseScope, SearchRequest


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
