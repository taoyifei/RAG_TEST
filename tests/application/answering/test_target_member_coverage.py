"""职责完整性须覆盖目标成员，不能由无关完整结构代替。"""

from __future__ import annotations

from rag_app.application.answering.grounded import _source_fact_content_covered
from rag_app.application.answering.target_coverage import (
    TargetMemberCoverage,
    target_member_coverage,
)
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import AnswerClaim, ClaimSupport, EvidenceItem
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.evidence_group import EvidenceGroup, EvidenceGroupKind
from rag_app.core.models.query import QuerySemantics, RequestedAnswerType
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from tests.application.answering.source_contract_fixtures import (
    trusted_list_group,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _table_cell,
)
from tests.application.retrieval.test_descriptive_answers import (
    _POLICY,
    _candidates,
    _paragraph,
)
from tests.application.retrieval.test_evidence_groups import (
    _groups,
    _table_row,
    _with_chunk,
)


def _table() -> tuple[tuple[EvidenceItem, ...], EvidenceGroup]:
    specs = (
        (0, 0, "角色"),
        (0, 1, "主要职责"),
        (0, 2, "交付成果"),
        (1, 0, "开发团队"),
        (1, 1, "负责设计与开发。"),
        (1, 1, "负责现场调试与问题修复。"),
        (1, 2, "测试报告。"),
        (2, 0, "运营团队"),
        (2, 1, "负责联系客户。"),
    )
    by_text = {
        item.citation_text: item
        for item in EvidenceAssembler().assemble(
            _candidates("".join(_paragraph(text) for _, _, text in specs)),
            _POLICY.model_copy(
                update={
                    "max_evidence_items_per_chunk": 16,
                    "max_evidence_items": 16,
                    "per_document_cap": 16,
                    "per_section_cap": 16,
                }
            ),
        )
    }
    evidence = tuple(
        _table_cell(by_text[text], row, column) for row, column, text in specs
    )
    group = trusted_list_group(evidence).model_copy(
        update={
            "kind": EvidenceGroupKind.TABLE_ROW_GROUP,
            "heading_path": ("协同方案", "首单交付阶段"),
            "metadata": freeze_json_object(
                {
                    "canonical_header_node_ids": [
                        item.source_spans[0].node_id for item in evidence[:3]
                    ]
                }
            ),
        }
    )
    return evidence, group


def _claims(*items: EvidenceItem) -> tuple[AnswerClaim, ...]:
    return tuple(
        AnswerClaim(
            text=item.citation_text,
            supports=(
                ClaimSupport(
                    support_id=item.support_id, quote=item.citation_text
                ),
            ),
        )
        for item in items
    )


def _coverage(
    evidence: tuple[EvidenceItem, ...],
    group: EvidenceGroup,
    claims: tuple[AnswerClaim, ...],
    *,
    stage: str = "首单交付",
) -> TargetMemberCoverage:
    return target_member_coverage(
        QueryAtom(
            atom_id="A1",
            target="开发团队",
            relation="职责",
            answer_shape=AtomAnswerShape.DUTIES,
        ),
        evidence,
        claims,
        trusted_groups=(group,),
        semantics=QuerySemantics(
            target="开发团队",
            relation="职责",
            context_qualifier=stage,
            answer_type=RequestedAnswerType.DUTIES,
        ),
        fact_covered=_source_fact_content_covered,
    )


def test_complete_target_duties_do_not_require_other_role_or_output() -> None:
    evidence, group = _table()
    kept = evidence[:6]
    result = _coverage(kept, group, _claims(*evidence[4:6]))
    assert result.complete
    assert result.source_complete
    assert len(result.required_member_keys) == 2
    assert result.covered_member_keys == result.required_member_keys


def test_one_complete_cell_does_not_cover_sibling_duty() -> None:
    evidence, group = _table()
    result = _coverage(evidence, group, _claims(evidence[4]))
    assert result.source_complete
    assert not result.complete
    assert len(result.missing_member_keys) == 1


def test_complete_other_role_list_cannot_fill_target_duty() -> None:
    evidence, group = _table()
    result = _coverage(evidence, group, _claims(evidence[-1]))
    assert not result.complete
    assert len(result.missing_member_keys) == 2


def test_missing_sent_member_stays_in_required_stable_set() -> None:
    evidence, group = _table()
    full = _coverage(evidence, group, _claims(*evidence[4:6]))
    result = _coverage(evidence[:5], group, _claims(evidence[4]))
    assert not result.source_complete
    assert not result.complete
    assert result.required_member_keys == full.required_member_keys


def test_same_role_different_stage_cannot_establish_target_set() -> None:
    evidence, group = _table()
    result = _coverage(evidence, group, _claims(*evidence), stage="研发阶段")
    assert not result.required_member_keys
    assert not result.complete


def test_physical_complete_without_canonical_header_is_not_task_complete() -> (
    None
):
    evidence, group = _table()
    result = _coverage(
        evidence, group.model_copy(update={"metadata": ()}), _claims(*evidence)
    )
    assert group.complete
    assert not result.complete
    assert result.reason_codes == ("TARGET_MEMBER_SET_UNPROVED",)


def test_full_quote_does_not_disguise_missing_answer_fact() -> None:
    evidence, group = _table()
    claims = (
        AnswerClaim(
            text="负责设计与开发。",
            supports=tuple(
                ClaimSupport(
                    support_id=item.support_id, quote=item.citation_text
                )
                for item in evidence[4:6]
            ),
        ),
    )
    result = _coverage(evidence, group, claims)
    assert not result.complete
    assert len(result.missing_member_keys) == 1


def test_support_alias_changes_do_not_change_required_members() -> None:
    evidence, group = _table()
    before = _coverage(evidence, group, _claims(*evidence[4:6]))
    renamed = tuple(
        item.model_copy(update={"evidence_id": f"S{99 - index}"})
        for index, item in enumerate(evidence)
    )
    after = _coverage(renamed, group, _claims(*renamed[4:6]))
    assert after.complete
    assert before.required_member_keys == after.required_member_keys


def test_all_target_groups_required_not_first_complete_group_only() -> None:
    evidence, group = _table()
    groups = tuple(
        trusted_list_group(
            (*evidence[:4], fact), group_id=f"egrp_{n:032x}"
        ).model_copy(
            update={
                "kind": group.kind,
                "heading_path": group.heading_path,
                "metadata": group.metadata,
            }
        )
        for n, fact in enumerate(evidence[4:6], start=1)
    )
    # 两个独立闭合组都属于同一任务，第一组完整不能遮蔽第二组缺项。
    result = target_member_coverage(
        QueryAtom(
            atom_id="A1",
            target="开发团队",
            relation="职责",
            answer_shape=AtomAnswerShape.DUTIES,
        ),
        evidence,
        _claims(evidence[4]),
        trusted_groups=groups,
        semantics=QuerySemantics(target="开发团队", relation="职责"),
        fact_covered=_source_fact_content_covered,
    )
    assert not result.complete
    assert len(result.required_member_keys) == 2


def test_unrelated_deeper_role_heading_does_not_inherit_parent_role() -> None:
    evidence, group = _table()
    group = group.model_copy(
        update={
            "kind": EvidenceGroupKind.LIST_GROUP,
            "heading_path": ("首单交付阶段", "开发团队", "运营团队职责"),
        }
    )
    assert not _coverage(evidence, group, _claims(*evidence)).complete


def test_canonical_header_nodes_require_explicit_source_mapping() -> None:
    header = _table_row(1, 0, ("岗位", "职责"), header=True)
    row = _table_row(2, 1, ("开发团队", "负责开发"), header=False)
    chunk = header.hydrated.chunk
    nodes = [span.node_id for span in chunk.source_spans if span.is_citable]
    metadata = {
        **dict(chunk.metadata),
        "atoms": [
            {
                "metadata": {
                    "table_node_id": "node_" + "a" * 32,
                    "row_index": 0,
                    "header_strategy": "tblHeader",
                    "cell_source_node_ids": {
                        str(i): [node] for i, node in enumerate(nodes)
                    },
                }
            }
        ],
    }
    canonical = _with_chunk(header, metadata=freeze_json_object(metadata))
    assert (
        dict(_groups(canonical, row)[0].group.metadata)[
            "canonical_header_node_ids"
        ]
        == nodes
    )
    assert (
        dict(_groups(header, row)[0].group.metadata)[
            "canonical_header_node_ids"
        ]
        == []
    )


def test_complete_requested_table_column_ignores_unasked_columns() -> None:
    evidence, group = _table()
    atom = QueryAtom(
        atom_id="A1",
        target="开发团队",
        relation="交付成果",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    result = target_member_coverage(
        atom,
        evidence,
        _claims(evidence[6]),
        trusted_groups=(group,),
        semantics=QuerySemantics(target=atom.target, relation=atom.relation),
        fact_covered=_source_fact_content_covered,
    )
    assert result.complete
    assert len(result.required_member_keys) == 1


def _input_table() -> tuple[tuple[EvidenceItem, ...], EvidenceGroup]:
    """把现有物理表格夹具转为短行名与短列名对照样本。"""
    specs = (
        (0, 0, "模式"),
        (0, 1, "输入（业务团队 / 外部单位需提供）"),
        (0, 2, "输出"),
        (1, 0, "需求快验"),
        (1, 1, "需求功能点描述。"),
        (1, 1, "验收标准。"),
        (1, 2, "快验结论。"),
        (2, 0, "其他模式"),
        (2, 1, "其他输入。"),
    )
    by_text = {
        item.citation_text: item
        for item in EvidenceAssembler().assemble(
            _candidates("".join(_paragraph(text) for _, _, text in specs)),
            _POLICY.model_copy(
                update={
                    "max_evidence_items_per_chunk": 16,
                    "max_evidence_items": 16,
                    "per_document_cap": 16,
                    "per_section_cap": 16,
                }
            ),
        )
    }
    evidence = tuple(
        _table_cell(by_text[text], row, column) for row, column, text in specs
    )
    group = trusted_list_group(evidence).model_copy(
        update={
            "kind": EvidenceGroupKind.TABLE_ROW_GROUP,
            "heading_path": ("协同方案",),
            "metadata": freeze_json_object(
                {
                    "canonical_header_node_ids": [
                        item.source_spans[0].node_id for item in evidence[:3]
                    ]
                }
            ),
        }
    )
    return evidence, group


def test_explicit_row_and_column_words_prove_short_table_axes() -> None:
    evidence, group = _input_table()
    question = "需求快验的“输入”项列了哪些内容？"
    atom = QueryAtom(
        atom_id="A1",
        target=question.rstrip("？"),
        relation="内容",
        answer_shape=AtomAnswerShape.FACT,
        original_fragment=question,
    )

    result = target_member_coverage(
        atom,
        evidence,
        _claims(*evidence[4:6]),
        trusted_groups=(group,),
        semantics=QuerySemantics(target=atom.target, relation=atom.relation),
        fact_covered=_source_fact_content_covered,
    )

    assert result.complete
    assert result.source_complete
    assert len(result.required_member_keys) == 2


def test_source_title_words_do_not_expand_requested_table_columns() -> None:
    evidence, group = _input_table()
    question = "《输出管理办法》中，需求快验的输入项是什么？"
    atom = QueryAtom(
        atom_id="A1",
        target="需求快验",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
        source_qualifier="输出管理办法",
        original_fragment=question,
    )

    result = target_member_coverage(
        atom,
        evidence,
        _claims(*evidence[4:6]),
        trusted_groups=(group,),
        semantics=QuerySemantics(target=atom.target, relation=atom.relation),
        fact_covered=_source_fact_content_covered,
    )

    assert result.complete
    assert len(result.required_member_keys) == 2


def test_unique_short_row_target_and_input_relation_are_complete() -> None:
    evidence, group = _input_table()
    question = "做快验前到底得备齐啥？"
    atom = QueryAtom(
        atom_id="A1",
        target="快验",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
        original_fragment=question,
    )

    result = target_member_coverage(
        atom,
        evidence,
        _claims(*evidence[4:6]),
        trusted_groups=(group,),
        semantics=QuerySemantics(target=atom.target, relation=atom.relation),
        fact_covered=_source_fact_content_covered,
    )

    assert result.complete
    assert len(result.required_member_keys) == 2


def test_query_only_timing_and_modality_do_not_complete_input_relation() -> (
    None
):
    evidence, group = _input_table()
    question = "需求快验之前必须提供什么？"
    atom = QueryAtom(
        atom_id="A1",
        target="需求快验",
        relation="提供",
        answer_shape=AtomAnswerShape.ENUMERATION,
        original_fragment=question,
    )

    result = target_member_coverage(
        atom,
        evidence,
        _claims(*evidence[4:6]),
        trusted_groups=(group,),
        semantics=QuerySemantics(target=atom.target, relation=atom.relation),
        fact_covered=_source_fact_content_covered,
    )

    assert not result.complete
    assert result.reason_codes == ("TARGET_MEMBER_SET_UNPROVED",)
