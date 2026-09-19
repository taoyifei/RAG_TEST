"""生成发送合同的合成来源组 fixtures，不含真实题或文档。"""

from __future__ import annotations

from collections import defaultdict

from rag_app.core.models import (
    EvidenceGroup,
    EvidenceGroupKind,
    EvidenceItem,
    GroupSourceMap,
    SourceSpan,
)


def trusted_groups(
    evidence: tuple[EvidenceItem, ...],
) -> tuple[EvidenceGroup, ...]:
    """为合成证据生成独立真实映射，测试不再只相信复制的 group 字符串。"""
    grouped: dict[str, list[EvidenceItem]] = defaultdict(list)
    for item in evidence:
        group_id = dict(item.metadata).get("evidence_group_id")
        if isinstance(group_id, str):
            grouped[group_id].append(item)
    result = []
    for group_id, items in grouped.items():
        by_chunk: dict[str, list[EvidenceItem]] = defaultdict(list)
        for item in items:
            by_chunk[item.chunk_id].append(item)
        source_maps = []
        for chunk_id, sources in by_chunk.items():
            text = ""
            spans: list[SourceSpan] = []
            for source in sources:
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
                    for span in source.source_spans
                )
                text += source.citation_text
            source_maps.append(
                GroupSourceMap(
                    chunk_id=chunk_id,
                    citation_text=text,
                    source_spans=tuple(spans),
                )
            )
        first = items[0]
        result.append(
            EvidenceGroup(
                group_id=group_id,
                kind=EvidenceGroupKind.PARAGRAPH_GROUP,
                document_id=first.document_id,
                document_version_id=first.document_version_id,
                index_revision_id="irev_" + "c" * 32,
                section_id=first.section_id or "synthetic-section",
                display_name="合成来源",
                member_chunk_ids=tuple(by_chunk),
                member_source_maps=tuple(source_maps),
                member_ranks=tuple(range(1, len(by_chunk) + 1)),
                complete=True,
                token_cost=sum(len(item.citation_text) for item in items),
            )
        )
    return tuple(result)
