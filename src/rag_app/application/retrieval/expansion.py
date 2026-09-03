"""P07 有界、无 LLM 依赖的 query expansion。"""

from __future__ import annotations

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import QueryAnalysis, QueryVariant


class NoopExpander:
    """只保留原始 query 的默认安全实现。"""

    def expand(self, analysis: QueryAnalysis) -> tuple[QueryVariant, ...]:
        """返回唯一原始变体。

        Args:
            analysis: QueryAnalyzer 结果。

        Returns:
            只含原始 query 的变体序列。

        """
        return (_variant(analysis.original_query, "original"),)


class RuleBasedNormalizer:
    """最多追加一个 NFKC/空白规范化变体。"""

    def expand(self, analysis: QueryAnalysis) -> tuple[QueryVariant, ...]:
        """原始 query 永远位于第一位并执行稳定去重。

        Args:
            analysis: QueryAnalyzer 结果。

        Returns:
            原始 query 加至多一个 normalized 变体。

        """
        variants = [_variant(analysis.original_query, "original")]
        if analysis.normalized_query != analysis.original_query:
            variants.append(_variant(analysis.normalized_query, "normalized"))
        return tuple(variants[:2])


def _variant(text: str, kind: str) -> QueryVariant:
    return QueryVariant(
        text=text,
        kind=kind,
        identity=canonical_sha256({"kind": kind, "text": text}),
    )


__all__ = ["NoopExpander", "RuleBasedNormalizer"]
