"""为来源合同测试构造独立真实成员映射，不以 metadata 字符串认证。"""

from __future__ import annotations

from collections.abc import Callable

from rag_app.core.models import EvidenceItem, SourceSpan
from rag_app.core.models.evidence_group import (
    EvidenceGroup,
    EvidenceGroupKind,
    GroupSourceMap,
)


def contiguous_node_fragments(
    evidence: tuple[EvidenceItem, ...],
    include: Callable[[EvidenceItem], bool] = lambda _item: True,
) -> tuple[EvidenceItem, ...]:
    """构造真正连续的节点分块，不能只改 node_id 而保留重叠错位范围。"""
    members = sorted(
        (item for item in evidence if include(item)),
        key=lambda item: min(
            span.source_anchor.ordinal
            for span in item.source_spans
            if span.source_anchor is not None
        ),
    )
    source_span = members[0].source_spans[0]
    assert source_span.source_anchor is not None
    length = sum(len(item.citation_text) for item in members)
    anchor = source_span.source_anchor.model_copy(
        update={"source_start_char": 0, "source_end_char": length}
    )
    updated: dict[str, EvidenceItem] = {}
    offset = 0
    for item in members:
        size = len(item.citation_text)
        updated[item.support_id] = item.model_copy(
            update={
                "source_spans": (
                    item.source_spans[0].model_copy(
                        update={
                            "node_id": source_span.node_id,
                            "source_anchor": anchor,
                            "structural_path": source_span.structural_path,
                            "source_start_char": offset,
                            "source_end_char": offset + size,
                            "chunk_start_char": 0,
                            "chunk_end_char": size,
                        }
                    ),
                )
            }
        )
        offset += size
    return tuple(updated.get(item.support_id, item) for item in evidence)


def trusted_list_group(
    evidence: tuple[EvidenceItem, ...],
    *,
    group_id: str = "egrp_" + "2" * 32,
) -> EvidenceGroup:
    """按真实原文节点映射合成组，允许一 Chunk 包含多个事实跨度。"""
    maps: list[GroupSourceMap] = []
    for chunk_id in dict.fromkeys(item.chunk_id for item in evidence):
        members = tuple(item for item in evidence if item.chunk_id == chunk_id)
        text = ""
        spans: list[SourceSpan] = []
        for item in members:
            if text:
                text += "\n"
            offset = len(text)
            spans.extend(
                span.model_copy(
                    update={
                        "chunk_start_char": span.chunk_start_char + offset,
                        "chunk_end_char": span.chunk_end_char + offset,
                    }
                )
                for span in item.source_spans
            )
            text += item.citation_text
        maps.append(
            GroupSourceMap(
                chunk_id=chunk_id,
                citation_text=text,
                source_spans=tuple(spans),
            )
        )
    first = evidence[0]
    assert first.document_id is not None
    assert first.document_version_id is not None
    return EvidenceGroup(
        group_id=group_id,
        kind=EvidenceGroupKind.LIST_GROUP,
        document_id=first.document_id,
        document_version_id=first.document_version_id,
        index_revision_id="irev_" + "1" * 32,
        section_id=first.section_id or "synthetic-section",
        display_name=first.display_name or "合成资料",
        member_chunk_ids=tuple(member.chunk_id for member in maps),
        member_source_maps=tuple(maps),
        member_ranks=tuple(range(1, len(maps) + 1)),
        complete=True,
        token_cost=sum(len(item.citation_text) for item in evidence),
    )
