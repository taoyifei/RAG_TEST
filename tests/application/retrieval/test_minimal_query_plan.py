"""最小 Planner 只允许引用原问，硬约束由服务端构造。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.minimal_plan import (
    MinimalPlanPayload,
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


def _payload(
    atoms: list[dict[str, object]], *, intent: str = "COMPOUND"
) -> MinimalPlanPayload:
    return MinimalPlanPayload.model_validate(
        {
            "intent": intent,
            "needs_clarification": False,
            "clarification_question": None,
            "atoms": atoms,
        }
    )


def test_original_fragments_and_server_constraints_remain_intact() -> None:
    request = _request("根据《甲制度V2》，乙不得超过5天提交吗？")
    payload = _payload(
        [
            {
                "fragment": "根据《甲制度V2》，乙不得超过5天提交吗",
                "target": "乙",
                "relation": "提交时限",
                "answer_shape": "DURATION",
            }
        ],
        intent="SINGLE",
    )

    atom = build_query_atoms(
        payload, request, QueryAnalyzer().analyze(request)
    )[0]

    assert atom.atom_id == "A1"
    assert atom.original_fragment == payload.atoms[0].fragment
    assert atom.source_qualifier == "甲制度V2"
    assert {
        (item.kind.value, item.value, item.unit)
        for item in atom.constraints
    } == {
        ("VERSION", "V2", None),
        ("DURATION", "5", "天"),
        ("NEGATION", "不得", None),
        ("SOURCE", "甲制度V2", None),
    }


@pytest.mark.parametrize(
    ("fragment", "target", "relation"),
    (
        ("不存在的片段", "甲", "提交时间"),
        ("甲什么时候提交", "丙", "提交时间"),
        ("甲什么时候提交", "甲", "提交前7天"),
        ("甲什么时候提交", "甲", "X-99提交时间"),
    ),
)
def test_unanchored_or_invented_planner_field_rejects_entire_plan(
    fragment: str, target: str, relation: str
) -> None:
    request = _request("甲什么时候提交，乙多久审核？")
    payload = _payload(
        [
            {
                "fragment": fragment,
                "target": target,
                "relation": relation,
                "answer_shape": "FACT",
            },
            {
                "fragment": "乙多久审核",
                "target": "乙",
                "relation": "审核时限",
                "answer_shape": "DURATION",
            },
        ]
    )

    with pytest.raises(ValueError):
        build_query_atoms(payload, request, QueryAnalyzer().analyze(request))


def test_missing_independent_clause_rejects_entire_plan() -> None:
    request = _request("甲什么时候提交，乙多久审核？")
    payload = _payload(
        [
            {
                "fragment": "甲什么时候提交",
                "target": "甲",
                "relation": "提交时间",
                "answer_shape": "FACT",
            }
        ]
    )

    with pytest.raises(ValueError, match="独立问句"):
        build_query_atoms(payload, request, QueryAnalyzer().analyze(request))


def test_recent_context_relation_is_allowed() -> None:
    request = _request(
        "乙呢？", context=("甲什么时候提交？", "甲审核多久？")
    )
    payload = _payload(
        [
            {
                "fragment": "乙呢",
                "target": "乙",
                "relation": "提交时间",
                "answer_shape": "FACT",
            }
        ],
        intent="FOLLOW_UP",
    )

    atoms = build_query_atoms(
        payload, request, QueryAnalyzer().analyze(request)
    )

    assert len(atoms) == 1
    assert atoms[0].target == "乙"
