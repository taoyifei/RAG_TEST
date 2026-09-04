"""Canonical source-span 去重、来源多样性和预算 packing。"""

from __future__ import annotations

import re
from collections import Counter

from rag_app.core.models import (
    Chunk,
    EvidenceItem,
    EvidenceSelectionContext,
    QueryKind,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.chunk import SourceSpan, SourceSpanKind

_RELATIVE_RELEVANCE_FLOOR = 0.98


class EvidenceAssembler:
    """只发布可映射到真实来源的单 span quote。"""

    def assemble(
        self,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
        *,
        context: EvidenceSelectionContext | None = None,
    ) -> tuple[EvidenceItem, ...]:
        """依次执行 chunk/span dedup、cap、多样性和 token packing。

        Args:
            candidates: canonical hydrated 候选和结构扩展。
            policy: Evidence V2 relevance、cap 与 token 预算。
            context: QueryAnalysis、QueryKind、rerank mode 与 selected slot。

        Returns:
            仅含单一可发布 span quote 的 EvidenceItem 序列。

        """
        unique_chunks = tuple(
            {item.hydrated.chunk.chunk_id: item for item in candidates}.values()
        )
        ordered = unique_chunks
        documents: Counter[str] = Counter()
        sections: Counter[tuple[str, str]] = Counter()
        used_spans: set[tuple[object, ...]] = set()
        remaining = policy.evidence_token_budget
        evidence: list[EvidenceItem] = []
        semantic_only_result = bool(
            policy.dense_semantic_enabled
            and context is not None
            and context.selected_slot is not None
            and unique_chunks
            and all(
                candidate.contributions
                and all(
                    contribution.channel.startswith("dense:")
                    for contribution in candidate.contributions
                )
                for candidate in unique_chunks
            )
        )
        best_relevance = max(
            (
                _best_candidate_relevance(candidate, context)
                for candidate in unique_chunks
            ),
            default=0.0,
        )
        relevance_floor = max(
            policy.minimum_span_overlap,
            best_relevance * _RELATIVE_RELEVANCE_FLOOR,
        )
        for candidate in ordered:
            if len(evidence) >= policy.max_evidence_items:
                break
            chunk = candidate.hydrated.chunk
            document_id = chunk.version.document_id
            section_key = (document_id, chunk.section_id)
            selected_for_chunk = 0
            for span, quote, span_key in _ranked_citable_spans(
                chunk,
                used_spans,
                context=context,
                minimum_overlap=relevance_floor,
                allow_semantic=semantic_only_result,
            ):
                if len(evidence) >= policy.max_evidence_items:
                    break
                if selected_for_chunk >= policy.max_evidence_items_per_chunk:
                    break
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
                selected_for_chunk += 1
                support_id = f"S{len(evidence) + 1}"
                evidence.append(
                    _evidence_item(candidate, span, quote, support_id)
                )
        return tuple(evidence)


def _ranked_citable_spans(
    chunk: Chunk,
    used: set[tuple[object, ...]],
    *,
    context: EvidenceSelectionContext | None,
    minimum_overlap: float,
    allow_semantic: bool,
) -> tuple[tuple[SourceSpan, str, tuple[object, ...]], ...]:
    selected: list[
        tuple[float, int, SourceSpan, str, tuple[object, ...]]
    ] = []
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
        if not quote.strip():
            continue
        relevance = _span_relevance(quote, context, chunk.role.value)
        if context is not None and not _span_is_eligible(
            relevance,
            context,
            minimum_overlap,
            allow_semantic=allow_semantic,
        ):
            continue
        selected.append(
            (
                relevance,
                span.chunk_end_char - span.chunk_start_char,
                span,
                quote,
                key,
            )
        )
    selected.sort(
        key=lambda item: (-item[0], item[1], item[2].chunk_start_char)
    )
    return tuple((span, quote, key) for _, _, span, quote, key in selected)


def _span_is_eligible(
    relevance: float,
    context: EvidenceSelectionContext,
    minimum_overlap: float,
    *,
    allow_semantic: bool,
) -> bool:
    if allow_semantic and relevance == 0.0:
        return True
    if context.query_kind is QueryKind.AMBIGUOUS:
        return relevance > 0.0
    return relevance >= minimum_overlap


def _span_relevance(
    quote: str,
    context: EvidenceSelectionContext | None,
    chunk_role: str,
) -> float:
    if context is None:
        return 1.0
    normalized_quote = quote.casefold()
    analysis = context.analysis
    table_score = 0.0
    if (
        context.query_kind is QueryKind.TABLE_NUMERIC
        and chunk_role == "table"
        and re.search(r"\d", quote)
        and re.search(
            r"(?:%|kg|mm|cm|mpa|kpa|℃|°c|秒|分钟|小时|米|千克)",
            normalized_quote,
        )
    ):
        table_score = 7.0 if re.search(r"[a-zA-Z℃°]", quote) else 6.0
    identifiers = tuple(
        item.casefold()
        for item in analysis.identifiers
        if item.casefold() in normalized_quote
    )
    identifier_score = 3.0 + len(identifiers) if identifiers else 0.0
    phrases = tuple(
        item.casefold()
        for item in analysis.quoted_phrases
        if item.casefold() in normalized_quote
    )
    phrase_score = 2.0 + len(phrases) if phrases else 0.0
    query_terms = _lexical_terms(analysis.normalized_query)
    if not query_terms:
        return max(table_score, identifier_score, phrase_score)
    quote_terms = _lexical_terms(quote)
    overlap = len(query_terms & quote_terms) / len(query_terms)
    return max(
        table_score,
        identifier_score + overlap,
        phrase_score + overlap,
        overlap,
    )


def _best_candidate_relevance(
    candidate: RankedChunk,
    context: EvidenceSelectionContext | None,
) -> float:
    chunk = candidate.hydrated.chunk
    return max(
        (
            _span_relevance(
                chunk.citation_text[
                    span.chunk_start_char : span.chunk_end_char
                ],
                context,
                chunk.role.value,
            )
            for span in chunk.source_spans
            if span.is_citable
            and span.span_type is not SourceSpanKind.SEPARATOR
        ),
        default=0.0,
    )


def _lexical_terms(value: str) -> set[str]:
    terms = {
        item.casefold()
        for item in re.findall(r"[A-Za-z0-9]+(?:[-_.:/][A-Za-z0-9]+)*", value)
    }
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        terms.update(run)
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


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
        table_context=chunk.role.value == "table",
        selection_reason=(
            candidate.expansion_reason or "retrieval_candidate"
        ),
        publishable=True,
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
