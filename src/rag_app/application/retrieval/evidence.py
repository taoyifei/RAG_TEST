"""Canonical source-span 去重、来源多样性和预算 packing。"""

from __future__ import annotations

from collections import Counter

from rag_app.core.models import (
    Chunk,
    EvidenceItem,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.chunk import SourceSpan, SourceSpanKind


class EvidenceAssembler:
    """只发布可映射到真实来源的单 span quote。"""

    def assemble(
        self,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
    ) -> tuple[EvidenceItem, ...]:
        """依次执行 chunk/span dedup、cap、多样性和 token packing。

        Args:
            candidates: canonical hydrated 候选和结构扩展。
            policy: P07 evidence cap 与 token 预算。

        Returns:
            仅含单一可发布 span quote 的 EvidenceItem 序列。

        """
        unique_chunks = tuple(
            {item.hydrated.chunk.chunk_id: item for item in candidates}.values()
        )
        ordered = _diverse_order(unique_chunks)
        documents: Counter[str] = Counter()
        sections: Counter[tuple[str, str]] = Counter()
        used_spans: set[tuple[object, ...]] = set()
        remaining = policy.evidence_token_budget
        evidence: list[EvidenceItem] = []
        for candidate in ordered:
            chunk = candidate.hydrated.chunk
            document_id = chunk.version.document_id
            section_key = (document_id, chunk.section_id)
            for span, quote, span_key in _unique_citable_spans(
                chunk, used_spans
            ):
                if documents[document_id] >= policy.per_document_cap:
                    break
                if sections[section_key] >= policy.per_section_cap:
                    break
                estimated_tokens = max(1, (len(quote) + 3) // 4)
                if estimated_tokens > remaining:
                    continue
                remaining -= estimated_tokens
                used_spans.add(span_key)
                documents[document_id] += 1
                sections[section_key] += 1
                support_id = f"S{len(evidence) + 1}"
                evidence.append(
                    _evidence_item(candidate, span, quote, support_id)
                )
        return tuple(evidence)


def _diverse_order(
    candidates: tuple[RankedChunk, ...],
) -> tuple[RankedChunk, ...]:
    first: list[RankedChunk] = []
    rest: list[RankedChunk] = []
    seen_documents: set[str] = set()
    for candidate in candidates:
        document_id = candidate.hydrated.chunk.version.document_id
        target = first if document_id not in seen_documents else rest
        target.append(candidate)
        seen_documents.add(document_id)
    return tuple(first + rest)


def _unique_citable_spans(
    chunk: Chunk,
    used: set[tuple[object, ...]],
) -> tuple[tuple[SourceSpan, str, tuple[object, ...]], ...]:
    selected: list[tuple[SourceSpan, str, tuple[object, ...]]] = []
    for span in chunk.source_spans:
        if not span.is_citable or span.span_type is SourceSpanKind.SEPARATOR:
            continue
        key = (
            chunk.version.document_version_id,
            span.node_id,
            span.source_start_char,
            span.source_end_char,
            span.span_type.value,
        )
        if key in used:
            continue
        quote = chunk.citation_text[span.chunk_start_char : span.chunk_end_char]
        if quote.strip():
            selected.append((span, quote, key))
    return tuple(selected)


def _evidence_item(
    candidate: RankedChunk,
    span: SourceSpan,
    quote: str,
    support_id: str,
) -> EvidenceItem:
    chunk = candidate.hydrated.chunk
    return EvidenceItem(
        evidence_id=support_id,
        chunk_id=chunk.chunk_id,
        citation_text=quote,
        source_label=_source_label(
            candidate.hydrated.display_name, chunk.heading_path
        ),
        source_spans=(_relative_span(span, len(quote)),),
        document_id=chunk.version.document_id,
        document_version_id=chunk.version.document_version_id,
        display_name=candidate.hydrated.display_name,
        heading_path=chunk.heading_path,
        section_id=chunk.section_id,
        table_locator=(
            chunk.neighbor_group_id if chunk.role.value == "table" else None
        ),
        retrieval_origins=tuple(
            contribution.channel for contribution in candidate.contributions
        )
        + (
            (candidate.expansion_reason,)
            if candidate.expansion_reason
            else ()
        ),
        fusion_rank=candidate.fusion_rank,
        rerank_rank=candidate.rerank_rank,
        quality_flags=(
            ("METADATA_ONLY",)
            if chunk.role.value in {"image_metadata", "header_footer"}
            else ()
        ),
    )


def _relative_span(span: SourceSpan, length: int) -> SourceSpan:
    return span.model_copy(
        update={"chunk_start_char": 0, "chunk_end_char": length}
    )


def _source_label(display_name: str, headings: tuple[str, ...]) -> str:
    return (
        f"{display_name} · {' / '.join(headings)}" if headings else display_name
    )


__all__ = ["EvidenceAssembler"]
