"""口语问句命中表格值时，保留同一行的主体、表头与值。"""

from __future__ import annotations

from rag_app.application.answering.source_projection import (
    render_physical_table_fact,
)
from rag_app.application.retrieval.evidence import _evidence_item
from rag_app.application.retrieval.generation_evidence import (
    _atom_fact_bindings,
    _physical_table_facts,
    _priority_reading_units,
    _reading_table_identity,
    project_evidence_read_units,
)
from rag_app.core.models import (
    ChunkRole,
    EvidenceItem,
    RankedChunk,
    RetrievalPolicy,
    SourceSpanKind,
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
        candidate.hydrated.chunk.chunk_id: candidate for candidate, _ in cells
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
        selected = {owner: {key[0] for key in keys} for owner, keys in priority}
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


def test_action_coverage_selects_unseeded_closed_row() -> None:
    cells = (
        _cell(1, 0, 0, "责任主体"),
        _cell(2, 0, 1, "反馈时限"),
        _cell(3, 0, 2, "执行动作"),
        _cell(4, 2, 0, "业务团队"),
        _cell(5, 2, 1, "收到设计文档后2个工作日内反馈"),
        _cell(6, 2, 2, "确认设计方向与业务目标一致"),
        _cell(7, 3, 0, "开发中心"),
        _cell(8, 3, 1, "设计文档完成后2个工作日内提交配置库"),
    )
    candidates = {
        candidate.hydrated.chunk.chunk_id: candidate for candidate, _ in cells
    }
    items = tuple(
        item.model_copy(update={"rerank_rank": None})
        if index in {5, 6}
        else item
        for index, (_, item) in enumerate(cells, 1)
    )
    plan = make_query_plan(
        standalone_query="设计文档由谁确认、几天内反馈？",
        intent="FACT",
        effort="DIRECT",
        atoms=(
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
                relation="内反馈",
                answer_shape=AtomAnswerShape.DURATION,
                original_fragment="几天内反馈",
            ),
        ),
        reason_code="TEST",
        planner_called=False,
    )
    priority = _priority_reading_units(
        query_plan=plan,
        items=items,
        candidate_by_id=candidates,
        root_evidence=(items[7],),
        atom_candidates_by_atom=(
            ("A1", (items[7],)),
            ("A2", (items[7],)),
        ),
        policy=RetrievalPolicy(),
    )

    selected = {owner: {key[0] for key in keys} for owner, keys in priority}
    correct_row = {stable_support_key(items[index]) for index in (3, 4, 5)}
    assert correct_row <= selected["A1"]
    assert correct_row <= selected["A2"]
    assert stable_support_key(items[7]) not in selected["A1"]


def test_explicit_level_keeps_unseeded_matching_row() -> None:
    """目标行已进入有界池时，完整行名优先于邻行重排。"""
    cells = (
        _cell(1, 0, 0, "设备故障级别"),
        _cell(2, 0, 1, "通知方式"),
        _cell(3, 0, 2, "通知时限"),
        _cell(4, 1, 0, "设备故障事件（Ⅰ级）"),
        _cell(5, 1, 1, "疑似发现后电话和邮件通知"),
        _cell(6, 1, 2, "4分钟"),
        _cell(7, 2, 0, "设备故障事件（Ⅱ级）"),
        _cell(8, 2, 2, "7分钟"),
    )
    candidates = {
        candidate.hydrated.chunk.chunk_id: (
            candidate.model_copy(update={"rerank_rank": None})
            if index >= 6
            else candidate
        )
        for index, (candidate, _item) in enumerate(cells)
    }
    items = tuple(
        item.model_copy(update={"rerank_rank": None}) if index >= 6 else item
        for index, (_candidate, item) in enumerate(cells)
    )
    question = "设备故障事件（Ⅱ级）疑似发现后如何通知，多久完成？"
    plan = make_query_plan(
        standalone_query=question,
        intent="FACT",
        effort="DIRECT",
        atoms=(
            QueryAtom(
                atom_id="A1",
                target="通知方式",
                relation="通知",
                answer_shape=AtomAnswerShape.FACT,
            ),
            QueryAtom(
                atom_id="A2",
                target="通知时限",
                relation="时限",
                answer_shape=AtomAnswerShape.DURATION,
            ),
        ),
        reason_code="TEST",
        planner_called=False,
    )
    priority = _priority_reading_units(
        query_plan=plan,
        items=items,
        candidate_by_id=candidates,
        root_evidence=(items[3], items[4], items[5]),
        atom_candidates_by_atom=(
            ("A1", (items[4],)),
            ("A2", (items[5],)),
        ),
        policy=RetrievalPolicy(),
    )

    selected = {owner: {key[0] for key in keys} for owner, keys in priority}
    target = stable_support_key(items[7])
    adjacent = stable_support_key(items[5])
    assert target in selected["A1"]
    assert target in selected["A2"]
    assert adjacent not in selected["A1"]
    assert adjacent not in selected["A2"]


def test_vertical_merge_restores_only_complete_mapped_value() -> None:
    """跨行继承须有规范节点映射和完整原始单元格，不能借邻行值。"""
    source_text = "疑似发现后电话通知。\n确认后发送邮件。"
    _original_candidate, original = _cell(20, 1, 1, source_text)
    inherited_candidate, _unused = _cell(21, 2, 1, source_text)
    original_span = original.source_spans[0]
    repeated_span = original_span.model_copy(
        update={
            "span_type": SourceSpanKind.REPEATED_CONTEXT,
            "is_repeated": True,
        }
    )
    chunk = inherited_candidate.hydrated.chunk
    atoms = dict(chunk.metadata)["atoms"]
    assert isinstance(atoms, list)
    atom = atoms[0]
    assert isinstance(atom, dict)
    metadata = atom["metadata"]
    assert isinstance(metadata, dict)
    metadata["cell_source_node_ids"] = {"1": [original_span.node_id]}
    inherited_candidate = inherited_candidate.model_copy(
        update={
            "hydrated": inherited_candidate.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={
                            "source_spans": (repeated_span,),
                            "metadata": freeze_json_object({"atoms": atoms}),
                        }
                    )
                }
            )
        }
    )
    inherited = _evidence_item(
        inherited_candidate, repeated_span, source_text, "S21"
    )
    cells = (
        _cell(22, 0, 0, "事件级别"),
        _cell(23, 0, 1, "通知方式"),
        _cell(24, 2, 0, "二级事件"),
        (inherited_candidate, inherited),
    )
    candidates = {
        candidate.hydrated.chunk.chunk_id: candidate for candidate, _ in cells
    }
    evidence = tuple(item for _, item in cells)
    assert _reading_table_identity(inherited, inherited_candidate) is not None
    facts = _physical_table_facts(evidence, candidates)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.row_index == 2
    assert fact.value_origin_row_index == 1
    unit = project_evidence_read_units(evidence, facts)[-1]
    assert source_text in unit.text
    assert source_text in render_physical_table_fact(
        fact, {item.support_id: item for item in evidence}
    )

    cut = len(source_text) // 2
    left_span = repeated_span.model_copy(
        update={"chunk_end_char": cut + 2, "source_end_char": cut + 2}
    )
    right_span = repeated_span.model_copy(
        update={"chunk_start_char": cut, "source_start_char": cut}
    )
    left = _evidence_item(
        inherited_candidate, left_span, source_text[: cut + 2], "S31"
    )
    right = _evidence_item(
        inherited_candidate, right_span, source_text[cut:], "S32"
    )
    overlapped = (*evidence[:-1], left, right)
    overlapped_fact = _physical_table_facts(overlapped, candidates)[0]
    overlapped_unit = project_evidence_read_units(
        overlapped, (overlapped_fact,)
    )[-1]
    assert source_text in overlapped_unit.text
    assert source_text in render_physical_table_fact(
        overlapped_fact, {item.support_id: item for item in overlapped}
    )

    half = len(source_text) // 2
    partial_span = repeated_span.model_copy(
        update={"chunk_end_char": half, "source_end_char": half}
    )
    partial = _evidence_item(
        inherited_candidate, partial_span, source_text[:half], "S21"
    )
    assert not _physical_table_facts((*evidence[:-1], partial), candidates)

    wrong_metadata = freeze_json_object(
        {
            "atoms": [
                {
                    "metadata": {
                        "table_node_id": f"node_{99:032x}",
                        "row_index": 2,
                        "cell_source_node_ids": {"1": [f"node_{98:032x}"]},
                    }
                }
            ]
        }
    )
    wrong_candidate = inherited_candidate.model_copy(
        update={
            "hydrated": inherited_candidate.hydrated.model_copy(
                update={
                    "chunk": inherited_candidate.hydrated.chunk.model_copy(
                        update={"metadata": wrong_metadata}
                    )
                }
            )
        }
    )
    wrong = _evidence_item(wrong_candidate, repeated_span, source_text, "S21")
    assert _reading_table_identity(wrong, wrong_candidate) is None
