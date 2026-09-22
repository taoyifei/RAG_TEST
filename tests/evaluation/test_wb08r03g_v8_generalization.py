"""冻结合成 holdout 的完整来源校验；不表示真实模型或召回质量。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NotRequired, TypedDict
from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import _validated_natural_claim
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    EvidenceItem,
    RequestedAnswerType,
    RetrievalPolicy,
)
from rag_app.core.models.document import StoryKind
from rag_app.core.models.query_plan import AtomStatus, QueryPlan
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
from tests.application.answering.source_contract_fixtures import (
    contiguous_node_fragments,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _answer_with_pack,
    _table_cell,
)
from tests.application.answering.test_natural_grounded_answer import (
    _draft,
    _evidence,
    _matrix,
    _plan,
)
from tests.application.retrieval.test_evidence_table_coordinates import (
    _context,
    _table,
)


class _Case(TypedDict):
    id: str
    category: str
    target: str
    query: str
    source: str
    claim: str
    valid: bool
    split_after: NotRequired[str]
    structure: NotRequired[str]
    source_context: NotRequired[str]
    requires_citation: NotRequired[bool]


_DATASET = (
    Path(__file__).resolve().parents[2]
    / "evaluation/wanshitong/v2/wb08r03g-v8-generalization.json"
)
_CASES: tuple[_Case, ...] = tuple(
    json.loads(_DATASET.read_text(encoding="utf-8"))["cases"]
)


def _case_plan(case: _Case) -> QueryPlan:
    plan = _plan(case["target"])
    return plan.model_copy(
        update={
            "standalone_query": case["query"],
            "original_query": case["query"],
            "resolved_root_query": case["query"],
            "atoms": (
                plan.atoms[0].model_copy(
                    update={
                        "relation": "职责",
                        "original_fragment": case["query"],
                    }
                ),
            ),
        }
    )


def _intersection(case: _Case) -> tuple[EvidenceItem, ...]:
    """从真实行、列头和值的合成 SourceMap 经旧 assembler 取得交点证书。"""
    candidate = _table(
        rows=(("角色", "职责"), (case["target"], case["source"]))
    )
    context = _context(case["query"])
    context = context.model_copy(
        update={
            "analysis": context.analysis.model_copy(
                update={
                    "semantics": context.analysis.semantics.model_copy(
                        update={
                            "target": case["target"],
                            "relation": "职责",
                            "answer_type": RequestedAnswerType.FACT,
                            "source": "SPAN_REFERENCED",
                        }
                    ),
                }
            ),
            "include_table_context": True,
        }
    )
    evidence = (
        EvidenceAssembler()
        .assemble_sets((candidate,), RetrievalPolicy(), context=context)
        .answer_support_set
    )
    assert {item.citation_text for item in evidence} == {
        case["target"],
        "职责",
        case["source"],
    }
    assert all(
        dict(item.metadata)["answer_support"]["support_reason"]
        == "TABLE_INTERSECTION"
        for item in evidence
    )
    return evidence


def _case_evidence(case: _Case) -> tuple[EvidenceItem, ...]:
    if case.get("structure") == "certified_row_header_value":
        return _intersection(case)
    prefix = case.get("split_after")
    if prefix is None:
        return _evidence(case["source"])
    assert case["source"].startswith(prefix)
    pieces = _evidence(prefix, case["source"][len(prefix) :])
    assert len(pieces) == 2
    if case.get("structure") == "sibling_rows":
        return tuple(
            _table_cell(item, index, 1) for index, item in enumerate(pieces, 1)
        )
    pieces = contiguous_node_fragments(
        tuple(_table_cell(item, 1, 1) for item in pieces)
    )
    second = pieces[1].model_copy(update={"chunk_id": "chunk_" + "f" * 32})
    if case.get("structure") == "different_story":
        span = second.source_spans[0]
        second = second.model_copy(
            update={
                "source_spans": (
                    span.model_copy(
                        update={
                            "source_anchor": span.source_anchor.model_copy(
                                update={
                                    "story_kind": StoryKind.HEADER,
                                    "part_uri": "word/header1.xml",
                                }
                            )
                        }
                    ),
                )
            }
        )
    return pieces[0], second


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_frozen_generalization_source_contract(case: _Case) -> None:
    """逐一执行冻结事实；负例必须由真实 validator 拒绝，不能改题消错。"""
    evidence = _case_evidence(case)
    plan = _case_plan(case)
    ids = tuple(item.support_id for item in evidence)
    matrix = _matrix(plan, ((AtomStatus.MISSING, ()),))
    claim = NaturalClaim(
        atom_id="A1",
        text=case["claim"],
        supports=tuple(
            ClaimSupport(support_id=item.support_id, quote=item.citation_text)
            for item in evidence
        ),
    )
    if not case["valid"]:
        with pytest.raises(ValidationFailed):
            _validated_natural_claim(claim, plan, matrix, evidence, None)
        return
    validated = _validated_natural_claim(claim, plan, matrix, evidence, None)
    assert validated.text == case["claim"]
    assert tuple(support.support_id for support in validated.supports) == ids
    generator = Mock()
    generator.generate.return_value = _draft((claim,), plan)
    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )
    assert outcome.answer is not None
    assert outcome.accepted_claim_count == 1
    assert case["claim"] in outcome.answer
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert all(f"[{support_id}]" in outcome.answer for support_id in ids)
    assert generator.generate.call_count == 1
