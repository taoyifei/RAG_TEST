"""Canonical source-span 去重、来源多样性和预算 packing。"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from rag_app.application.retrieval.answer_support import (
    AnswerSupport,
    SupportStatus,
    evaluate_linked_support,
    evaluate_span_support,
)
from rag_app.application.retrieval.semantics import source_qualifier_matches
from rag_app.core.models import (
    Chunk,
    EvidenceItem,
    EvidenceSelectionContext,
    QueryKind,
    RankedChunk,
    RequestedAnswerType,
    RetrievalPolicy,
)
from rag_app.core.models.chunk import SourceSpan, SourceSpanKind
from rag_app.core.models.common import freeze_json_object
from rag_app.core.query_text import (
    context_label_variants,
    normalize_document_label,
    select_unique_label_owner,
)

_MIN_TABLE_LABEL_LENGTH = 2
_MAX_SEMANTIC_RANK = 10
_LIST_LEAD_IN = re.compile(
    r"(?:包括|包含|分为|分成|具体如下|步骤如下|流程如下|如下)"
    r"[^。；;\n]{0,16}[:：。]?$"
)
_NUMBERED_STAGE_HEADING = re.compile(r"^\d+\.\d+(?:\.\d+)?\s+\S")
_FLOW_ARCHITECTURE_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*)?\s*流程架构$")
_STAGE_QUERY = re.compile(r"阶段|环节|全流程")
_MINIMUM_STAGE_MEMBER_COUNT = 2
_FLOW_ARCHITECTURE_PATH_DEPTH = 2
_TABLE_HEADER_SEMANTICS = {
    "DEFINITION": re.compile(r"定义|释义|说明|含义|描述|交付件说明|内容说明"),
    "PURPOSE": re.compile(r"目的|目标|作用|用途|宗旨"),
    "DUTIES": re.compile(
        r"职责|工作内容|岗位任务|负责事项|主要工作|任务说明|"
        r"(?:角色|岗位|职能)(?:定义|说明|描述)"
    ),
    "RESPONSIBLE_PARTY": re.compile(
        r"责任角色|责任人|负责人|主责|牵头|经办|承办|受理角色|受理人|"
        r"负责部门|责任部门|责任单位"
    ),
}
_MODEL_REVIEWABLE_ANSWER_TYPES = frozenset(
    {
        "DEFINITION",
        "PURPOSE",
        "DUTIES",
        "PROCEDURE",
        "SECTION_SUMMARY",
    }
)
_MIN_MODEL_REVIEW_OVERLAP = 0.4
_MIN_MODEL_REVIEW_MULTI_CHAR_TERMS = 3
_TableKey = tuple[str, str, str, str, str, str, str, str, tuple[str, ...]]
_SpanKey = tuple[object, ...]
_TableCells = dict[tuple[int, int], dict[_SpanKey, str]]
_TablePiece = tuple[RankedChunk, SourceSpan, str]


@dataclass(frozen=True, slots=True)
class EvidenceSelectionResult:
    """区分召回、模型候选和最终最小支持集的内部结果。"""

    retrieval_candidates: tuple[RankedChunk, ...]
    model_evidence_candidates: tuple[EvidenceItem, ...]
    answer_support_set: tuple[EvidenceItem, ...]
    rejected_candidate_reasons: tuple[tuple[str, str], ...] = ()
    ambiguous: bool = False


class EvidenceAssembler:
    """只发布可映射到真实来源的单 span quote。"""

    def assemble(
        self,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
        *,
        context: EvidenceSelectionContext | None = None,
        allow_uncertain: bool = False,
    ) -> tuple[EvidenceItem, ...]:
        """兼容入口：返回可发布支持集或显式请求的模型候选。

        Args:
            candidates: canonical hydrated 候选和结构扩展。
            policy: Evidence V2 relevance、cap 与 token 预算。
            context: QueryAnalysis、QueryKind、rerank mode 与 selected slot。
            allow_uncertain: 配置了证据核验生成器时保留待核验的相关来源。

        Returns:
            默认返回最小充分支持集；显式允许 uncertain 时返回有界模型候选。

        """
        selection = self.assemble_sets(
            candidates,
            policy,
            context=context,
            include_model_candidates=allow_uncertain,
        )
        return (
            selection.model_evidence_candidates
            if allow_uncertain
            else selection.answer_support_set
        )

    def assemble_sets(
        self,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
        *,
        context: EvidenceSelectionContext | None = None,
        include_model_candidates: bool = False,
    ) -> EvidenceSelectionResult:
        """形成互不混淆的模型候选与最小充分支持集。

        Args:
            candidates: 有界召回和结构扩展候选。
            policy: Evidence 数量、来源与 token 上限。
            context: 当前共享 QueryAnalysis 与路由身份。
            include_model_candidates: 是否保留相关但未直接支持的模型候选。

        Returns:
            包含候选淘汰原因和跨文档歧义状态的选择结果。

        """
        model_candidates = self._assemble_candidates(
            candidates,
            policy,
            context=context,
            allow_uncertain=include_model_candidates,
        )
        supported = (
            model_candidates
            if context is None
            else tuple(
                item for item in model_candidates if _support_is_supported(item)
            )
        )
        support_set, ambiguous = _minimal_support_set(supported, context)
        selected_keys = {_evidence_key(item) for item in support_set}
        ordered = (
            *support_set,
            *(
                item
                for item in model_candidates
                if _evidence_key(item) not in selected_keys
            ),
        )
        normalized_candidates = _renumber_evidence(ordered)
        normalized_support = normalized_candidates[: len(support_set)]
        rejected = tuple(
            (
                item.chunk_id,
                "AMBIGUOUS_SAME_TARGET_ACROSS_DOCUMENTS"
                if ambiguous and _support_is_supported(item)
                else (
                    "NOT_IN_MINIMUM_SUPPORT_SET"
                    if _support_is_supported(item)
                    else "MODEL_EVIDENCE_NOT_DIRECTLY_SUPPORTED"
                ),
            )
            for item in model_candidates
            if _evidence_key(item) not in selected_keys
        )
        return EvidenceSelectionResult(
            retrieval_candidates=candidates,
            model_evidence_candidates=normalized_candidates,
            answer_support_set=normalized_support,
            rejected_candidate_reasons=rejected,
            ambiguous=ambiguous,
        )

    def _assemble_candidates(  # noqa: PLR0912, PLR0915
        self,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
        *,
        context: EvidenceSelectionContext | None = None,
        allow_uncertain: bool = False,
    ) -> tuple[EvidenceItem, ...]:
        """执行 span 去重、结构闭合、cap、多样性和 token packing。"""
        unique_chunks = tuple(
            {item.hydrated.chunk.chunk_id: item for item in candidates}.values()
        )
        source_qualifier = (
            context.analysis.semantics.source_qualifier
            if context is not None
            else None
        )
        if source_qualifier is not None:
            unique_chunks = tuple(
                item
                for item in unique_chunks
                if source_qualifier_matches(
                    item.hydrated.display_name,
                    item.hydrated.chunk.heading_path,
                    source_qualifier,
                )
            )
        stage_hierarchy = _stage_hierarchy_evidence(
            unique_chunks, policy, context
        )
        if stage_hierarchy is not None:
            return stage_hierarchy
        descriptive_table = _descriptive_table_evidence(
            unique_chunks, policy, context
        )
        if descriptive_table is not None:
            return descriptive_table
        definition_table = _definition_table_evidence(
            unique_chunks, policy, context
        )
        if definition_table is not None:
            return definition_table
        descriptive_list = _descriptive_list_evidence(
            unique_chunks, policy, context
        )
        if descriptive_list is not None:
            return descriptive_list
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
                **({"allow_uncertain": True} if allow_uncertain else {}),
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
                                {
                                    **dict(item.metadata),
                                    "answer_support": support_metadata,
                                }
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
    allow_uncertain: bool = False,
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
        temperature_supported = False
        if context is not None:
            support = (support_overrides or {}).get(
                key
            ) or evaluate_span_support(
                context.analysis,
                quote,
                table_relation=table_spans is not None,
            )
            model_reviewable = _model_reviewable_span(
                support,
                quote,
                relevance,
                context,
                minimum_overlap,
            )
            if support.status is not SupportStatus.SUPPORTED and not (
                allow_uncertain and (allow_semantic or model_reviewable)
            ):
                continue
            # 精确对象、温度属性与合法量值已逐项证明，中英词面差异不否定该证据。
            temperature_supported = (
                support.status is SupportStatus.SUPPORTED
                and support.answer_type == "TEMPERATURE"
            )
        if (
            table_spans is None
            and key not in (support_overrides or {})
            and context is not None
            and not temperature_supported
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


def _model_reviewable_span(
    support: AnswerSupport,
    quote: str,
    relevance: float,
    context: EvidenceSelectionContext,
    minimum_overlap: float,
) -> bool:
    """判断未直接支持的片段是否真能交给模型核验。

    弱词面命中只属于召回候选，不能仅因存在模型能力就改写最终拒答状态。
    结构化责任、计数、序号和列举必须先完成现有结构闭合；模型不能从同主题
    片段推断缺失的责任关系或集合成员。

    Args:
        support: 当前片段的确定性支持判定。
        quote: 未修改的真实 SourceSpan 文本。
        relevance: 当前片段对原问题的词面相关度。
        context: 当前请求共享的 QueryAnalysis 与路由上下文。
        minimum_overlap: 普通 Evidence 的最低词面覆盖阈值。

    Returns:
        片段具有足够对象或表面覆盖、值得由已授权模型核验时为 True。

    """
    analysis = context.analysis
    normalized_quote = quote.casefold()
    reviewable = support.status is SupportStatus.SUPPORTED
    if (
        support.status is SupportStatus.UNSUPPORTED
        and support.answer_type in _MODEL_REVIEWABLE_ANSWER_TYPES
    ):
        target = support.query_target.casefold().strip()
        reviewable = bool(target and target in normalized_quote)
    elif support.status is SupportStatus.UNCERTAIN:
        identifiers = tuple(item.casefold() for item in analysis.identifiers)
        phrases = tuple(item.casefold() for item in analysis.quoted_phrases)
        if identifiers:
            reviewable = all(item in normalized_quote for item in identifiers)
        elif phrases:
            reviewable = all(item in normalized_quote for item in phrases)
        else:
            query_terms = _lexical_terms(analysis.normalized_query)
            overlap = query_terms & _lexical_terms(quote)
            multi_char_overlap = sum(len(item) > 1 for item in overlap)
            required_overlap = max(
                _MIN_MODEL_REVIEW_OVERLAP,
                minimum_overlap * 2,
            )
            reviewable = bool(query_terms) and (
                relevance >= required_overlap
                and multi_char_overlap >= _MIN_MODEL_REVIEW_MULTI_CHAR_TERMS
            )
    return reviewable


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
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        if chunk.role.value != "table":
            continue
        for span in chunk.source_spans:
            location = _table_location(chunk, span)
            if location is None:
                continue
            table_key, row, column = location
            if not _table_coordinate_is_trusted(chunk, span, row, column):
                continue
            members[table_key].add(chunk.chunk_id)
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if quote:
                tables[table_key][row, column][_span_key(chunk, span)] = quote
    query = context.analysis.normalized_query.casefold()
    selected: dict[str, set[_SpanKey]] = {}
    for table_key, cells in tables.items():
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


def _descriptive_table_evidence(
    candidates: tuple[RankedChunk, ...],
    policy: RetrievalPolicy,
    context: EvidenceSelectionContext | None,
) -> tuple[EvidenceItem, ...] | None:
    """把同表唯一角色行的多段职责作为完整支持链，保留各自原文引用。"""
    if context is None:
        return None
    request = evaluate_span_support(context.analysis, "")
    if request.answer_type != "DUTIES":
        return None
    groups = _descriptive_table_groups(
        candidates,
        request.query_target,
        context.analysis.semantics.source_qualifier,
        context.analysis.semantics.context_qualifier,
    )
    if not groups:
        return None
    # 多个文档的同名角色不能悄悄拼成一个角色，保留明确的来源歧义。
    if (
        len(groups) != 1
        or not groups[0]
        or not _complete_table_pieces(groups[0])
    ):
        return ()
    pieces = groups[0]
    counts = Counter(piece[0].hydrated.chunk.chunk_id for piece in pieces)
    if (
        len(pieces)
        > min(
            policy.max_evidence_items,
            policy.per_document_cap,
            policy.per_section_cap,
        )
        or max(counts.values()) > policy.max_evidence_items_per_chunk
        or sum(max(1, (len(piece[2]) + 3) // 4) for piece in pieces)
        > policy.evidence_token_budget
    ):
        return ()
    span_ids = list(dict.fromkeys(piece[1].node_id for piece in pieces))
    metadata = freeze_json_object(
        {
            "answer_support": {
                "status": SupportStatus.SUPPORTED.value,
                "query_target": request.query_target,
                "requested_relation_or_attribute": "职责",
                "answer_type": "DUTIES",
                "support_reason": "TABLE_ROW_ATTRIBUTE",
                "supporting_span_ids": span_ids,
            }
        }
    )
    return tuple(
        _evidence_item(candidate, span, quote, f"S{index}").model_copy(
            update={
                "metadata": freeze_json_object(
                    {**dict(span.metadata), **dict(metadata)}
                )
            }
        )
        for index, (candidate, span, quote) in enumerate(pieces, 1)
    )


def _stage_hierarchy_evidence(  # noqa: PLR0911, PLR0912
    candidates: tuple[RankedChunk, ...],
    policy: RetrievalPolicy,
    context: EvidenceSelectionContext | None,
) -> tuple[EvidenceItem, ...] | None:
    """把唯一文档中的阶段标题与各节首正文闭合为完整支持组。"""
    if context is None:
        return None
    semantics = context.analysis.semantics
    if semantics.answer_type not in {
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.COUNT,
        RequestedAnswerType.ORDINAL_ITEM,
        RequestedAnswerType.PROCEDURE,
    } or not _STAGE_QUERY.search(
        " ".join(
            (
                semantics.relation or "",
                context.analysis.resolved_query
                or context.analysis.normalized_query,
            )
        )
    ):
        return None
    numbered_groups: dict[str, dict[tuple[str, ...], list[RankedChunk]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    flow_groups: dict[str, dict[tuple[str, ...], list[RankedChunk]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        flow_group = _flow_architecture_stage_group(chunk)
        if flow_group is not None:
            flow_groups[chunk.version.document_id][flow_group].append(candidate)
        numbered_group = _numbered_stage_heading_group(chunk)
        if numbered_group is not None:
            numbered_groups[chunk.version.document_id][numbered_group].append(
                candidate
            )
    grouped = {
        document_id: (
            flow_groups[document_id]
            if len(flow_groups[document_id]) >= _MINIMUM_STAGE_MEMBER_COUNT
            else numbered_groups[document_id]
        )
        for document_id in set(numbered_groups) | set(flow_groups)
        if len(flow_groups[document_id]) >= _MINIMUM_STAGE_MEMBER_COUNT
        or len(numbered_groups[document_id]) >= _MINIMUM_STAGE_MEMBER_COUNT
    }
    if not grouped:
        return ()
    target = semantics.target or ""
    anchor = select_unique_label_owner(
        target,
        (
            (
                candidate.hydrated.chunk.version.document_id,
                candidate.hydrated.display_name,
            )
            for candidate in candidates
        ),
    )
    anchored_documents = {anchor} if anchor is not None else set()
    eligible = set(grouped)
    if anchored_documents:
        eligible &= anchored_documents
    if len(eligible) != 1:
        return ()
    document_id = next(iter(eligible))
    pieces: list[_TablePiece] = []
    for _group, members in sorted(
        grouped[document_id].items(), key=_stage_group_order
    ):
        candidate = min(members, key=_candidate_source_order)
        chunk = candidate.hydrated.chunk
        span = next(
            (
                item
                for item in chunk.source_spans
                if item.is_citable
                and not item.is_repeated
                and item.span_type is not SourceSpanKind.SEPARATOR
            ),
            None,
        )
        if span is None:
            return ()
        quote = chunk.citation_text[
            span.chunk_start_char : span.chunk_end_char
        ].strip()
        if not quote:
            return ()
        pieces.append((candidate, span, quote))
    if len(pieces) < _MINIMUM_STAGE_MEMBER_COUNT:
        return ()
    if semantics.answer_type is RequestedAnswerType.ORDINAL_ITEM:
        ordinal = semantics.ordinal
        if ordinal is None or ordinal > len(pieces):
            return ()
        pieces = [pieces[ordinal - 1]]
    if (
        len(pieces) > policy.max_evidence_items
        or sum(max(1, (len(piece[2]) + 3) // 4) for piece in pieces)
        > policy.evidence_token_budget
    ):
        return ()
    span_ids = [
        span.node_id for _, span, _ in pieces if span.node_id is not None
    ]
    metadata = freeze_json_object(
        {
            "answer_support": {
                "status": SupportStatus.SUPPORTED.value,
                "query_target": semantics.target,
                "requested_relation_or_attribute": semantics.relation,
                "answer_type": semantics.answer_type.value,
                "support_reason": "SECTION_STAGE_SET",
                "supporting_span_ids": span_ids,
            }
        }
    )
    return tuple(
        _evidence_item(candidate, span, quote, f"S{index}").model_copy(
            update={
                "metadata": freeze_json_object(
                    {**dict(span.metadata), **dict(metadata)}
                )
            }
        )
        for index, (candidate, span, quote) in enumerate(pieces, 1)
    )


def _stage_heading_group(chunk: Chunk) -> tuple[str, ...] | None:
    """识别编号子节或带流程架构子节的通用阶段组。"""
    return _flow_architecture_stage_group(
        chunk
    ) or _numbered_stage_heading_group(chunk)


def _flow_architecture_stage_group(
    chunk: Chunk,
) -> tuple[str, ...] | None:
    """将共享“流程架构”子节归到其真实顶层阶段。"""
    if len(
        chunk.heading_path
    ) >= _FLOW_ARCHITECTURE_PATH_DEPTH and _FLOW_ARCHITECTURE_HEADING.match(
        chunk.heading_path[1]
    ):
        return chunk.heading_path[:1]
    return None


def _numbered_stage_heading_group(
    chunk: Chunk,
) -> tuple[str, ...] | None:
    """返回 3.1/3.2 等编号阶段标题路径。"""
    for index, heading in enumerate(chunk.heading_path):
        if _NUMBERED_STAGE_HEADING.match(heading):
            return chunk.heading_path[: index + 1]
    return None


def _stage_group_order(
    item: tuple[tuple[str, ...], list[RankedChunk]],
) -> tuple[int, ...]:
    """按标题编号或首个真实来源顺序稳定排列阶段。"""
    group, members = item
    numbers = tuple(int(value) for value in re.findall(r"\d+", group[-1]))
    return numbers or (
        _candidate_source_order(min(members, key=_candidate_source_order)),
    )


def _candidate_source_order(candidate: RankedChunk) -> int:
    return min(
        (
            span.source_anchor.ordinal
            for span in candidate.hydrated.chunk.source_spans
            if span.is_citable and span.source_anchor is not None
        ),
        default=2**31 - 1,
    )


def _normalized_structure_text(value: str) -> str:
    return re.sub(
        r"[\s_\-—–/\\·:：()（）\[\]【】]+",
        "",
        value.casefold(),
    )


def _definition_table_evidence(
    candidates: tuple[RankedChunk, ...],
    policy: RetrievalPolicy,
    context: EvidenceSelectionContext | None,
) -> tuple[EvidenceItem, ...] | None:
    """将术语或交付件记录的行名、表头和值闭合为一个支持组。"""
    if (
        context is None
        or context.analysis.semantics.answer_type
        is not RequestedAnswerType.DEFINITION
        or not context.analysis.semantics.target
    ):
        return None
    target = context.analysis.semantics.target.casefold()
    source_qualifier = context.analysis.semantics.source_qualifier
    tables: dict[
        _TableKey, dict[tuple[int, int], dict[_SpanKey, _TablePiece]]
    ] = defaultdict(lambda: defaultdict(dict))
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        if (
            chunk.role.value != "table"
            or not _regular_table_grid(chunk)
            or (
                source_qualifier is not None
                and not source_qualifier_matches(
                    candidate.hydrated.display_name,
                    chunk.heading_path,
                    source_qualifier,
                )
            )
        ):
            continue
        for span in chunk.source_spans:
            location = _table_location(chunk, span)
            if location is None or span.is_repeated:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if quote:
                key, row, column = location
                tables[key][row, column][_span_key(chunk, span)] = (
                    candidate,
                    span,
                    quote,
                )
    groups: list[tuple[_TablePiece, ...]] = []
    definition_header = _TABLE_HEADER_SEMANTICS["DEFINITION"]
    for cells in tables.values():
        rows = {
            row
            for (row, column), pieces in cells.items()
            if row > 0
            and column == 0
            and {piece[2].strip().casefold() for piece in pieces.values()}
            == {target}
        }
        if len(rows) != 1:
            continue
        row = next(iter(rows))
        available_columns = {
            column
            for (header_row, column), header in cells.items()
            if header_row == 0
            and column > 0
            and cells.get((row, column))
            and header
        }
        definition_columns = {
            column
            for column in available_columns
            if any(
                definition_header.search(piece[2])
                for piece in cells[0, column].values()
            )
        }
        subject_headers = {
            piece[2].strip() for piece in cells.get((0, 0), {}).values()
        }
        record_row = any(
            re.search(r"交付件|交付物|文档|表单|记录", header)
            for header in subject_headers
        )
        selected_columns = (
            available_columns
            if record_row
            else definition_columns or available_columns
        )
        if not selected_columns:
            continue
        pieces: list[_TablePiece] = list(cells[row, 0].values())
        for column in sorted(selected_columns):
            pieces.extend(cells[0, column].values())
            pieces.extend(cells[row, column].values())
        unique = tuple(
            {
                _span_key(candidate.hydrated.chunk, span): (
                    candidate,
                    span,
                    quote,
                )
                for candidate, span, quote in pieces
            }.values()
        )
        groups.append(unique)
    if len(groups) != 1 or not groups[0]:
        return None if not groups else ()
    selected_pieces = groups[0]
    if not _complete_table_pieces(selected_pieces) or not _list_pieces_fit(
        selected_pieces, policy
    ):
        return ()
    span_ids = list(
        dict.fromkeys(
            span.node_id for _, span, _ in pieces if span.node_id is not None
        )
    )
    metadata = freeze_json_object(
        {
            "answer_support": {
                "status": SupportStatus.SUPPORTED.value,
                "query_target": context.analysis.semantics.target,
                "requested_relation_or_attribute": "定义",
                "answer_type": "DEFINITION",
                "support_reason": "TABLE_ROW_RECORD",
                "supporting_span_ids": span_ids,
            }
        }
    )
    return tuple(
        _evidence_item(candidate, span, quote, f"S{index}").model_copy(
            update={
                "metadata": freeze_json_object(
                    {**dict(span.metadata), **dict(metadata)}
                )
            }
        )
        for index, (candidate, span, quote) in enumerate(selected_pieces, 1)
    )


def _descriptive_list_evidence(  # noqa: PLR0911
    candidates: tuple[RankedChunk, ...],
    policy: RetrievalPolicy,
    context: EvidenceSelectionContext | None,
) -> tuple[EvidenceItem, ...] | None:
    """将关系导语与紧邻结构化列表作为一个完整证据集合。"""
    if context is None:
        return None
    semantics = context.analysis.semantics
    if (
        semantics.answer_type
        not in {
            RequestedAnswerType.ENUMERATION,
            RequestedAnswerType.COUNT,
            RequestedAnswerType.ORDINAL_ITEM,
            RequestedAnswerType.PROCEDURE,
        }
        or not semantics.target
        or not semantics.relation
    ):
        return None
    by_id = {item.hydrated.chunk.chunk_id: item for item in candidates}
    matches: list[
        tuple[RankedChunk, SourceSpan, str, tuple[RankedChunk, ...]]
    ] = []
    target = semantics.target.casefold()
    relation = semantics.relation.casefold()
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        intro_ordinal = _last_source_ordinal(chunk)
        list_candidates = tuple(
            item
            for item in candidates
            if item.hydrated.chunk.role.value == "list"
            and item.hydrated.chunk.version == chunk.version
            and item.hydrated.chunk.section_id == chunk.section_id
            and intro_ordinal is not None
            and _first_source_ordinal(item.hydrated.chunk) == intro_ordinal + 1
        )
        if len(list_candidates) != 1:
            continue
        next_candidate = list_candidates[0]
        for span in chunk.source_spans:
            if (
                not span.is_citable
                or span.span_type is SourceSpanKind.SEPARATOR
            ):
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            folded = quote.casefold()
            if (
                target in folded
                and relation in folded
                and _LIST_LEAD_IN.search(folded)
            ):
                matches.append(
                    (
                        candidate,
                        span,
                        quote,
                        _contiguous_list_chunks(next_candidate, by_id),
                    )
                )
    if not matches:
        return None
    if len(matches) != 1:
        return ()
    intro_candidate, intro_span, intro_quote, list_chunks = matches[0]
    items = tuple(
        (
            candidate,
            span,
            candidate.hydrated.chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip(),
        )
        for candidate in list_chunks
        for span in candidate.hydrated.chunk.source_spans
        if span.is_citable
        and span.span_type
        not in {
            SourceSpanKind.DERIVED_NUMBERING,
            SourceSpanKind.SEPARATOR,
        }
        and candidate.hydrated.chunk.citation_text[
            span.chunk_start_char : span.chunk_end_char
        ].strip()
    )
    ordinal = semantics.ordinal
    if ordinal is not None:
        if ordinal > len(items):
            return ()
        items = (items[ordinal - 1],)
    pieces = ((intro_candidate, intro_span, intro_quote), *items)
    if not items or not _list_pieces_fit(pieces, policy):
        return ()
    actual_count = sum(
        1
        for candidate in list_chunks
        for span in candidate.hydrated.chunk.source_spans
        if span.is_citable
        and span.span_type
        not in {
            SourceSpanKind.DERIVED_NUMBERING,
            SourceSpanKind.SEPARATOR,
        }
    )
    support_reason = (
        "SOURCE_CORRECTS_COUNT_PREMISE"
        if semantics.expected_count is not None
        and semantics.expected_count != actual_count
        else "STRUCTURED_LIST_RELATION"
    )
    span_ids = [
        span.node_id for _, span, _ in pieces if span.node_id is not None
    ]
    metadata = freeze_json_object(
        {
            "answer_support": {
                "status": SupportStatus.SUPPORTED.value,
                "query_target": semantics.target,
                "requested_relation_or_attribute": semantics.relation,
                "answer_type": semantics.answer_type.value,
                "support_reason": support_reason,
                "supporting_span_ids": span_ids,
            }
        }
    )
    return tuple(
        _evidence_item(candidate, span, quote, f"S{index}").model_copy(
            update={
                "metadata": freeze_json_object(
                    {**dict(span.metadata), **dict(metadata)}
                )
            }
        )
        for index, (candidate, span, quote) in enumerate(pieces, 1)
    )


def _contiguous_list_chunks(
    first: RankedChunk, by_id: dict[str, RankedChunk]
) -> tuple[RankedChunk, ...]:
    """按双向 chunk 链收集同一结构组内已扩展的列表块。"""
    result = [first]
    current = first.hydrated.chunk
    while current.next_chunk_id:
        candidate = by_id.get(current.next_chunk_id)
        if candidate is None:
            break
        following = candidate.hydrated.chunk
        if (
            following.role.value != "list"
            or following.previous_chunk_id != current.chunk_id
            or following.version != current.version
            or following.section_id != current.section_id
            or following.neighbor_group_id != current.neighbor_group_id
        ):
            break
        result.append(candidate)
        current = following
    return tuple(result)


def _first_source_ordinal(chunk: Chunk) -> int | None:
    ordinals = [
        span.source_anchor.ordinal
        for span in chunk.source_spans
        if span.source_anchor is not None and span.is_citable
    ]
    return min(ordinals) if ordinals else None


def _last_source_ordinal(chunk: Chunk) -> int | None:
    ordinals = [
        span.source_anchor.ordinal
        for span in chunk.source_spans
        if span.source_anchor is not None and span.is_citable
    ]
    return max(ordinals) if ordinals else None


def _list_pieces_fit(
    pieces: tuple[tuple[RankedChunk, SourceSpan, str], ...],
    policy: RetrievalPolicy,
) -> bool:
    counts = Counter(piece[0].hydrated.chunk.chunk_id for piece in pieces)
    return (
        len(pieces)
        <= min(
            policy.max_evidence_items,
            policy.per_document_cap,
            policy.per_section_cap,
        )
        and max(counts.values()) <= policy.max_evidence_items_per_chunk
        and sum(max(1, (len(piece[2]) + 3) // 4) for piece in pieces)
        <= policy.evidence_token_budget
    )


def _descriptive_table_groups(
    candidates: tuple[RankedChunk, ...],
    target: str,
    source_qualifier: str | None,
    context_qualifier: str | None,
) -> list[tuple[_TablePiece, ...]]:
    context_labels = tuple(
        (
            candidate.hydrated.chunk.version.document_id,
            " ".join(
                (
                    candidate.hydrated.display_name,
                    *candidate.hydrated.chunk.heading_path,
                )
            ),
        )
        for candidate in candidates
    )
    context_owners = {
        owner
        for variant in context_label_variants(context_qualifier or "")
        if (owner := select_unique_label_owner(variant, context_labels))
        is not None
    }
    context_owner = (
        next(iter(context_owners)) if len(context_owners) == 1 else None
    )
    tables: dict[
        _TableKey, dict[tuple[int, int], dict[_SpanKey, _TablePiece]]
    ] = defaultdict(lambda: defaultdict(dict))
    table_contexts: dict[_TableKey, list[str]] = defaultdict(list)
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        if (
            chunk.role.value != "table"
            or not _regular_table_grid(chunk)
            or (
                context_owner is not None
                and chunk.version.document_id != context_owner
            )
            or (
                source_qualifier is not None
                and not source_qualifier_matches(
                    candidate.hydrated.display_name,
                    chunk.heading_path,
                    source_qualifier,
                )
            )
        ):
            continue
        for span in chunk.source_spans:
            location = _table_location(chunk, span)
            if location is None or span.is_repeated:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ]
            if quote.strip():
                key, row, column = location
                table_contexts[key].append(
                    " ".join(
                        (
                            candidate.hydrated.display_name,
                            *chunk.heading_path,
                            quote,
                        )
                    )
                )
                tables[key][row, column][_span_key(chunk, span)] = (
                    candidate,
                    span,
                    quote,
                )
    context_tables = (
        {
            key
            for key, values in table_contexts.items()
            if any(
                normalize_document_label(variant)
                in normalize_document_label(" ".join(values))
                for variant in context_label_variants(context_qualifier)
            )
        }
        if context_qualifier
        else set()
    )
    groups: list[tuple[_TablePiece, ...]] = []
    for key, cells in tables.items():
        if context_tables and key not in context_tables:
            continue
        columns = {
            column
            for (row, column), pieces in cells.items()
            if row == 0
            and column > 0
            and any(
                _TABLE_HEADER_SEMANTICS["DUTIES"].search(p[2].strip())
                for p in pieces.values()
            )
        }
        rows = {
            row
            for (row, column), pieces in cells.items()
            if row > 0
            and column == 0
            and {p[2].strip().casefold() for p in pieces.values()} == {target}
        }
        if len(rows) != 1 or not columns:
            continue
        row = next(iter(rows))
        subject = tuple(cells[row, 0].values())
        values = tuple(
            piece
            for column in sorted(columns)
            for piece in cells.get((row, column), {}).values()
        )
        if not values:
            continue
        pieces = (*subject, *sorted(values, key=_table_piece_order))
        if any(
            not _all_cell_nodes_present(
                (
                    *subject,
                    *cells.get((row, column), {}).values(),
                ),
                row,
                column,
            )
            for column in columns
        ):
            return [()]
        groups.append(pieces)
    return groups


def _table_piece_order(piece: _TablePiece) -> tuple[int, int]:
    span = piece[1]
    return (
        span.source_anchor.ordinal if span.source_anchor is not None else 0,
        span.source_start_char or 0,
    )


def _all_cell_nodes_present(
    pieces: tuple[_TablePiece, ...], row: int, column: int
) -> bool:
    actual = {piece[1].node_id for piece in pieces}
    expected: set[str] = set()
    for candidate, _, _ in pieces:
        atoms = dict(candidate.hydrated.chunk.metadata).get("atoms", [])
        if not isinstance(atoms, list):
            continue
        for atom in atoms:
            if not isinstance(atom, dict):
                continue
            metadata = atom.get("metadata", {})
            if (
                not isinstance(metadata, dict)
                or metadata.get("row_index") != row
            ):
                continue
            cells = metadata.get("cell_source_node_ids", {})
            if isinstance(cells, dict):
                values = cells.get(str(column), [])
                if isinstance(values, list):
                    expected.update(
                        value for value in values if isinstance(value, str)
                    )
    return not expected or expected <= actual


def _complete_table_pieces(pieces: tuple[_TablePiece, ...]) -> bool:
    """跨 chunk 的同一段落必须覆盖整个实际来源范围。"""
    ranges: dict[str, list[SourceSpan]] = defaultdict(list)
    for _, span, _ in pieces:
        if span.node_id:
            ranges[span.node_id].append(span)
    for spans in ranges.values():
        first = spans[0]
        anchor = first.source_anchor
        if anchor is None:
            return False
        cursor = anchor.source_start_char
        if cursor is None or anchor.source_end_char is None:
            return False
        for span in sorted(spans, key=lambda s: s.source_start_char or 0):
            if span.source_start_char != cursor or span.source_end_char is None:
                return False
            cursor = span.source_end_char
        if cursor != anchor.source_end_char:
            return False
    return True


def _requested_columns(
    cells: _TableCells, rows: set[int], context: EvidenceSelectionContext
) -> set[int]:
    query = context.analysis.normalized_query.casefold()
    columns = {
        column
        for (row, column), values in cells.items()
        if row == 0 and column > 0 and _header_matches(values, query, context)
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


def _header_matches(
    values: dict[_SpanKey, str],
    query: str,
    context: EvidenceSelectionContext,
) -> bool:
    if _label_matches(values, query):
        return True
    if len(set(values.values())) != 1:
        return False
    header = next(iter(values.values()))
    answer_type = context.analysis.semantics.answer_type.value
    mapping = _TABLE_HEADER_SEMANTICS.get(answer_type)
    if mapping is not None and mapping.search(header):
        return True
    return (
        re.search(r"谁(?!的)|哪位", query) is not None
        and re.search(
            r"受理角色|负责人员|责任角色|责任人|审核人员|复核人员",
            header,
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
    supports = _section_heading_supports(candidates, context)
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


def _section_heading_supports(  # noqa: PLR0912
    candidates: tuple[RankedChunk, ...],
    context: EvidenceSelectionContext,
) -> dict[_SpanKey, AnswerSupport]:
    """用真实标题路径与首个正文 SourceSpan 闭合节级关系。"""
    semantics = context.analysis.semantics
    if (
        semantics.answer_type
        not in {
            RequestedAnswerType.PURPOSE,
            RequestedAnswerType.SECTION_SUMMARY,
            RequestedAnswerType.ENUMERATION,
            RequestedAnswerType.PROCEDURE,
        }
        or not semantics.target
    ):
        return {}
    if semantics.answer_type is RequestedAnswerType.PURPOSE:
        relation_pattern = re.compile(r"目的|目标|作用|用途|宗旨")
    elif semantics.answer_type is RequestedAnswerType.SECTION_SUMMARY:
        relation_pattern = re.compile(r"管理要求|工作要求|规定|要求|章节")
    elif semantics.answer_type is RequestedAnswerType.ENUMERATION:
        relation_pattern = re.compile(
            rf"{re.escape(semantics.relation or '')}|"
            r"交付物|交付件|输出|清单|包括|包含|分为|分成"
        )
    else:
        relation_pattern = re.compile(
            rf"{re.escape(semantics.relation or '')}|"
            r"操作|步骤|流程|导入|上传|录入|登记|创建|配置"
        )
    document_owner = select_unique_label_owner(
        semantics.target,
        (
            (
                candidate.hydrated.chunk.version.document_id,
                candidate.hydrated.display_name,
            )
            for candidate in candidates
        ),
    )
    document_purpose_has_heading = any(
        semantics.answer_type is RequestedAnswerType.PURPOSE
        and document_owner is not None
        and candidate.hydrated.chunk.version.document_id == document_owner
        and candidate.hydrated.chunk.role.value
        not in {"table", "image_metadata", "header_footer"}
        and relation_pattern.search(
            " / ".join(candidate.hydrated.chunk.heading_path)
        )
        for candidate in candidates
    )
    document_purpose_depth = min(
        (
            len(candidate.hydrated.chunk.heading_path)
            for candidate in candidates
            if semantics.answer_type is RequestedAnswerType.PURPOSE
            and document_owner is not None
            and candidate.hydrated.chunk.version.document_id == document_owner
            and candidate.hydrated.chunk.role.value
            not in {"table", "image_metadata", "header_footer"}
            and relation_pattern.search(
                " / ".join(candidate.hydrated.chunk.heading_path)
            )
        ),
        default=None,
    )
    supports: dict[_SpanKey, AnswerSupport] = {}
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        if chunk.role.value in {"table", "image_metadata", "header_footer"}:
            continue
        heading = " / ".join(chunk.heading_path)
        source_matches = (
            semantics.source_qualifier is not None
            and source_qualifier_matches(
                candidate.hydrated.display_name,
                chunk.heading_path,
                semantics.source_qualifier,
            )
        )
        target_matches = semantics.target.casefold() in heading.casefold()
        document_target_matches = (
            document_owner is not None
            and chunk.version.document_id == document_owner
        )
        if not (
            source_matches or target_matches or document_target_matches
        ) or not relation_pattern.search(
            " ".join((heading, chunk.citation_text))
        ):
            continue
        if (
            semantics.answer_type is RequestedAnswerType.PURPOSE
            and document_target_matches
            and document_purpose_has_heading
            and not relation_pattern.search(heading)
        ):
            continue
        if (
            semantics.answer_type is RequestedAnswerType.PURPOSE
            and document_target_matches
            and document_purpose_depth is not None
            and len(chunk.heading_path) != document_purpose_depth
        ):
            continue
        citable_spans = tuple(
            item
            for item in chunk.source_spans
            if item.is_citable
            and not item.is_repeated
            and item.span_type is not SourceSpanKind.SEPARATOR
        )
        span = next(iter(citable_spans), None)
        if not relation_pattern.search(heading):
            span = next(
                (
                    item
                    for item in citable_spans
                    if relation_pattern.search(
                        chunk.citation_text[
                            item.chunk_start_char : item.chunk_end_char
                        ]
                    )
                ),
                None,
            )
        if span is None:
            continue
        quote = chunk.citation_text[span.chunk_start_char : span.chunk_end_char]
        if not quote.strip() or re.search(r"未提供|未确定|暂无|未知", quote):
            continue
        if (
            semantics.answer_type is RequestedAnswerType.ENUMERATION
            and not re.search(
                r"包括|包含|分为|分成|输出|交付|清单|"
                r"[①②③④⑤⑥⑦⑧⑨⑩]|(?:^|[；;])\s*\d+[.、)]",
                quote,
            )
        ):
            continue
        supports[_span_key(chunk, span)] = AnswerSupport(
            status=SupportStatus.SUPPORTED,
            query_target=semantics.target,
            requested_relation_or_attribute=semantics.relation or "章节内容",
            answer_type=semantics.answer_type.value,
            support_reason="SECTION_HEADING_BODY",
            supporting_span_ids=(span.node_id,) if span.node_id else (),
        )
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


def _table_coordinate_is_trusted(
    chunk: Chunk,
    span: SourceSpan,
    row: int,
    column: int,
) -> bool:
    """仅信任能由节点映射核对的合并或偏移表格逻辑坐标。"""
    atoms = dict(chunk.metadata).get("atoms", [])
    if not isinstance(atoms, list):
        return False
    # 旧测试或 Adapter 没有坐标元数据时沿用 SourceAnchor；
    # Product Parser 会提供 atoms。
    if not atoms:
        return True
    if span.node_id is None:
        return False
    source_mappings = [_source_node_mapping(atom) for atom in atoms]
    if all(mapping is None for mapping in source_mappings):
        return _regular_table_grid(chunk)
    if any(not isinstance(mapping, dict) for mapping in source_mappings):
        return False
    matches = tuple(
        _table_atom_matches_coordinate(atom, span.node_id, row, column)
        for atom in atoms
    )
    return None not in matches and any(matches)


def _source_node_mapping(atom: object) -> object:
    """读取单个 atom 的节点映射，并保留缺失与非法状态的区别。"""
    if not isinstance(atom, dict):
        return False
    metadata = atom.get("metadata", {})
    if not isinstance(metadata, dict):
        return False
    return metadata.get("cell_source_node_ids")


def _table_atom_matches_coordinate(
    atom: object,
    node_id: str,
    row: int,
    column: int,
) -> bool | None:
    """验证单个表格 atom，并返回节点是否命中目标逻辑坐标。"""
    if not isinstance(atom, dict):
        return None
    metadata = atom.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    row_index = metadata.get("row_index")
    coordinates = metadata.get("cell_coordinates")
    source_nodes = metadata.get("cell_source_node_ids")
    if (
        not isinstance(row_index, int)
        or not isinstance(coordinates, list)
        or not isinstance(source_nodes, dict)
    ):
        return None
    matched = False
    for physical_column, coordinate in enumerate(coordinates):
        logical_cell = _validated_logical_cell(
            coordinate,
            row_index,
            source_nodes.get(str(physical_column)),
        )
        if logical_cell is None:
            return None
        logical_row, logical_column, nodes = logical_cell
        if node_id in nodes and (logical_row, logical_column) == (row, column):
            matched = True
    return matched


def _validated_logical_cell(
    coordinate: object,
    row_index: int,
    nodes: object,
) -> tuple[int, int, list[str]] | None:
    """解析并验证一个物理单元格声明的逻辑坐标和来源节点。"""
    coordinate_match = re.fullmatch(
        r"r(\d+):c(\d+):rs(\d+):cs(\d+)", str(coordinate)
    )
    if (
        coordinate_match is None
        or int(coordinate_match[1]) != row_index
        or int(coordinate_match[3]) < 1
        or int(coordinate_match[4]) < 1
        or not isinstance(nodes, list)
        or any(not isinstance(node, str) for node in nodes)
    ):
        return None
    return int(coordinate_match[1]), int(coordinate_match[2]), nodes


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
        source_spans=(_relative_span(span, quote, chunk.citation_text),),
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
        metadata=(
            span.metadata if dict(span.metadata).get("origin") == "ocr" else ()
        ),
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


def _relative_span(span: SourceSpan, quote: str, chunk_text: str) -> SourceSpan:
    """把原 chunk span 精确投影到发布 quote 的相对坐标。

    结构证据会去掉单元格首尾空白。此时不仅要缩短 quote 的 chunk
    坐标，也必须同步移动 source 坐标；否则引用看似指向原节点，实际
    字符范围却不再等长。除首尾空白外禁止改写或借用别的 chunk 文本。

    Args:
        span: quote 在 canonical chunk 中的原始映射。
        quote: 准备发布的原文片段。
        chunk_text: span 所属 canonical chunk 的完整 citation_text。

    Returns:
        相对于 quote 的、仍与原始来源字符一一对应的 span。

    Raises:
        ValueError: quote 不是该 span 的原文或仅去空白后的原文。

    """
    raw = chunk_text[span.chunk_start_char : span.chunk_end_char]
    if quote == raw:
        leading_trim = 0
    elif quote == raw.strip():
        leading_trim = len(raw) - len(raw.lstrip())
    else:
        raise ValueError("发布证据必须来自当前 chunk span 的原文。")
    updates: dict[str, int] = {
        "chunk_start_char": 0,
        "chunk_end_char": len(quote),
    }
    if span.source_start_char is not None:
        source_start = span.source_start_char + leading_trim
        updates.update(
            {
                "source_start_char": source_start,
                "source_end_char": source_start + len(quote),
            }
        )
    return span.model_copy(update=updates)


def _source_label(display_name: str, headings: tuple[str, ...]) -> str:
    return (
        f"{display_name} · {' / '.join(headings)}" if headings else display_name
    )


def _support_is_supported(item: EvidenceItem) -> bool:
    """判断候选是否已经直接证明本次所问关系。"""
    support = dict(item.metadata).get("answer_support")
    return isinstance(support, dict) and support.get("status") == (
        SupportStatus.SUPPORTED.value
    )


def _evidence_key(item: EvidenceItem) -> tuple[object, ...]:
    """返回不依赖临时 Support ID 的真实来源身份。"""
    return (
        item.document_version_id,
        item.chunk_id,
        item.citation_text,
        tuple(
            (
                span.node_id,
                span.source_start_char,
                span.source_end_char,
                span.span_type.value,
            )
            for span in item.source_spans
        ),
    )


def _minimal_support_set(
    supported: tuple[EvidenceItem, ...],
    context: EvidenceSelectionContext | None,
) -> tuple[tuple[EvidenceItem, ...], bool]:
    """选择第一个完整结构支持组，并保留跨文档同名歧义。"""
    if not supported or context is None:
        return supported, False
    semantics = context.analysis.semantics
    if semantics.target and semantics.source_qualifier is None:
        documents = {item.document_id for item in supported}
        if len(documents) > 1:
            return (), True
    first = supported[0]
    metadata = dict(first.metadata).get("answer_support")
    if not isinstance(metadata, dict):
        return (first,), False
    nodes = metadata.get("supporting_span_ids", [])
    required_nodes = (
        {node for node in nodes if isinstance(node, str)}
        if isinstance(nodes, list)
        else set()
    )
    if not required_nodes:
        return (first,), False
    grouped = tuple(
        item
        for item in supported
        if item.document_version_id == first.document_version_id
        and any(span.node_id in required_nodes for span in item.source_spans)
    )
    present_nodes = {
        span.node_id for item in grouped for span in item.source_spans
    }
    if not required_nodes <= present_nodes:
        return (), False
    return grouped, False


def _renumber_evidence(
    evidence: tuple[EvidenceItem, ...],
) -> tuple[EvidenceItem, ...]:
    """在支持集优先排序后重新分配唯一稳定 Support ID。"""
    return tuple(
        item.model_copy(update={"evidence_id": f"S{index}"})
        for index, item in enumerate(evidence, 1)
    )


__all__ = ["EvidenceAssembler", "EvidenceSelectionResult"]
