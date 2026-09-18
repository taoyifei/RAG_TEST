"""结构组完整性以真实来源跨度为准。"""

from __future__ import annotations

from rag_app.application.retrieval.evidence import (
    _annotate_group_evidence,
    group_source_maps_covered,
)
from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.core.models import EvidenceGroup, EvidenceGroupKind, GroupSourceMap
from tests.application.answering.test_natural_grounded_answer import _evidence
from tests.application.retrieval.test_descriptive_answers import (
    _candidates,
    _paragraph,
)


def test_one_selected_span_does_not_close_multi_fact_chunk_group() -> None:
    sentences = ("甲类文具包括：", "钢笔。", "纸张。")
    text = "".join(_paragraph(sentence) for sentence in sentences)
    ranked = _candidates(text)[0]
    chunk = ranked.hydrated.chunk
    group = GroupCandidate(
        group=EvidenceGroup(
            group_id="egrp_" + "3" * 32,
            kind=EvidenceGroupKind.LIST_GROUP,
            document_id=chunk.version.document_id,
            document_version_id=chunk.version.document_version_id,
            index_revision_id=chunk.index_revision_id,
            section_id=chunk.section_id,
            display_name="合成资料.docx",
            heading_path=chunk.heading_path,
            member_chunk_ids=(chunk.chunk_id,),
            member_source_maps=(
                GroupSourceMap(
                    chunk_id=chunk.chunk_id,
                    citation_text=chunk.citation_text,
                    source_spans=chunk.source_spans,
                ),
            ),
            member_ranks=(1,),
            group_text_for_model=chunk.citation_text,
            complete=True,
            token_cost=chunk.token_count,
        ),
        members=(ranked,),
        rerank_text=chunk.citation_text,
    )
    evidence = _evidence(*sentences)
    assert len(evidence) == 3

    incomplete = _annotate_group_evidence(evidence[:1], (group,))
    complete = _annotate_group_evidence(evidence, (group,))

    assert dict(incomplete[0].metadata)["group_complete"] is False
    assert "EVIDENCE_SOURCE_SPAN_NOT_SELECTED" in dict(
        incomplete[0].metadata
    )["group_completeness_reason"]
    assert all(
        dict(item.metadata)["group_complete"] is True for item in complete
    )
    assert not group_source_maps_covered(group, incomplete)
    assert group_source_maps_covered(group, complete)
