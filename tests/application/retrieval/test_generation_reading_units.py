"""有界结构闭合必须能保留目标行的真实阅读单元。"""

from __future__ import annotations

from rag_app.application.retrieval.evidence import _evidence_item
from rag_app.core.models import (
    ChunkRole,
    EvidenceItem,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from tests.application.retrieval.helpers import make_ranked_chunk
from tests.application.retrieval.test_generation_evidence_pack import (
    _pack,
    _plan,
)


def _table_cell(number: int, text: str, row: int, column: int) -> RankedChunk:
    """构造已闭合且有真实节点映射的独立表格单元格。

    Args:
        number: 唯一来源编号。
        text: 合成原文。
        row: 原表行号。
        column: 原表列号。

    Returns:
        保留独立来源范围和结构映射的候选。

    """
    candidate = make_ranked_chunk(number, text, role=ChunkRole.TABLE)
    chunk = candidate.hydrated.chunk
    span = chunk.source_spans[0]
    assert span.source_anchor is not None
    path = ("body", "tbl:1", f"tr:{row}", f"tc:{column}", "p:1")
    source = span.model_copy(
        update={
            "structural_path": path,
            "source_anchor": span.source_anchor.model_copy(
                update={"structural_path": path}
            ),
        }
    )
    metadata = freeze_json_object(
        {
            "atoms": [
                {
                    "metadata": {
                        "table_node_id": f"node_{900:032x}",
                        "row_index": row,
                        "header_strategy": "tblHeader" if row == 0 else "none",
                        "header_confidence": 1.0 if row == 0 else 0.0,
                        "cell_source_node_ids": {str(column): [source.node_id]},
                    }
                }
            ]
        }
    )
    return candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"source_spans": (source,), "metadata": metadata}
                    )
                }
            ),
            "expansion_reason": "STRUCTURE_CONTINUITY",
        }
    )


def _sources() -> tuple[RankedChunk, ...]:
    """返回无须依赖旧语义证书的完整目标行及已认证表头。"""
    values = (
        (
            "工装试制",
            "用于检查新工装。",
            "图纸基线、材料清单。",
            "样件。",
            "检查通过并留存签字记录后开工。",
        ),
        ("任务类型", "定义", "输入", "输出", "启动条件"),
    )
    row = tuple(
        _table_cell(30 + column, value, 2, column)
        for column, value in enumerate(values[0])
    )
    header_cells = tuple(
        _table_cell(40 + column, value, 0, column)
        for column, value in enumerate(values[1])
    )
    header = header_cells[0]
    header_text = " | ".join(values[1])
    offset = 0
    spans = []
    mapping = {}
    for column, cell in enumerate(header_cells):
        span = cell.hydrated.chunk.source_spans[0]
        text = cell.hydrated.chunk.citation_text
        spans.append(
            span.model_copy(
                update={
                    "chunk_start_char": offset,
                    "chunk_end_char": offset + len(text),
                }
            )
        )
        mapping[str(column)] = [span.node_id]
        offset += len(text) + 3
    header_metadata = freeze_json_object(
        {
            "atoms": [
                {
                    "metadata": {
                        "table_node_id": f"node_{900:032x}",
                        "row_index": 0,
                        "header_strategy": "tblHeader",
                        "cell_source_node_ids": mapping,
                    }
                }
            ]
        }
    )
    header = header.model_copy(
        update={
            "hydrated": header.hydrated.model_copy(
                update={
                    "chunk": header.hydrated.chunk.model_copy(
                        update={
                            "citation_text": header_text,
                            "source_spans": tuple(spans),
                            "metadata": header_metadata,
                        }
                    )
                }
            )
        }
    )
    return (row[0].model_copy(update={"rerank_rank": 20}), *row[1:], header)


def _reading_keys(pack: object) -> set[str]:
    """兼容修复前缺字段的失败形状，不把缺观测误判为通过。

    Args:
        pack: 当前实际装配结果。

    Returns:
        应用声明的优先阅读来源稳定身份集合。

    """
    return {
        key
        for _owner, members in getattr(pack, "priority_source_units", ())
        for key in members
    }


def test_closed_row_values_survive_old_candidate_admission() -> None:
    """行名被 Rerank 选中后，无分数的同表闭合事实不能消失。"""
    candidates = _sources()
    atom = QueryAtom(
        atom_id="A1",
        target="试制",
        relation="准备事项",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "试制前得备啥？",
            "resolved_root_query": "试制前得备啥？",
        }
    )
    pack = _pack(plan, candidates)
    by_text = {item.citation_text: item for item in pack.evidence}
    expected = (
        "工装试制",
        "图纸基线、材料清单。",
        "检查通过并留存签字记录后开工。",
    )
    assert set(expected) <= set(by_text)
    assert {
        stable_support_key(by_text[text]) for text in expected
    } <= _reading_keys(pack)
    assert all(by_text[text].rerank_rank is None for text in expected[1:])
    assert set(by_text) >= {"输入", "启动条件"}
    assert not pack.complete_group_ids


def test_reading_unit_keeps_target_relation_under_ordinary_item_cap() -> None:
    """阅读预算须保留行名、表头与目标事实，不能只留下高排名主题句。"""
    candidates = _sources()
    distractor = make_ranked_chunk(
        1, "试制申请登记表需要填写。", document_number=3
    ).model_copy(update={"rerank_rank": 1})
    root = _evidence_item(
        distractor,
        distractor.hydrated.chunk.source_spans[0],
        distractor.hydrated.chunk.citation_text,
        "S1",
    )
    atom = QueryAtom(
        atom_id="A1",
        target="试制",
        relation="准备事项",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "试制前得备啥？",
            "resolved_root_query": "试制前得备啥？",
        }
    )
    pack = _pack(
        plan,
        (distractor, *candidates),
        root=(root,),
        policy=RetrievalPolicy(
            generation_max_ordinary_items=2, generation_per_document_cap=1
        ),
    )
    assert {"图纸基线、材料清单。", "检查通过并留存签字记录后开工。"} <= {
        item.citation_text for item in pack.evidence
    }
    assert stable_support_key(root) not in _reading_keys(pack)


def test_priority_row_cannot_borrow_another_story_or_row() -> None:
    """同列内容也不得凭主题相近加入另一故事或兄弟行的阅读单元。"""
    candidates = _sources()
    sibling = _table_cell(70, "无需批准即可开工。", 3, 4)
    other_story = _table_cell(71, "免交材料清单。", 2, 2)
    chunk = other_story.hydrated.chunk
    span = chunk.source_spans[0]
    assert span.source_anchor is not None
    other_anchor = span.source_anchor.model_copy(
        update={"part_uri": "/word/header1.xml"}
    )
    other_story = other_story.model_copy(
        update={
            "hydrated": other_story.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={
                            "source_spans": (
                                span.model_copy(
                                    update={"source_anchor": other_anchor}
                                ),
                            )
                        }
                    )
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="试制",
        relation="准备事项",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "试制前得备啥？",
            "resolved_root_query": "试制前得备啥？",
        }
    )
    pack = _pack(plan, (*candidates, sibling, other_story))
    priority = _reading_keys(pack)
    assert priority
    forbidden = {
        sibling.hydrated.chunk.chunk_id,
        other_story.hydrated.chunk.chunk_id,
    }
    assert all(
        stable_support_key(item) not in priority
        for item in pack.evidence
        if item.chunk_id in forbidden
    )


def test_priority_unit_has_no_synthetic_semantic_certificate() -> None:
    """阅读相容不伪造事实支持，也不把 null group 自动标为完整。"""
    atom = QueryAtom(
        atom_id="A1",
        target="试制",
        relation="准备事项",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "试制前得备啥？",
            "resolved_root_query": "试制前得备啥？",
        }
    )
    pack = _pack(plan, _sources())
    priority = _reading_keys(pack)
    assert priority
    items: tuple[EvidenceItem, ...] = pack.evidence
    assert all(
        not dict(item.metadata).get("answer_support")
        for item in items
        if stable_support_key(item) in priority
    )
    assert len(pack.entries) <= 44


def test_explicit_columns_form_a_smaller_reading_unit() -> None:
    """列名明确时保留行名和所问交点，不把整行不可分割化。"""
    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.FACT,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "工装试制的输入有哪些？",
            "resolved_root_query": "工装试制的输入有哪些？",
        }
    )
    pack = _pack(plan, _sources())
    priority = _reading_keys(pack)
    texts = {
        item.citation_text
        for item in pack.evidence
        if stable_support_key(item) in priority
    }
    assert texts == {"工装试制", "图纸基线、材料清单。", "任务类型", "输入"}


def test_inactive_closure_cannot_enter_priority_unit() -> None:
    """结构闭合也必须逐项通过活动索引与授权范围硬边界。"""
    candidates = list(_sources())
    inactive = candidates[2]
    candidates[2] = inactive.model_copy(
        update={
            "hydrated": inactive.hydrated.model_copy(
                update={
                    "chunk": inactive.hydrated.chunk.model_copy(
                        update={"index_revision_id": f"irev_{'e' * 32}"}
                    )
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="试制",
        relation="准备事项",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "试制前得备啥？",
            "resolved_root_query": "试制前得备啥？",
        }
    )
    pack = _pack(plan, tuple(candidates))
    assert inactive.hydrated.chunk.chunk_id not in {
        item.chunk_id for item in pack.evidence
    }
    assert any(
        item.evidence_item.chunk_id == inactive.hydrated.chunk.chunk_id
        for item in pack.rejected_entries
    )
    assert not pack.complete_group_ids
