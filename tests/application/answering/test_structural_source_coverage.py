"""同一 Chunk 中的多条事实必须逐 SourceSpan 覆盖。"""

from __future__ import annotations

import pytest

from rag_app.application.answering.grounded import (
    _structural_member_coverage,
    _validated_natural_claim,
)
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import AnswerClaim, ClaimSupport
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
)
from rag_app.core.models.retrieval import NaturalClaim
from tests.application.answering.source_contract_fixtures import (
    trusted_list_group,
)
from tests.application.answering.test_natural_grounded_answer import (
    _evidence,
    _plan,
)


def test_one_cited_fact_does_not_cover_sibling_fact_in_same_chunk() -> None:
    items = _evidence("甲类文具包括：", "钢笔。", "纸张。")
    assert len({item.chunk_id for item in items}) == 1
    grouped = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": "egrp_" + "1" * 32,
                        "group_complete": True,
                        "group_member_index": 1,
                        "group_member_count": 1,
                    }
                )
            }
        )
        for item in items
    )
    fact_ids = tuple(
        item.support_id
        for item in grouped
        if item.citation_text != "甲类文具包括："
    )
    first = next(item for item in grouped if item.support_id == fact_ids[0])
    claim = AnswerClaim(
        text="甲类文具包括钢笔。",
        supports=(
            ClaimSupport(
                support_id=first.support_id, quote=first.citation_text
            ),
        ),
    )

    assert _structural_member_coverage(grouped, (claim,)) is False


def test_group_certificate_requires_lead_in_in_claim_citations() -> None:
    evidence = _evidence("甲类文具包括：", "钢笔。", "纸张。")
    group_id = "egrp_" + "2" * 32
    grouped = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": group_id,
                        "group_complete": True,
                        "group_member_index": 1
                        if item.citation_text == "甲类文具包括："
                        else 2,
                        "group_member_count": 2,
                    }
                )
            }
        )
        for item in evidence
    )
    plan = _plan("甲类文具", shape=AtomAnswerShape.ENUMERATION)
    matrix = AtomSupportMatrix(
        atoms=(
            AtomSupport(
                atom_id="A1",
                status=AtomStatus.SUPPORTED,
                supporting_group_ids=(group_id,),
                supporting_support_ids=tuple(
                    item.support_id for item in grouped
                ),
                relation_certified_group_ids=(group_id,),
            ),
        )
    )
    fact = next(item for item in grouped if item.citation_text == "钢笔。")

    with pytest.raises(ValidationFailed) as failure:
        _validated_natural_claim(
            NaturalClaim(
                atom_id="A1",
                text="甲类文具包括钢笔。",
                supports=(
                    ClaimSupport(
                        support_id=fact.support_id,
                        quote=fact.citation_text,
                    ),
                ),
            ),
            plan,
            matrix,
            grouped,
            None,
        )

    # 没有受信组时，矩阵里的组 ID 不能替代缺失导语；本层先按事实文本
    # 不受来源支持拒绝，服务层才可恢复原句或进入有界语义复核。
    assert failure.value.code == "CLAIM_TEXT_UNSUPPORTED"

    lead = next(
        item for item in grouped if item.citation_text == "甲类文具包括："
    )
    validated = _validated_natural_claim(
        NaturalClaim(
            atom_id="A1",
            text="甲类文具包括钢笔。",
            supports=tuple(
                ClaimSupport(
                    support_id=item.support_id,
                    quote=item.citation_text,
                )
                for item in (lead, fact)
            ),
        ),
        plan,
        matrix,
        grouped,
        None,
        trusted_groups=(trusted_list_group(grouped, group_id=group_id),),
    )
    assert tuple(item.support_id for item in validated.supports) == (
        lead.support_id,
        fact.support_id,
    )
