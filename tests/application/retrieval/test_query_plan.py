"""类型化 Adaptive Planner 与安全单原子回退的定向测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from rag_app.application.retrieval.adaptive import ReasoningEffort
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    QueryAtom,
    fallback_query_plan,
    make_query_plan,
)
from rag_app.product.grounded_runtime import ProductGroundedModel


def _request(question: str) -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=deterministic_id("prj", "typed-plan"),
            knowledge_base_id=deterministic_id("kb", "typed-plan"),
        ),
        text=question,
    )


def _planner_response(
    question: str,
    atoms: list[dict[str, object]] | list[str],
    *,
    raw_content: str | None = None,
    observed_calls: list[dict[str, object]] | None = None,
) -> object:
    payload = {
        "standalone_query": question,
        "intent": "COMPOUND",
        "needs_clarification": False,
        "clarification_question": None,
        "atoms": atoms,
        "route_hints": [],
    }

    class Adapter:
        def complete(self, *_args: object, **kwargs: object) -> object:
            if observed_calls is not None:
                observed_calls.append(kwargs)
            return SimpleNamespace(
                content=raw_content
                if raw_content is not None
                else json.dumps(payload, ensure_ascii=False),
                call=None,
            )

    model = object.__new__(ProductGroundedModel)
    model._campaign_required = False  # type: ignore[assignment]
    model.adapter = Adapter()  # type: ignore[assignment]
    return model


@pytest.mark.parametrize(
    ("question", "parts", "shapes"),
    (
        (
            "甲提交材料、乙审核材料，各自什么时候完成，同时审批要多久？",
            (("甲", "提交时间"), ("乙", "审核时间"), ("审批", "时限")),
            ("FACT", "FACT", "DURATION"),
        ),
        (
            "甲、乙、丙分别负责什么？",
            (("甲", "职责"), ("乙", "职责"), ("丙", "职责")),
            ("DUTIES", "DUTIES", "DUTIES"),
        ),
        (
            "流程输入是什么，同时在什么条件下启动？",
            (("流程", "输入"), ("流程", "启动条件")),
            ("FACT", "FACT"),
        ),
    ),
)
def test_typed_adaptive_plan_preserves_independent_atoms(
    question: str,
    parts: tuple[tuple[str, str], ...],
    shapes: tuple[str, ...],
) -> None:
    request = _request(question)
    atoms = [
        {
            "target": target,
            "relation": relation,
            "answer_shape": shape,
            "source_qualifier": None,
            "constraints": [],
            "original_fragment": None,
        }
        for (target, relation), shape in zip(parts, shapes, strict=True)
    ]
    observed_calls: list[dict[str, object]] = []
    model = _planner_response(question, atoms, observed_calls=observed_calls)

    outcome = model.plan_adaptive(  # type: ignore[union-attr]
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    assert outcome.reason_code == "ADAPTIVE_PLAN_APPLIED"
    assert len(outcome.atoms) == len(parts)
    assert tuple(atom.atom_id for atom in outcome.atoms) == tuple(
        f"A{index}" for index in range(1, len(parts) + 1)
    )
    assert tuple(atom.answer_shape.value for atom in outcome.atoms) == shapes
    assert observed_calls[0]["timeout_seconds"] == 6.0
    assert observed_calls[0]["max_output_tokens"] == 512


def test_constraint_and_source_qualifier_are_preserved() -> None:
    question = "按甲制度V2，乙不得超过5天提交吗？"
    request = _request(question)
    atom = {
        "target": "乙",
        "relation": "提交时限",
        "answer_shape": "DURATION",
        "source_qualifier": "甲制度V2",
        "constraints": [
            {"kind": "VERSION", "value": "V2"},
            {"kind": "NUMBER", "value": "5", "unit": "天"},
            {"kind": "NEGATION", "value": "不得", "polarity": "NEGATIVE"},
        ],
        "original_fragment": "乙不得超过5天提交",
    }
    model = _planner_response(question, [atom])
    outcome = model.plan_adaptive(  # type: ignore[union-attr]
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )

    assert outcome.reason_code == "ADAPTIVE_PLAN_APPLIED"
    assert outcome.atoms[0].source_qualifier == "甲制度V2"
    assert tuple(item.kind.value for item in outcome.atoms[0].constraints) == (
        "VERSION",
        "NUMBER",
        "NEGATION",
    )


def test_planner_invalid_schema_falls_back_to_single_rule_atom() -> None:
    request = _request("甲和乙分别需要多久？")
    model = _planner_response(request.text, ["甲多久", "乙多久"])
    outcome = model.plan_adaptive(  # type: ignore[union-attr]
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    plan = fallback_query_plan(
        QueryAnalyzer().analyze(request),
        effort="DEEP",
        reason_code=outcome.reason_code,
        planner_called=outcome.attempted,
    )

    assert outcome.reason_code == "ADAPTIVE_PLAN_SCHEMA_FALLBACK"
    assert len(plan.atoms) == 1
    assert plan.atoms[0].atom_id == "A1"


@pytest.mark.parametrize("invalid_json", ("{bad json", "[]"))
def test_planner_invalid_json_falls_back_without_user_visible_error(
    invalid_json: str,
) -> None:
    request = _request("甲和乙分别需要多久？")
    model = _planner_response(request.text, [], raw_content=invalid_json)

    outcome = model.plan_adaptive(  # type: ignore[union-attr]
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )

    assert outcome.reason_code == "ADAPTIVE_PLAN_SCHEMA_FALLBACK"
    assert outcome.atoms == ()


def test_planner_more_than_four_atoms_falls_back() -> None:
    request = _request("甲、乙、丙、丁、戊各自负责什么？")
    atoms = [
        {
            "target": target,
            "relation": "职责",
            "answer_shape": "DUTIES",
            "source_qualifier": None,
            "constraints": [],
            "original_fragment": None,
        }
        for target in "甲乙丙丁戊"
    ]
    model = _planner_response(request.text, atoms)

    outcome = model.plan_adaptive(  # type: ignore[union-attr]
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )

    assert outcome.reason_code == "ADAPTIVE_PLAN_SCHEMA_FALLBACK"
    assert outcome.atoms == ()


def test_duplicate_atoms_are_removed_without_answer_table() -> None:
    atom = QueryAtom(
        atom_id="A1",
        target="甲",
        relation="提交时间",
        answer_shape=AtomAnswerShape.FACT,
    )
    plan = make_query_plan(
        standalone_query="甲何时提交？",
        intent="FACT",
        effort="ASSISTED",
        atoms=(atom, atom.model_copy(update={"atom_id": "A2"})),
        reason_code="ADAPTIVE_PLAN_APPLIED",
        planner_called=True,
    )

    assert len(plan.atoms) == 1
    assert plan.atoms[0].atom_id == "A1"
