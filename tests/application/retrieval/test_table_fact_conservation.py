"""口语问句命中表格值时，保留同一行的主体、表头与值。"""

from __future__ import annotations

from rag_app.application.retrieval.evidence import _evidence_item
from rag_app.application.retrieval.generation_evidence import (
    _atom_fact_bindings,
    _physical_table_facts,
    _priority_reading_units,
)
from rag_app.core.models import (
    ChunkRole,
    EvidenceItem,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    QueryAtom,
    make_query_plan,
)
from tests.application.retrieval.helpers import make_ranked_chunk


def _cell(
    number: int, row: int, column: int, value: str
) -> tuple[RankedChunk, EvidenceItem]:
    candidate = make_ranked_chunk(number, value, role=ChunkRole.TABLE)
    chunk = candidate.hydrated.chunk
    span = chunk.source_spans[0]
    assert span.source_anchor is not None
    path = ("body", "tbl:1", f"tr:{row}", f"tc:{column}")
    table_span = span.model_copy(
        update={
            "structural_path": path,
            "source_anchor": span.source_anchor.model_copy(
                update={
                    "structural_path": path,
                    "row_index": row,
                    "cell_index": column,
                    "column_index": column,
                }
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={
                            "source_spans": (table_span,),
                            "metadata": freeze_json_object(
                                {
                                    "atoms": [
                                        {
                                            "metadata": {
                                                "table_node_id": (
                                                    f"node_{99:032x}"
                                                ),
                                                "row_index": row,
                                                "cell_source_node_ids": {
                                                    str(column): [span.node_id]
                                                },
                                            }
                                        }
                                    ]
                                }
                            ),
                        }
                    )
                }
            )
        }
    )
    item = _evidence_item(candidate, table_span, value, f"S{number}")
    if row > 0 and column > 0:
        item = item.model_copy(
            update={"rerank_rank": 1 if row == 3 else number}
        )
    return candidate, item


def test_value_match_keeps_exact_row_fact_for_both_question_orders() -> None:
    cells = (
        _cell(1, 0, 0, "责任主体"),
        _cell(2, 0, 1, "确认与反馈时限"),
        _cell(3, 2, 0, "业务团队"),
        _cell(4, 2, 1, "收到设计文档后2个工作日内反馈并确认"),
        _cell(5, 3, 0, "开发中心"),
        _cell(6, 3, 1, "设计文档完成后2个工作日内提交配置库"),
    )
    candidates = {
        candidate.hydrated.chunk.chunk_id: candidate
        for candidate, _ in cells
    }
    items = tuple(item for _, item in cells)
    by_id = {item.support_id: item for item in items}
    target_value = items[3]
    for question in (
        "设计文档由谁确认、几天内反馈？",
        "设计文档几天内反馈、由谁确认？",
    ):
        atoms = (
            QueryAtom(
                atom_id="A1",
                target="设计文档",
                relation="确认",
                answer_shape=AtomAnswerShape.RESPONSIBLE_PARTY,
                original_fragment="设计文档由谁确认",
            ),
            QueryAtom(
                atom_id="A2",
                target="设计文档",
                relation="反馈",
                answer_shape=AtomAnswerShape.DURATION,
                original_fragment="几天内反馈",
            ),
        )
        plan = make_query_plan(
            standalone_query=question,
            intent="FACT",
            effort="DIRECT",
            atoms=atoms,
            reason_code="TEST",
            planner_called=False,
        )
        priority = _priority_reading_units(
            query_plan=plan,
            items=items,
            candidate_by_id=candidates,
            root_evidence=(target_value,),
            atom_candidates_by_atom=(
                ("A1", (target_value,)),
                ("A2", (target_value,)),
            ),
            policy=RetrievalPolicy(),
        )
        selected = {
            owner: {key[0] for key in keys} for owner, keys in priority
        }
        assert selected["A1"] == selected["A2"]
        assert {
            stable_support_key(items[index]) for index in (0, 1, 2, 3)
        } <= selected["A1"]
        assert stable_support_key(items[5]) not in selected["A1"]

        facts = _physical_table_facts(items, candidates)
        bindings = _atom_fact_bindings(
            plan,
            facts,
            items,
            tuple((atom.atom_id, tuple(by_id)) for atom in plan.atoms),
            tuple((atom_id, tuple(keys)) for atom_id, keys in selected.items()),
        )
        target_facts = {
            fact.fact_id
            for fact in facts
            if fact.row_index == 2 and fact.value_column_index == 1
        }
        assert target_facts
        assert {(binding.atom_id, binding.fact_id) for binding in bindings} >= {
            (atom.atom_id, fact_id)
            for atom in plan.atoms
            for fact_id in target_facts
        }
