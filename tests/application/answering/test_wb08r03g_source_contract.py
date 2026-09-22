"""V8 来源连续性、原句边界和受控恢复的固定服务回放。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import (
    _complete_source_sentence,
    _validate_natural_entailment,
    _validate_natural_support_structure,
    _validated_natural_claim,
)
from rag_app.core.errors import ValidationFailed
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import AnswerClaim, EvidenceItem, SourceSpanKind
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    SourceContentRequirement,
    SourceDocumentIdentity,
    SourceIntent,
    SourceResolution,
    SourceScopeDecision,
)
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
from rag_app.core.source_compatibility import (
    source_compatibility,
    source_group_contains,
    source_group_covered,
)
from tests.application.answering.source_contract_fixtures import (
    trusted_list_group,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _answer_with_pack,
    _table_cell,
)
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _draft,
    _evidence,
    _matrix,
    _plan,
)


def _fragments() -> tuple[EvidenceItem, EvidenceItem]:
    """同一个真实单元格中的两个连续原文片段。"""
    first_text = "甲部门负责交付，"
    second_text = "并负责巡检。"
    by_text = {
        item.citation_text: _table_cell(item, 1, 1)
        for item in _evidence(first_text, second_text)
    }
    first = by_text[first_text]
    second = by_text[second_text]
    first_span = first.source_spans[0]
    span = second.source_spans[0]
    second = second.model_copy(
        update={
            "source_spans": (
                span.model_copy(
                    update={
                        "node_id": first_span.node_id,
                        "source_start_char": len(first_text),
                        "source_end_char": len(first_text) + len(second_text),
                    }
                ),
            )
        }
    )
    return first, second


@pytest.mark.parametrize("ending", ["。", "；", "。\n", "。   ", "。\u201d"])
def test_sentence_already_terminated_does_not_swallow_next(ending: str) -> None:
    quote = f"甲部门负责交付{ending}"
    source = f"{quote}乙部门负责巡检。"

    assert _complete_source_sentence(source, quote) == quote.strip()


def test_recovery_is_idempotent() -> None:
    source = "甲部门负责交付。乙部门负责巡检。"
    restored = _complete_source_sentence(source, "负责交付")

    assert restored == "甲部门负责交付。"
    assert _complete_source_sentence(source, restored) == restored


def test_ambiguous_repeated_quote_requires_position_proof() -> None:
    source = "完成审批后甲部门负责交付。无需审批时甲部门负责交付。"

    with pytest.raises(ValidationFailed) as failure:
        _complete_source_sentence(source, "甲部门负责交付")

    assert failure.value.code == "CLAIM_QUOTE_AMBIGUOUS"


def test_missing_quote_cannot_be_recovered_as_source() -> None:
    with pytest.raises(ValidationFailed) as failure:
        _complete_source_sentence("甲部门负责交付。", "乙部门负责交付。")

    assert failure.value.code == "CLAIM_QUOTE_INVALID"


def test_forged_group_id_cannot_join_sibling_data_rows() -> None:
    evidence = tuple(
        _table_cell(item, row, 1).model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        "evidence_group_id": "egrp_" + "a" * 32,
                        "group_complete": True,
                    }
                )
            }
        )
        for item, row in zip(
            _evidence("甲部门负责交付。", "乙部门负责巡检。"),
            (1, 2),
            strict=True,
        )
    )

    with pytest.raises(ValidationFailed) as failure:
        _validate_natural_support_structure(evidence)

    assert failure.value.code == "CLAIM_SOURCE_MISMATCH"


def test_validated_claim_is_published_without_hidden_source_expansion() -> None:
    source = "甲部门负责交付。乙部门负责巡检。"
    evidence = _evidence(source)
    plan = _plan("甲部门")
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", "甲部门负责交付。", "A1", "S1"),), plan
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.answer == "甲部门负责交付。 [S1]"
    assert outcome.accepted_claim_count == 1
    assert generator.generate.call_count == 1


def test_contiguous_cell_claim_passes_complete_service() -> None:
    evidence = _fragments()
    plan = _plan("甲部门")
    natural = NaturalClaim(
        atom_id="A1",
        text="甲部门负责交付，并负责巡检。",
        supports=tuple(
            ClaimSupport(support_id=item.support_id, quote=item.citation_text)
            for item in evidence
        ),
    )
    generator = Mock()
    generator.generate.return_value = _draft((natural,), plan)

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.accepted_claim_count == 1
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.claim_rejection_codes == ()
    assert outcome.repair_calls == 0


def test_zero_accepted_omission_can_use_one_local_repair() -> None:
    evidence = _evidence("甲部门负责交付。")
    plan = _plan("甲部门")
    generator = Mock()
    generator.generate.side_effect = (
        _draft((), plan),
        _draft((_claim("C1", evidence[0].citation_text, "A1", "S1"),), plan),
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert generator.generate.call_count == 2
    assert generator.generate.call_args.args[0].repair_atom_ids == ("A1",)
    assert outcome.repair_calls == 1
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)


@pytest.mark.parametrize(
    ("source", "text"),
    [
        (
            "首次交付阶段，甲部门负责上线。后续运维阶段，甲部门负责巡检。",
            "首次交付阶段，甲部门负责巡检。",
        ),
        ("甲部门在审批通过后负责交付。", "甲部门负责交付。"),
        ("甲部门在材料齐全时可以检查材料。", "甲部门可以检查材料。"),
    ],
)
def test_scope_and_condition_remain_bound_to_their_actions(
    source: str, text: str
) -> None:
    with pytest.raises(ValidationFailed) as failure:
        _validate_natural_entailment(
            AnswerClaim(
                text=text,
                supports=(ClaimSupport(support_id="S1", quote=source),),
            )
        )

    assert failure.value.code in {
        "CLAIM_STAGE_UNSUPPORTED",
        "CLAIM_CONDITION_UNSUPPORTED",
    }


@pytest.mark.parametrize("suffix", ["组", "中心", "实验室"])
def test_explicit_organization_owner_cannot_be_replaced(suffix: str) -> None:
    evidence = _evidence(f"甲{suffix}负责设备巡检。")
    plan = _plan(f"乙{suffix}")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    natural = NaturalClaim(
        atom_id="A1",
        text=f"乙{suffix}负责设备巡检。",
        supports=(
            ClaimSupport(support_id="S1", quote=evidence[0].citation_text),
        ),
    )

    with pytest.raises(ValidationFailed) as failure:
        _validated_natural_claim(natural, plan, matrix, evidence, None)

    assert failure.value.code in {
        "CLAIM_TARGET_UNSUPPORTED",
        "CLAIM_OBJECT_CHANGED",
    }


@pytest.mark.parametrize("marker", ("", "- ", "• "))
def test_post_subject_context_cannot_hide_organization_replacement(
    marker: str,
) -> None:
    evidence = _evidence("甲机构在该项工作中负责设备巡检。")
    plan = _plan("乙组")
    plan = plan.model_copy(
        update={
            "atoms": (
                plan.atoms[0].model_copy(
                    update={
                        "relation": "负责",
                        "answer_shape": AtomAnswerShape.DUTIES,
                        "original_fragment": "乙组在该项工作中负责什么？",
                    }
                ),
            )
        }
    )
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    natural = NaturalClaim(
        atom_id="A1",
        text=f"{marker}乙组在该项工作中负责设备巡检。",
        supports=(
            ClaimSupport(support_id="S1", quote=evidence[0].citation_text),
        ),
    )

    with pytest.raises(ValidationFailed) as failure:
        _validated_natural_claim(natural, plan, matrix, evidence, None)

    assert failure.value.code in {
        "CLAIM_TARGET_UNSUPPORTED",
        "CLAIM_OBJECT_CHANGED",
    }


def test_generic_team_suffix_alias_keeps_same_role_owner() -> None:
    evidence = _evidence("甲团队在该项工作中负责设备巡检。")
    plan = _plan("甲组")
    plan = plan.model_copy(
        update={
            "atoms": (
                plan.atoms[0].model_copy(
                    update={
                        "relation": "负责",
                        "answer_shape": AtomAnswerShape.DUTIES,
                        "original_fragment": "甲组在该项工作中负责什么？",
                    }
                ),
            )
        }
    )
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    natural = NaturalClaim(
        atom_id="A1",
        text="甲组在该项工作中负责设备巡检。",
        supports=(
            ClaimSupport(support_id="S1", quote=evidence[0].citation_text),
        ),
    )

    validated = _validated_natural_claim(natural, plan, matrix, evidence, None)

    assert validated.text == natural.text


def test_legacy_and_direct_validation_reject_wrong_document_identity() -> None:
    """旧协议与服务端摘录也必须服从同一精确文档身份合同。"""
    evidence = _evidence("另一份方案中的真实要求。")
    plan = _plan("评审会议纪要")
    scope = SourceScopeDecision(
        atom_id="A1",
        source_intent=SourceIntent.DOCUMENT_AUTHORITY,
        resolution=SourceResolution.RESOLVED,
        allowed_documents=(
            SourceDocumentIdentity(
                document_id=f"doc_{'e' * 32}",
                document_version_id=f"dver_{'f' * 32}",
            ),
        ),
        required_content=SourceContentRequirement.BODY,
        mention_sha256=canonical_sha256("指定会议纪要模板"),
        registry_revision="test-registry-v1",
        scope_digest=canonical_sha256("source-scope"),
    )
    plan = plan.model_copy(
        update={
            "atoms": (plan.atoms[0].model_copy(update={"source_scope": scope}),)
        }
    )
    natural = NaturalClaim(
        atom_id="A1",
        text=evidence[0].citation_text,
        supports=(
            ClaimSupport(
                support_id=evidence[0].support_id,
                quote=evidence[0].citation_text,
            ),
        ),
    )

    with pytest.raises(ValidationFailed) as failure:
        _validated_natural_claim(
            natural,
            plan,
            _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),)),
            evidence,
            None,
        )

    assert failure.value.code == "CLAIM_DOCUMENT_SCOPE_MISMATCH"


def test_real_derived_list_marker_is_group_member_without_fake_offsets() -> (
    None
):
    item = _evidence("（一）")[0]
    marker = item.model_copy(
        update={
            "source_spans": (
                item.source_spans[0].model_copy(
                    update={
                        "span_type": SourceSpanKind.DERIVED_NUMBERING,
                        "source_start_char": None,
                        "source_end_char": None,
                    }
                ),
            )
        }
    )
    group = trusted_list_group((marker,))

    assert source_group_contains(marker, group)
    assert source_group_covered((marker,), group)


@pytest.mark.parametrize(
    "change",
    [
        "document",
        "version",
        "part",
        "story",
        "table",
        "row",
        "node",
        "gap",
        "bool",
        "missing_anchor",
        "missing_coordinate",
        "bad_overlap",
    ],
)
def test_continuity_rejects_missing_or_conflicting_source_proof(
    change: str,
) -> None:
    first, second = _fragments()
    span = second.source_spans[0]
    anchor = span.source_anchor
    assert anchor is not None
    item_changes: dict[str, object] = {}
    span_changes: dict[str, object] = {}
    anchor_changes: dict[str, object] = {}
    if change == "document":
        item_changes["document_id"] = "doc_" + "f" * 32
    elif change == "version":
        item_changes["document_version_id"] = "dver_" + "f" * 32
    elif change == "part":
        anchor_changes["part_uri"] = "word/header1.xml"
    elif change == "story":
        anchor_changes["story_kind"] = "header"
    elif change in {"table", "row", "missing_coordinate"}:
        path = (
            tuple(
                "tbl:2"
                if change == "table" and value.startswith("tbl:")
                else "tr:2"
                if change == "row" and value.startswith("tr:")
                else value
                for value in span.structural_path
            )
            if change != "missing_coordinate"
            else ("body", "p:1")
        )
        span_changes["structural_path"] = path
        anchor_changes["structural_path"] = path
    elif change == "node":
        span_changes["node_id"] = "node_" + "f" * 32
    elif change == "gap":
        assert span.source_start_char is not None
        assert span.source_end_char is not None
        span_changes["source_start_char"] = span.source_start_char + 1
        span_changes["source_end_char"] = span.source_end_char + 1
    elif change == "bool":
        span_changes["source_start_char"] = True
    elif change == "bad_overlap":
        span_changes["source_start_char"] = 0
        span_changes["source_end_char"] = len(second.citation_text)
    else:
        span_changes["source_anchor"] = None
    if anchor_changes:
        span_changes["source_anchor"] = anchor.model_copy(update=anchor_changes)
    item_changes["source_spans"] = (span.model_copy(update=span_changes),)
    changed = second.model_copy(update=item_changes)

    assert not source_compatibility((first, changed)).compatible


def test_contiguous_fragments_may_be_reordered_by_real_offsets() -> None:
    first, second = _fragments()
    decision = source_compatibility((second, first))

    assert decision.compatible
    assert decision.reason == "CONTIGUOUS_NODE"
    assert decision.ordered_support_ids == (first.support_id, second.support_id)


def test_overlap_requires_exact_shared_original_characters() -> None:
    first, second = _fragments()
    quote = first.citation_text[-3:] + second.citation_text
    span = second.source_spans[0].model_copy(
        update={
            "source_start_char": len(first.citation_text) - 3,
            "source_end_char": len(first.citation_text)
            + len(second.citation_text),
        }
    )
    second = second.model_copy(
        update={"citation_text": quote, "source_spans": (span,)}
    )

    assert source_compatibility((first, second)).compatible


def test_repeated_quote_uses_explicit_position() -> None:
    source = "完成审批后甲部门负责交付。无需审批时甲部门负责交付。"
    quote = "甲部门负责交付"
    assert (
        _complete_source_sentence(
            source, quote, quote_start=source.rfind(quote)
        )
        == "无需审批时甲部门负责交付。"
    )


def test_quote_recovery_keeps_closing_quote_without_next_sentence() -> None:
    source = "“甲部门负责交付。”乙部门负责巡检。"
    assert (
        _complete_source_sentence(source, "甲部门负责交付。")
        == "“甲部门负责交付。”"
    )


@pytest.mark.parametrize("request_scope_proved", [False, True])
def test_full_cell_coverage_and_requested_duties_coverage_are_separate(
    request_scope_proved: bool,
) -> None:
    fragments = _fragments()
    first_span = fragments[0].source_spans[0]
    assert first_span.source_anchor is not None
    anchor = first_span.source_anchor.model_copy(
        update={
            "source_start_char": 0,
            "source_end_char": sum(
                len(item.citation_text) for item in fragments
            ),
        }
    )
    certificate = (
        {
            "answer_support": {
                "status": "SUPPORTED",
                "query_target": "甲部门",
                "answer_type": "DUTIES",
                "support_reason": "TABLE_ROW_CONTENT",
                "supporting_span_ids": [first_span.node_id],
            }
        }
        if request_scope_proved
        else {}
    )
    evidence = tuple(
        item.model_copy(
            update={
                "source_spans": (
                    item.source_spans[0].model_copy(
                        update={"source_anchor": anchor}
                    ),
                ),
                "metadata": freeze_json_object(certificate),
            }
        )
        for item in fragments
    )
    plan = _plan("甲部门", shape=AtomAnswerShape.DUTIES)
    claim = NaturalClaim(
        atom_id="A1",
        text="甲部门负责交付，并负责巡检。",
        supports=tuple(
            ClaimSupport(support_id=item.support_id, quote=item.citation_text)
            for item in evidence
        ),
    )
    generator = Mock()
    generator.generate.return_value = _draft((claim,), plan)

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.accepted_claim_count == 1
    assert outcome.atom_coverage == (
        ("A1", "SUPPORTED" if request_scope_proved else "PARTIAL"),
    )
    assert outcome.repair_calls == (0 if request_scope_proved else 1)
