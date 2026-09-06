"""Canonical source-span 去重、来源多样性和预算 packing。"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

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
_MIN_TABLE_LABEL_LENGTH = 2
_TableKey = tuple[str, str, str, str, str, tuple[str, ...]]
_SpanKey = tuple[object, ...]


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
        table_spans = _table_intersections(unique_chunks, context)
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
            and not context.rerank_mode.casefold().startswith("rerank_")
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
                allow_semantic=(
                    semantic_only_result and candidate.rerank_rank is not None
                ),
                table_spans=table_spans.get(chunk.chunk_id),
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


def _ranked_citable_spans(  # noqa: PLR0913
    chunk: Chunk,
    used: set[tuple[object, ...]],
    *,
    context: EvidenceSelectionContext | None,
    minimum_overlap: float,
    allow_semantic: bool,
    table_spans: set[_SpanKey] | None = None,
) -> tuple[tuple[SourceSpan, str, tuple[object, ...]], ...]:
    selected: list[tuple[float, int, SourceSpan, str, tuple[object, ...]]] = []
    for span in chunk.source_spans:
        if not span.is_citable or span.span_type is SourceSpanKind.SEPARATOR:
            continue
        key = _span_key(chunk, span)
        if key in used:
            continue
        if table_spans is not None and key not in table_spans:
            continue
        quote = chunk.citation_text[span.chunk_start_char : span.chunk_end_char]
        if not quote.strip():
            continue
        relevance = _span_relevance(quote, context, chunk.role.value)
        if (
            table_spans is None
            and context is not None
            and not _span_is_eligible(
                relevance,
                context,
                minimum_overlap,
                allow_semantic=allow_semantic,
            )
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


def _span_key(chunk: Chunk, span: SourceSpan) -> _SpanKey:
    return (
        chunk.version.document_version_id,
        span.node_id,
        span.source_start_char,
        span.source_end_char,
        span.span_type.value,
    )


def _table_intersections(
    candidates: tuple[RankedChunk, ...],
    context: EvidenceSelectionContext | None,
) -> dict[str, set[_SpanKey]]:
    """在同表候选中用唯一行名和列头定位原始单元格。

    只识别具有第零行表头和第零列行名的规则表。标签必须完整出现在
    查询中，缺失、重复或多坐标请求继续使用原有保守选择。跨 chunk
    仅共享结构信息，不拼接或重写引用；输出仍受现有 cap 和预算约束。
    """
    if context is None or context.query_kind is QueryKind.AMBIGUOUS:
        return {}
    tables: dict[_TableKey, dict[tuple[int, int], dict[_SpanKey, str]]] = (
        defaultdict(lambda: defaultdict(dict))
    )
    members: dict[_TableKey, set[str]] = defaultdict(set)
    invalid: set[_TableKey] = set()
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        if chunk.role.value != "table":
            continue
        for span in chunk.source_spans:
            location = _table_location(chunk, span)
            if location is None:
                continue
            table_key, row, column = location
            members[table_key].add(chunk.chunk_id)
            if not _regular_table_grid(chunk):
                invalid.add(table_key)
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if quote:
                tables[table_key][row, column][_span_key(chunk, span)] = quote
    query = context.analysis.normalized_query.casefold()
    selected: dict[str, set[_SpanKey]] = {}
    for table_key, cells in tables.items():
        if table_key in invalid:
            continue
        rows = {
            row
            for (row, column), values in cells.items()
            if row > 0 and column == 0 and _label_matches(values, query)
        }
        columns = {
            column
            for (row, column), values in cells.items()
            if row == 0 and column > 0 and _label_matches(values, query)
        }
        if len(rows) != 1 or len(columns) != 1:
            continue
        values = cells.get((next(iter(rows)), next(iter(columns))), {})
        if not values or len(set(values.values())) != 1:
            continue
        for chunk_id in members[table_key]:
            selected.setdefault(chunk_id, set()).update(values)
    return selected


def _label_matches(values: dict[_SpanKey, str], query: str) -> bool:
    labels = {value.casefold() for value in values.values()}
    return len(labels) == 1 and any(
        len(label) >= _MIN_TABLE_LABEL_LENGTH and label in query
        for label in labels
    )


def _table_location(
    chunk: Chunk, span: SourceSpan
) -> tuple[_TableKey, int, int] | None:
    if not span.is_citable or span.source_anchor is None:
        return None
    path = span.structural_path
    for index in range(len(path) - 2):
        if not path[index].startswith("tbl:"):
            continue
        row = re.fullmatch(r"tr:(\d+)", path[index + 1])
        column = re.fullmatch(r"tc:(\d+)", path[index + 2])
        if row is None or column is None:
            continue
        # 嵌套表有独立 group；不能把子表来源投射到外层单元格。
        if any(part.startswith("tbl:") for part in path[index + 1 :]):
            continue
        table_key = (
            chunk.version.document_id,
            chunk.version.document_version_id,
            chunk.neighbor_group_id,
            chunk.section_id,
            span.source_anchor.part_uri,
            path[: index + 1],
        )
        return table_key, int(row[1]), int(column[1])
    return None


def _regular_table_grid(chunk: Chunk) -> bool:
    """物理 tc 位置只用于无合并、无省略列的规则逻辑网格。"""
    atoms = dict(chunk.metadata).get("atoms", [])
    if not isinstance(atoms, list):
        return False
    for atom in atoms:
        if not isinstance(atom, dict):
            return False
        metadata = atom.get("metadata", {})
        if not isinstance(metadata, dict):
            return False
        coordinates = metadata.get("cell_coordinates", [])
        if not isinstance(coordinates, list):
            return False
        for physical_column, coordinate in enumerate(coordinates):
            match = re.fullmatch(r"r\d+:c(\d+):rs1:cs1", str(coordinate))
            if match is None or int(match[1]) != physical_column:
                return False
    return True


def _span_is_eligible(
    relevance: float,
    context: EvidenceSelectionContext,
    minimum_overlap: float,
    *,
    allow_semantic: bool,
) -> bool:
    if allow_semantic:
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
        selection_reason=(candidate.expansion_reason or "retrieval_candidate"),
        publishable=True,
        retrieval_origins=tuple(
            contribution.channel for contribution in candidate.contributions
        )
        + ((candidate.expansion_reason,) if candidate.expansion_reason else ()),
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
