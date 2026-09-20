"""表格关系只在问句与物理行列轴明确对齐时确定。"""

from __future__ import annotations

from rag_app.application.retrieval.generation_evidence import (
    _atom_fact_bindings,
)
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    QueryAtom,
    make_query_plan,
)
from tests.application.answering.test_evidence_binding import _fixture


def _binding_for(*, target: str, relation: str, original_fragment: str) -> str:
    evidence, fact, _unit, _binding = _fixture()
    replacements = {
        "S1": "需求快验",
        "S2": "需求功能点描述",
        "S3": "验收标准",
        "S4": "演示目标",
        "S5": "用户场景",
        "S6": "界面设计草图",
        "S7": "其他输入",
        "S8": "补充说明",
        "S9": "输入",
        "S10": "（业务团队 / 外部单位需提供）",
    }
    evidence = tuple(
        item.model_copy(update={"citation_text": replacements[item.support_id]})
        for item in evidence
    )
    atom = QueryAtom(
        atom_id="A1",
        target=target,
        relation=relation,
        answer_shape=AtomAnswerShape.FACT,
        original_fragment=original_fragment,
    )
    plan = make_query_plan(
        standalone_query=original_fragment,
        original_query=original_fragment,
        intent="FACT",
        effort="DIRECT",
        atoms=(atom,),
        reason_code="SYNTHETIC",
        planner_called=False,
    )
    bindings = _atom_fact_bindings(
        plan,
        (fact,),
        evidence,
        (("A1", fact.all_support_ids),),
        (
            (
                "A1",
                tuple(stable_support_key(item) for item in evidence),
            ),
        ),
    )
    assert len(bindings) == 1
    return bindings[0].relation_status


def test_explicit_short_row_and_column_axes_certify_table_relation() -> None:
    question = "需求快验的“输入”项列了哪些内容？"

    assert (
        _binding_for(
            target=question.rstrip("？"),
            relation="内容",
            original_fragment=question,
        )
        == "SUPPORTED"
    )


def test_query_only_timing_and_modality_stay_undetermined() -> None:
    assert (
        _binding_for(
            target="需求快验",
            relation="提供",
            original_fragment="需求快验之前必须提供什么？",
        )
        == "UNDETERMINED"
    )
