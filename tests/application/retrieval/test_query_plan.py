"""类型化 Planner v3、失败分类及可信降级。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from rag_app.application.retrieval.adaptive import (
    FieldResolutionExecutionState,
    ReasoningEffort,
)
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.context_resolution import (
    build_input_spans,
    degraded_query_plan,
    resolve_root_query,
)
from rag_app.application.retrieval.source_scope import (
    resolve_query_source_context,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    FieldCandidate,
    FieldResolutionStatus,
    KnowledgeBaseScope,
    ResolvedQueryView,
    SearchRequest,
)
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.model_settings import KnowledgeBaseModelSettings


def _request(question: str, context: tuple[str, ...] = ()) -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=deterministic_id("prj", "typed-plan"),
            knowledge_base_id=deterministic_id("kb", "typed-plan"),
        ),
        text=question,
        conversation_context=context,
    )


def _atom(
    clause: str, target: str, relation: str, shape: str
) -> dict[str, object]:
    return {
        "fragment_span_ids": [clause],
        "target_span_id": target,
        "relation_span_id": relation,
        "answer_shape": shape,
    }


def _planner_response(
    atoms: list[dict[str, object]] | list[str],
    *,
    raw_content: str | None = None,
    observed_calls: list[dict[str, object]] | None = None,
) -> ProductGroundedModel:
    payload = {
        "intent": "COMPOUND",
        "clarification_reason": None,
        "atoms": atoms,
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
                usage=SimpleNamespace(prompt_tokens=30, completion_tokens=40),
                finish_reason="stop",
            )

    model = object.__new__(ProductGroundedModel)
    model._campaign_required = False  # type: ignore[assignment]
    model.adapter = Adapter()  # type: ignore[assignment]
    model.settings = KnowledgeBaseModelSettings()  # type: ignore[assignment]
    return model


def _field_candidate(
    candidate_id: str,
    *,
    atom_id: str,
    column: int,
    field_label: str,
) -> FieldCandidate:
    """构造只含安全预览的真实 schema 字段候选。"""
    return FieldCandidate(
        candidate_id=candidate_id,
        atom_id=atom_id,
        fact_id=canonical_sha256(
            {"atom_id": atom_id, "column": column, "kind": "fact"}
        ),
        document_id=deterministic_id("doc", "field-resolution"),
        document_version_id=deterministic_id("dver", "field-resolution"),
        table_key=canonical_sha256({"kind": "table"}),
        row_index=1,
        value_column_index=column,
        target_label="需求快验",
        field_label=field_label,
        value_preview=f"{field_label}的有限预览",
        dependency_support_ids=(f"S{column}",),
    )


def _field_response(
    payload: dict[str, object],
    observed_calls: list[dict[str, object]],
) -> ProductGroundedModel:
    """构造记录调用次数的一次性字段解释模型。"""

    class Adapter:
        def complete(self, *_args: object, **kwargs: object) -> object:
            observed_calls.append(kwargs)
            return SimpleNamespace(
                content=json.dumps(payload, ensure_ascii=False),
                call=None,
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
                finish_reason="stop",
            )

    model = object.__new__(ProductGroundedModel)
    model._campaign_required = False  # type: ignore[assignment]
    model.adapter = Adapter()  # type: ignore[assignment]
    model.settings = KnowledgeBaseModelSettings()  # type: ignore[assignment]
    return model


def _field_contract_context(
    request: SearchRequest,
    *fragments: str,
) -> tuple[ResolvedQueryView, tuple[QueryAtom, ...]]:
    """为旧 DOMAIN_UNIT_TEST 构造生产同形的问题视图与 Atom。"""
    query_view = resolve_query_source_context(
        request.text,
        (),
        registry_revision="unit-field-contract-v1",
    ).query_view
    atoms = tuple(
        QueryAtom(
            atom_id=f"A{index}",
            target=fragment,
            relation="对应字段",
            answer_shape=AtomAnswerShape.FACT,
            original_fragment=fragment,
        )
        for index, fragment in enumerate(fragments, start=1)
    )
    return query_view, atoms


@pytest.mark.parametrize(
    ("question", "atoms", "shapes"),
    (
        (
            "甲提交材料、乙审核材料，各自什么时候完成，同时审批要多久？",
            [
                _atom("Q.C1", "Q.T1", "Q.R1", "FACT"),
                _atom("Q.C2", "Q.T2", "Q.R2", "FACT"),
                _atom("Q.C3", "Q.T4", "Q.R3", "DURATION"),
            ],
            ("FACT", "FACT", "DURATION"),
        ),
        (
            "甲、乙、丙分别负责什么？",
            [
                _atom("Q.C1", "Q.T1", "Q.R1", "DUTIES"),
                _atom("Q.C1", "Q.T2", "Q.R1", "DUTIES"),
                _atom("Q.C1", "Q.T3", "Q.R1", "DUTIES"),
            ],
            ("DUTIES", "DUTIES", "DUTIES"),
        ),
        (
            "流程输入是什么，同时在什么条件下启动？",
            [
                _atom("Q.C1", "Q.T1", "Q.R1", "FACT"),
                _atom("Q.C2", "Q.T1", "Q.R2", "FACT"),
            ],
            ("FACT", "FACT"),
        ),
    ),
)
def test_typed_adaptive_plan_preserves_independent_atoms(
    question: str, atoms: list[dict[str, object]], shapes: tuple[str, ...]
) -> None:
    request = _request(question)
    observed_calls: list[dict[str, object]] = []
    model = _planner_response(atoms, observed_calls=observed_calls)
    outcome = model.plan_adaptive(
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    assert outcome.reason_code == "ADAPTIVE_PLAN_APPLIED"
    assert tuple(atom.answer_shape.value for atom in outcome.atoms) == shapes
    assert len(outcome.atoms) == len(atoms)
    assert observed_calls[0]["timeout_seconds"] == 8.0
    assert observed_calls[0]["max_output_tokens"] == 160
    assert outcome.planner_output_tokens == 40


def test_constraint_and_source_qualifier_are_preserved() -> None:
    request = _request("根据《甲制度V2》，乙不得超过5天提交吗？")
    atom = _atom("Q.C2", "Q.T1", "Q.R2", "DURATION")
    atom["fragment_span_ids"] = ["Q.C1", "Q.C2"]
    outcome = _planner_response([atom]).plan_adaptive(
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    assert outcome.reason_code == "ADAPTIVE_PLAN_APPLIED"
    assert outcome.atoms[0].source_qualifier == "甲制度V2"
    assert {
        (item.kind.value, item.value) for item in outcome.atoms[0].constraints
    } == {
        ("VERSION", "V2"),
        ("DURATION", "5"),
        ("NEGATION", "不得"),
        ("SOURCE", "甲制度V2"),
    }


def test_planner_invalid_schema_falls_back_to_rule_atoms() -> None:
    request = _request("甲什么时候提交，乙多久审核？")
    outcome = _planner_response(["甲多久", "乙多久"]).plan_adaptive(
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    spans = build_input_spans(request)
    plan = degraded_query_plan(
        request,
        QueryAnalyzer().analyze(request),
        spans,
        resolve_root_query(request, spans),
        effort="DEEP",
        reason_code=outcome.reason_code,
        planner_called=outcome.attempted,
    )
    assert outcome.reason_code == "PLANNER_INVALID_SCHEMA"
    assert len(plan.atoms) == 2
    assert plan.fallback_mode == "DEGRADED_RULE_ATOMS"


@pytest.mark.parametrize("invalid_json", ("{bad json", "[]"))
def test_planner_invalid_json_falls_back_without_user_visible_error(
    invalid_json: str,
) -> None:
    request = _request("甲和乙分别需要多久？")
    outcome = _planner_response([], raw_content=invalid_json).plan_adaptive(
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    assert outcome.reason_code in {
        "PLANNER_INVALID_JSON",
        "PLANNER_INVALID_SCHEMA",
    }
    assert outcome.failure_category == outcome.reason_code


def test_planner_more_than_four_atoms_falls_back() -> None:
    request = _request("甲、乙、丙分别负责什么？")
    atoms = [_atom("Q.C1", "Q.T1", "Q.R1", "DUTIES") for _ in range(5)]
    outcome = _planner_response(atoms).plan_adaptive(
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    assert outcome.reason_code == "PLANNER_INVALID_SCHEMA"


def test_schema_failure_trace_records_only_shape_not_question_body() -> None:
    request = _request("甲什么时候提交？")
    malformed = json.dumps(
        {
            "i": "CLARIFICATION",
            "c": "MISSING_TARGET",
            "a": [_atom("Q.C1", "Q.T1", "Q.R1", "FACT")],
        },
        ensure_ascii=False,
    )
    outcome = _planner_response([], raw_content=malformed).plan_adaptive(
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )
    assert outcome.reason_code == "PLANNER_INVALID_SCHEMA"
    assert "intent=CLARIFICATION:atoms=1:reason=SET" in (
        outcome.schema_fallback_detail or ""
    )
    assert "甲" not in (outcome.schema_fallback_detail or "")


def test_field_resolution_reuses_one_interpret_call_after_schema() -> None:
    """schema-aware 解析只调用一次并保留原问逐字跨度。"""
    request = _request("需求快验开始前必须准备哪些材料？")
    candidates = (
        _field_candidate("F1", atom_id="A1", column=1, field_label="输入"),
        _field_candidate("F2", atom_id="A1", column=2, field_label="输出"),
    )
    observed_calls: list[dict[str, object]] = []
    model = _field_response(
        {
            "r": [
                {
                    "a": "A1",
                    "s": "SUPPORTED_PARAPHRASE",
                    "c": ["F1"],
                    "q": "准备哪些材料",
                }
            ]
        },
        observed_calls,
    )

    query_view, atoms = _field_contract_context(request, request.text)
    outcome = model.resolve_fields(
        request, candidates, query_view=query_view, atoms=atoms
    )

    assert len(observed_calls) == 1
    assert observed_calls[0]["operation"] == "query.interpret"
    assert outcome.reason_code == "FIELD_RESOLUTION_ACCEPTED"
    assert outcome.resolutions[0].status is (
        FieldResolutionStatus.SUPPORTED_PARAPHRASE
    )
    assert outcome.resolutions[0].candidate_ids == ("F1",)
    start = request.text.index("准备哪些材料")
    assert (
        outcome.resolutions[0].query_span_start,
        outcome.resolutions[0].query_span_end,
    ) == (start, start + len("准备哪些材料"))


def test_field_resolution_rejects_multi_atom_single_http() -> None:
    """单次 HTTP 只资格化一个 Atom，多 Atom 必须由应用层分包。"""
    request = _request("分别说明甲和乙的输入？")
    candidates = (
        _field_candidate("F1", atom_id="A1", column=1, field_label="甲输入"),
        _field_candidate("F2", atom_id="A2", column=1, field_label="乙输入"),
    )
    observed_calls: list[dict[str, object]] = []
    model = _field_response(
        {
            "r": [
                {
                    "a": "A1",
                    "s": "SUPPORTED_PARAPHRASE",
                    "c": ["F2"],
                    "q": "甲",
                },
                {
                    "a": "A2",
                    "s": "RELATED_FIELD",
                    "c": ["F2"],
                    "q": "乙",
                },
            ]
        },
        observed_calls,
    )

    query_view, atoms = _field_contract_context(request, "甲", "乙")
    outcome = model.resolve_fields(
        request, candidates, query_view=query_view, atoms=atoms
    )

    assert observed_calls == []
    assert outcome.reason_code == "FIELD_RESOLUTION_CAPABILITY_UNQUALIFIED"
    assert outcome.execution_state is (
        FieldResolutionExecutionState.REQUEST_REJECTED
    )
    assert outcome.resolutions == ()


def test_field_resolution_rejects_non_verbatim_query_span() -> None:
    """模型返回的解释跨度必须逐字来自当前业务问题。"""
    request = _request("需求快验要准备什么？")
    observed_calls: list[dict[str, object]] = []
    model = _field_response(
        {
            "r": [
                {
                    "a": "A1",
                    "s": "SUPPORTED_PARAPHRASE",
                    "c": ["F1"],
                    "q": "需要提前准备的材料",
                }
            ]
        },
        observed_calls,
    )

    query_view, atoms = _field_contract_context(request, request.text)
    outcome = model.resolve_fields(
        request,
        (_field_candidate("F1", atom_id="A1", column=1, field_label="输入"),),
        query_view=query_view,
        atoms=atoms,
    )

    assert len(observed_calls) == 1
    assert outcome.reason_code == "FIELD_RESOLUTION_OUTPUT_INVALID"
