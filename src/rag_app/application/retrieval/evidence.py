"""Canonical source-span 去重、来源多样性和预算 packing。"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict

from rag_app.application.retrieval.answer_support import (
    AnswerSupport,
    SupportStatus,
    evaluate_linked_support,
    evaluate_span_support,
)
from rag_app.core.models import (
    Chunk,
    EvidenceItem,
    EvidenceSelectionContext,
    QueryKind,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.chunk import SourceSpan, SourceSpanKind
from rag_app.core.models.common import freeze_json_object

_MIN_TABLE_LABEL_LENGTH = 2
_MAX_SEMANTIC_RANK = 10
_TableKey = tuple[str, str, str, str, str, str, str, str, tuple[str, ...]]
_SpanKey = tuple[object, ...]
_TableCells = dict[tuple[int, int], dict[_SpanKey, str]]


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
        support_overrides = _context_supports(
            unique_chunks, context, table_spans
        )
        ordered = unique_chunks
        documents: Counter[str] = Counter()
        sections: Counter[tuple[str, str]] = Counter()
        used_spans: set[tuple[object, ...]] = set()
        remaining = policy.evidence_token_budget
        evidence: list[EvidenceItem] = []
        # 字面得分仅在候选内部排列，不让别的表格单位淘汰当前段落。
        relevance_floor = policy.minimum_span_overlap
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
                allow_semantic=semantic_candidate_allowed(
                    candidate, policy, context
                ),
                table_spans=table_spans.get(chunk.chunk_id),
                support_overrides=support_overrides,
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
                item = _evidence_item(candidate, span, quote, support_id)
                if context is not None:
                    support = support_overrides.get(
                        span_key
                    ) or evaluate_span_support(
                        context.analysis,
                        quote,
                        span_id=span.node_id or "",
                        table_relation=table_spans.get(chunk.chunk_id)
                        is not None,
                    )
                    support_metadata = asdict(support)
                    support_metadata["status"] = support.status.value
                    support_metadata["supporting_span_ids"] = list(
                        support.supporting_span_ids
                    )
                    item = item.model_copy(
                        update={
                            "metadata": freeze_json_object(
                                {"answer_support": support_metadata}
                            )
                        }
                    )
                evidence.append(item)
        # 相邻对象标签与属性必须同时装入预算，禁止只发布其中半个支持链。
        return _complete_supports(tuple(evidence))


def _ranked_citable_spans(  # noqa: PLR0913
    chunk: Chunk,
    used: set[tuple[object, ...]],
    *,
    context: EvidenceSelectionContext | None,
    minimum_overlap: float,
    allow_semantic: bool,
    table_spans: set[_SpanKey] | None = None,
    support_overrides: dict[_SpanKey, AnswerSupport] | None = None,
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
        if context is not None:
            support = (support_overrides or {}).get(
                key
            ) or evaluate_span_support(
                context.analysis,
                quote,
                table_relation=table_spans is not None,
            )
            if support.status is not SupportStatus.SUPPORTED:
                continue
        if (
            table_spans is None
            and key not in (support_overrides or {})
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


def semantic_candidate_allowed(
    candidate: RankedChunk,
    policy: RetrievalPolicy,
    context: EvidenceSelectionContext | None,
) -> bool:
    """只检查当前直接候选的真实通道、重排和向量校准身份。

    Args:
        candidate: 当前 canonical 候选，扩展项不能继承种子身份。
        policy: 当前隔离验收或已校准策略。
        context: 当前实际路由与重排上下文。

    Returns:
        是否具备独立语义准入资格，不代表事实支持已经成立。

    """
    if (
        context is None
        or context.selected_slot is None
        or not policy.dense_semantic_enabled
        or policy.dense_semantic_calibration_state == "UNCALIBRATED"
        or context.rerank_mode.casefold().startswith("rerank_")
        or candidate.rerank_rank is None
        or candidate.rerank_rank > _MAX_SEMANTIC_RANK
        or candidate.expansion_reason is not None
    ):
        return False
    spaces = tuple(
        space
        for space in policy.dense_calibrated_vector_spaces
        if space.startswith(f"{context.selected_slot}:")
    )
    actual_space = context.selected_vector_space
    if actual_space is None and len(spaces) == 1:
        actual_space = spaces[0]
    return actual_space in spaces and any(
        contribution.channel == f"dense:{context.selected_slot}"
        for contribution in candidate.contributions
    )


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
        columns = _requested_columns(cells, rows, context)
        if len(rows) != 1 or len(columns) != 1:
            continue
        values = cells.get((next(iter(rows)), next(iter(columns))), {})
        if not values or len(set(values.values())) != 1:
            continue
        for chunk_id in members[table_key]:
            selected.setdefault(chunk_id, set()).update(values)
    return selected


def _requested_columns(
    cells: _TableCells, rows: set[int], context: EvidenceSelectionContext
) -> set[int]:
    query = context.analysis.normalized_query.casefold()
    columns = {
        column
        for (row, column), values in cells.items()
        if row == 0 and column > 0 and _header_matches(values, query)
    }
    if (
        len(rows) != 1
        or columns
        or not re.search(r"数值.*单位|单位.*数值", query)
    ):
        return columns
    # 无指定列名时，只接受所选行内唯一带物理单位的值。
    return {
        column
        for (row, column), values in cells.items()
        if row in rows
        and len(set(values.values())) == 1
        and evaluate_span_support(
            context.analysis,
            next(iter(values.values())),
            table_relation=True,
        ).status
        is SupportStatus.SUPPORTED
    }


def _header_matches(values: dict[_SpanKey, str], query: str) -> bool:
    if _label_matches(values, query):
        return True
    return (
        len(set(values.values())) == 1
        and re.search(r"谁(?!的)|哪位", query) is not None
        and re.search(
            r"受理角色|负责人员|责任人|审核人员|复核人员",
            next(iter(values.values())),
        )
        is not None
    )


def _context_supports(
    candidates: tuple[RankedChunk, ...],
    context: EvidenceSelectionContext | None,
    table_spans: dict[str, set[_SpanKey]],
) -> dict[_SpanKey, AnswerSupport]:
    if context is None:
        return {}
    supports: dict[_SpanKey, AnswerSupport] = {}
    headers: dict[tuple[_TableKey, int], set[str]] = defaultdict(set)
    chunks = {
        item.hydrated.chunk.chunk_id: item.hydrated.chunk for item in candidates
    }
    for chunk in chunks.values():
        for span in chunk.source_spans:
            location = _table_location(chunk, span)
            if location is not None and location[1] == 0:
                headers[location[0], location[2]].add(
                    chunk.citation_text[
                        span.chunk_start_char : span.chunk_end_char
                    ]
                )
    for chunk in chunks.values():
        for span in chunk.source_spans:
            key = _span_key(chunk, span)
            location = _table_location(chunk, span)
            if (
                key in table_spans.get(chunk.chunk_id, set())
                and location is not None
            ):
                labels = headers[location[0], location[2]]
                if len(labels) == 1:
                    supports[key] = evaluate_span_support(
                        context.analysis,
                        chunk.citation_text[
                            span.chunk_start_char : span.chunk_end_char
                        ],
                        span_id=span.node_id or "",
                        table_relation=True,
                        table_header=next(iter(labels)),
                    )
        neighbor = chunks.get(chunk.next_chunk_id or "")
        if neighbor is not None:
            supports.update(_linked_span_supports(context, chunk, neighbor))
    return supports


def _linked_span_supports(
    context: EvidenceSelectionContext, first: Chunk, second: Chunk
) -> dict[_SpanKey, AnswerSupport]:
    if (
        first.role.value == "table"
        or second.role.value == "table"
        or first.next_chunk_id != second.chunk_id
        or second.previous_chunk_id != first.chunk_id
        or any(
            getattr(first, field) != getattr(second, field)
            for field in (
                "project_id",
                "knowledge_base_id",
                "index_revision_id",
                "version",
                "section_id",
                "neighbor_group_id",
            )
        )
    ):
        return {}
    first_spans = [
        span for span in first.source_spans if span.is_citable and span.node_id
    ]
    second_spans = [
        span for span in second.source_spans if span.is_citable and span.node_id
    ]
    if not first_spans or not second_spans:
        return {}
    left, right = first_spans[-1], second_spans[0]
    support = evaluate_linked_support(
        context.analysis,
        first.citation_text[left.chunk_start_char : left.chunk_end_char],
        second.citation_text[right.chunk_start_char : right.chunk_end_char],
        (left.node_id or "", right.node_id or ""),
    )
    if support is None:
        return {}
    return {_span_key(first, left): support, _span_key(second, right): support}


def _complete_supports(
    evidence: tuple[EvidenceItem, ...],
) -> tuple[EvidenceItem, ...]:
    present = {span.node_id for item in evidence for span in item.source_spans}
    complete: list[EvidenceItem] = []
    for item in evidence:
        support = dict(item.metadata).get("answer_support")
        if (
            isinstance(support, dict)
            and support.get("support_reason") == "LINKED_SUBJECT_ATTRIBUTE"
        ):
            nodes = support.get("supporting_span_ids", [])
            if not isinstance(nodes, list) or any(
                node not in present for node in nodes
            ):
                continue
        complete.append(
            item.model_copy(update={"evidence_id": f"S{len(complete) + 1}"})
        )
    return tuple(complete)


def _label_matches(values: dict[_SpanKey, str], query: str) -> bool:
    labels = {
        re.sub(r"[（(][^()（）]*[）)]", "", value.casefold())
        for value in values.values()
    }
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
            chunk.project_id,
            chunk.knowledge_base_id,
            chunk.index_revision_id,
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
