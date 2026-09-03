"""P07 五类查询的确定性、bounded Planner。"""

from __future__ import annotations

from rag_app.core.models import (
    QueryAnalysis,
    QueryKind,
    QueryVariant,
    RetrievalPlan,
    RetrievalPolicy,
)

_COMPLEX_TERMS = (
    "比较",
    "对比",
    "为什么",
    "原因",
    "影响",
    "分别",
    "compare",
    "difference",
    "why",
)
_AMBIGUOUS_TERMS = ("这个", "那个", "它", "哪一个", "this", "that", "it")
_MAX_AMBIGUOUS_QUERY_LENGTH = 2


class QueryPlanner:
    """分类失败时仍产生 balanced 基础检索计划。"""

    def plan(
        self,
        analysis: QueryAnalysis,
        variants: tuple[QueryVariant, ...],
        policy: RetrievalPolicy,
        *,
        dense_required: bool = False,
    ) -> RetrievalPlan:
        """从显式信号构造有界通道和 evidence 预算。

        Args:
            analysis: 确定性 query 信号。
            variants: 原始 query 加至多一个规范化变体。
            policy: P07 provisional 上限。
            dense_required: 是否在 Dense 不可用时失败关闭。

        Returns:
            五类之一的可解释检索计划。

        """
        kind, reason = _classify(analysis)
        channels: tuple[str, ...] = ("lexical", "dense")
        must_keep = False
        neighbor = "same_group"
        if kind is QueryKind.EXACT_IDENTIFIER:
            channels = ("exact", "lexical", "dense")
            must_keep = True
        elif kind is QueryKind.TABLE_NUMERIC:
            channels = ("exact", "lexical", "dense")
            neighbor = "table"
        elif kind is QueryKind.AMBIGUOUS:
            neighbor = "none"
        elif kind is QueryKind.COMPLEX:
            neighbor = "section"
        enabled = set(policy.enabled_channels)
        channels = tuple(channel for channel in channels if channel in enabled)
        if not channels:
            channels = tuple(policy.enabled_channels)
        if not policy.neighbor_expansion_enabled:
            neighbor = "none"
        top_k = tuple((channel, policy.channel_top_k) for channel in channels)
        return RetrievalPlan(
            query_kind=kind,
            variants=variants[: policy.max_variants],
            channels=channels,
            channel_top_k=top_k,
            must_keep_exact=must_keep,
            use_reranker=policy.rerank_enabled,
            neighbor_mode=neighbor,
            evidence_token_budget=policy.evidence_token_budget,
            dense_required=dense_required,
            reason_codes=(reason, "P07_PROVISIONAL_PARAMETERS"),
            provisional_confidence=0.5,
        )


def _classify(analysis: QueryAnalysis) -> tuple[QueryKind, str]:
    folded = analysis.normalized_query.casefold()
    if analysis.structural_table_signals and (
        analysis.identifiers or analysis.numbers or analysis.units
    ):
        return QueryKind.TABLE_NUMERIC, "TABLE_NUMERIC_PLAN"
    if analysis.identifiers:
        return QueryKind.EXACT_IDENTIFIER, "IDENTIFIER_PLAN"
    if any(term in folded for term in _COMPLEX_TERMS):
        return QueryKind.COMPLEX, "COMPLEX_PLAN"
    if len(folded) <= _MAX_AMBIGUOUS_QUERY_LENGTH or folded in _AMBIGUOUS_TERMS:
        return QueryKind.AMBIGUOUS, "BALANCED_AMBIGUOUS_FALLBACK"
    return QueryKind.SIMPLE_FACT, "SIMPLE_FACT_PLAN"


__all__ = ["QueryPlanner"]
