"""只读 canonical 直接命中预览；不参与召回、置信度或答案资格。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_app.application.retrieval.answer_support import evaluate_span_support
from rag_app.application.retrieval.filters import apply_candidate_filters
from rag_app.core.errors import IndexCorrupt
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ChannelHit,
    Chunk,
    ChunkRole,
    ProviderFailureCategory,
    QueryAnalysis,
    RankedChunk,
    RelatedContent,
    SearchRequest,
    SourceSpan,
    SourceSpanKind,
)


@dataclass(frozen=True)
class RelatedDisplayPolicy:
    """集中保存展示预算，不代表模型 token 或质量阈值。"""

    version: str = "1"
    max_items: int = 3
    max_chars: int = 240
    total_chars: int = 720


DISPLAY_POLICY = RelatedDisplayPolicy()
_MIN_TERM_LENGTH = 2
_COMMON = frozenset(
    {
        "申请",
        "办理",
        "负责",
        "需要",
        "如何",
        "什么",
        "审核",
        "复核",
        "登记",
        "提出",
        "更正",
        "相关",
        "内容",
        "资料",
        "进行",
        "可以",
        "the",
        "what",
        "how",
        "is",
        "are",
        "of",
        "to",
        "and",
    }
)
_RERANK_FAILURES = frozenset(
    {
        "rerank_bypassed_provider_unavailable",
        "rerank_bypassed_circuit_open",
    }
)


def rerank_dependency_failed(mode: str) -> bool:
    """只识别实际 Provider/circuit 故障，不把策略拒绝称作故障。

    Args:
        mode: 本次稳定重排执行模式。

    Returns:
        是否实际经过 Provider 失败或熔断旁路。

    """
    return mode in _RERANK_FAILURES


def related_display_message(
    items: tuple[RelatedContent, ...],
    mode: str,
    *,
    failure_category: ProviderFailureCategory | None = None,
) -> str:
    """根据实际结果选择有限中文文案，不推断全知识库事实。

    Args:
        items: 当前安全相关预览。
        mode: 本次稳定重排执行模式。
        failure_category: 本次实际异常或熔断保留的类别；缺失不猜测。

    Returns:
        用户可见的固定提示。

    """
    if not items:
        return (
            "本次检索未找到足够相关的内容。可以补充关键词，"
            "或检查当前知识库是否包含所需资料。"
        )
    if (
        rerank_dependency_failed(mode)
        and failure_category is ProviderFailureCategory.TRANSIENT
    ):
        return (
            "重排服务暂时不可用，这次未能可靠确认答案。"
            "你可以先查看下面检索到的内容。"
        )
    return (
        "本次检索未找到足以直接回答这个问题的依据。"
        "下面这些内容可能相关，供你查阅。"
    )


def validate_candidate(
    candidate: RankedChunk, request: SearchRequest, revision_id: str
) -> None:
    """复核 canonical 模型、请求范围和过滤条件，失配保持失败关闭。

    Args:
        candidate: 当前 canonical 候选。
        request: 当前范围与过滤条件。
        revision_id: 当前请求快照版本。

    Returns:
        无返回值。

    Raises:
        IndexCorrupt: 候选身份或来源损坏。

    """
    chunk = candidate.hydrated.chunk
    try:
        Chunk.model_validate(chunk.model_dump())
    except ValueError as error:
        raise IndexCorrupt(
            "相关原文来源损坏。", stage="retrieval.related"
        ) from error
    if (chunk.project_id, chunk.knowledge_base_id, chunk.index_revision_id) != (
        request.scope.project_id,
        request.scope.knowledge_base_id,
        revision_id,
    ):
        raise IndexCorrupt("相关原文身份失配。", stage="retrieval.related")
    hit = ChannelHit(
        revision_id=revision_id,
        chunk_id=chunk.chunk_id,
        document_id=chunk.version.document_id,
        document_version_id=chunk.version.document_version_id,
        role=chunk.role.value,
        section_id=chunk.section_id,
        content_sha256=chunk.content_sha256,
        channel="related-validation",
        rank=1,
        raw_score=0.0,
    )
    if not apply_candidate_filters((hit,), request):
        raise IndexCorrupt(
            "相关原文不符合当前过滤条件。", stage="retrieval.related"
        )


def select_related_contents(
    candidates: tuple[RankedChunk, ...],
    request: SearchRequest,
    analysis: QueryAnalysis,
    *,
    revision_id: str,
    rerank_mode: str,
) -> tuple[RelatedContent, ...]:
    """从当前已校验直接候选选择独立原文，不访问端口或网络。

    Args:
        candidates: 原重排或融合顺序的 canonical 直接命中。
        request: 当前 scope 和访问过滤条件。
        analysis: 原问题的规则分析。
        revision_id: 本次不可变索引身份。
        rerank_mode: 本次实际执行模式。

    Returns:
        最多三条有真实来源、不能作为答案依据的预览。

    Raises:
        IndexCorrupt: canonical 身份或来源范围损坏。

    """
    result: list[RelatedContent] = []
    used_chunks: set[str] = set()
    used_ranges: set[tuple[object, ...]] = set()
    used_text: set[str] = set()
    remaining = DISPLAY_POLICY.total_chars
    for candidate in candidates:
        validate_candidate(candidate, request, revision_id)
        chunk = candidate.hydrated.chunk
        if chunk.chunk_id in used_chunks or not candidate.contributions:
            continue
        # 本轮不拼接表格单元格。无法保证完整行列语义时省略卡片。
        if chunk.role is ChunkRole.TABLE:
            continue
        for span in chunk.source_spans:
            if (
                span.span_type is not SourceSpanKind.ORIGINAL_TEXT
                or not span.is_citable
            ):
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ]
            if not _meaningful_overlap(analysis, quote):
                continue
            quote, selected_span = _excerpt(
                quote, span, min(DISPLAY_POLICY.max_chars, remaining), analysis
            )
            key = (
                chunk.version.document_version_id,
                span.node_id,
                selected_span.source_start_char,
                selected_span.source_end_char,
            )
            if not quote.strip() or key in used_ranges or quote in used_text:
                continue
            verified = (
                rerank_mode == "provider"
                and candidate.rerank_rank is not None
                and candidate.rerank_score is not None
            )
            result.append(
                RelatedContent(
                    related_id="related_"
                    + canonical_sha256((revision_id, chunk.chunk_id, key))[
                        7:39
                    ],
                    document_id=chunk.version.document_id,
                    document_version_id=chunk.version.document_version_id,
                    index_revision_id=revision_id,
                    chunk_id=chunk.chunk_id,
                    document_name=candidate.hydrated.display_name,
                    heading_path=chunk.heading_path,
                    excerpt=quote,
                    source_spans=(selected_span,),
                    rerank_verified=verified,
                    relevance_reason="KEYWORD_RELATED"
                    if verified
                    else "RELEVANCE_UNVERIFIED",
                )
            )
            used_chunks.add(chunk.chunk_id)
            used_ranges.add(key)
            used_text.add(quote)
            remaining -= len(quote)
            break
        if len(result) >= DISPLAY_POLICY.max_items or remaining <= 0:
            break
    return tuple(result)


def _related_terms(analysis: QueryAnalysis, quote: str) -> set[str]:
    target = evaluate_span_support(analysis, quote).query_target.casefold()
    terms = set(re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", target))
    for run in re.findall(r"[\u3400-\u9fff]+", target):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    terms -= _COMMON
    return {term for term in terms if len(term) >= _MIN_TERM_LENGTH}


def _meaningful_overlap(analysis: QueryAnalysis, quote: str) -> bool:
    return any(
        term in quote.casefold() for term in _related_terms(analysis, quote)
    )


def _excerpt(
    quote: str, span: SourceSpan, limit: int, analysis: QueryAnalysis
) -> tuple[str, SourceSpan]:
    matches = tuple(
        match.start()
        for term in _related_terms(analysis, quote)
        for match in re.finditer(re.escape(term), quote, re.IGNORECASE)
    )
    position = min(matches, default=0)
    boundaries = [
        match.end()
        for match in re.finditer(r"[。！？.!?；;\n]", quote[:position])
    ]
    start = max(boundaries, default=0)
    if position - start >= limit:
        start = max(start, position - limit // 3)
    end = min(len(quote), start + limit)
    if len(quote) > limit:
        ends = [
            match.end()
            for match in re.finditer(r"[。！？.!?；;\n]", quote[start:end])
            if start + match.end() > position
        ]
        if ends:
            end = start + ends[-1]
    selected = SourceSpan.model_validate(
        {
            **span.model_dump(),
            "chunk_start_char": span.chunk_start_char + start,
            "chunk_end_char": span.chunk_start_char + end,
            "source_start_char": (span.source_start_char or 0) + start,
            "source_end_char": (span.source_start_char or 0) + end,
        }
    )
    return quote[start:end], selected
