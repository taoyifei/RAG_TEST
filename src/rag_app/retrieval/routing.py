"""可配置关键词软路由与强制全库回退。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from rag_app.tracing.reasons import DecisionCode

__all__ = [
    "KeywordRouteRule",
    "KeywordSoftRouter",
    "RouteRuleScore",
    "SoftRouteDecision",
    "SoftRouter",
]


@dataclass(frozen=True, slots=True)
class KeywordRouteRule:
    """一个由冻结关键词指向稳定来源 ID 的软路由规则。"""

    route_id: str
    keywords: tuple[str, ...]
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝空值和重复值。"""
        if not self.route_id:
            raise ValueError("route_id 不能为空。")
        _require_unique_nonempty("keywords", self.keywords)
        _require_unique_nonempty("source_ids", self.source_ids)


@dataclass(frozen=True, slots=True)
class SoftRouteDecision:
    """一次路由决策及是否已回退全库。"""

    route_id: str | None
    source_ids: tuple[str, ...]
    confidence: float
    routed: bool
    reason_code: DecisionCode = DecisionCode.FALLBACK_FULL_CORPUS
    rule_scores: tuple[RouteRuleScore, ...] = ()
    threshold: float = 0.0


@dataclass(frozen=True, slots=True)
class RouteRuleScore:
    """一条路由规则的确定性关键词命中明细。"""

    route_id: str
    source_ids: tuple[str, ...]
    matched_keywords: int
    keyword_count: int
    coverage: float


class SoftRouter(Protocol):
    """查询链使用的最小软路由接口。"""

    def route(self, question: str) -> SoftRouteDecision:
        """返回高置信来源范围或空范围全库回退。

        Args:
            question: 当前用户问题。

        Returns:
            软路由范围、置信度与回退状态。

        """


class KeywordSoftRouter:
    """按冻结关键词覆盖率路由，并在低分或并列时回退全库。"""

    def __init__(
        self,
        rules: tuple[KeywordRouteRule, ...],
        *,
        minimum_confidence: float,
    ) -> None:
        """保存规则与由冻结集确定的置信度阈值。

        Args:
            rules: 路由规则；允许为空，此时始终检索全库。
            minimum_confidence: 唯一最高分必须达到的下限。

        Raises:
            ValueError: 阈值越界或 route ID 重复。

        """
        if not 0.0 < minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence 必须在 (0, 1]。")
        route_ids = tuple(rule.route_id for rule in rules)
        if len(set(route_ids)) != len(route_ids):
            raise ValueError("route_id 不能重复。")
        self._rules = rules
        self._minimum_confidence = minimum_confidence

    def route(self, question: str) -> SoftRouteDecision:
        """仅在唯一最高分达到阈值时缩小来源范围。

        Args:
            question: 当前用户问题。

        Returns:
            唯一高置信规则或全库回退决策。

        """
        normalized = question.casefold()
        scored = tuple(
            RouteRuleScore(
                route_id=rule.route_id,
                source_ids=rule.source_ids,
                matched_keywords=sum(
                    keyword.casefold() in normalized
                    for keyword in rule.keywords
                ),
                keyword_count=len(rule.keywords),
                coverage=sum(
                    keyword.casefold() in normalized
                    for keyword in rule.keywords
                )
                / len(rule.keywords),
            )
            for rule in self._rules
        )
        if not scored:
            return _fallback(
                0.0,
                reason=DecisionCode.NO_RULES,
                scores=(),
                threshold=self._minimum_confidence,
            )
        best_score = max(score.coverage for score in scored)
        winners = tuple(
            score for score in scored if score.coverage == best_score
        )
        if best_score < self._minimum_confidence:
            return _fallback(
                best_score,
                reason=DecisionCode.BELOW_THRESHOLD,
                scores=scored,
                threshold=self._minimum_confidence,
            )
        if len(winners) != 1:
            return _fallback(
                best_score,
                reason=DecisionCode.TIE,
                scores=scored,
                threshold=self._minimum_confidence,
            )
        winner = winners[0]
        return SoftRouteDecision(
            route_id=winner.route_id,
            source_ids=winner.source_ids,
            confidence=best_score,
            routed=True,
            reason_code=DecisionCode.UNIQUE_MATCH,
            rule_scores=scored,
            threshold=self._minimum_confidence,
        )


def _fallback(
    confidence: float,
    *,
    reason: DecisionCode,
    scores: tuple[RouteRuleScore, ...],
    threshold: float,
) -> SoftRouteDecision:
    return SoftRouteDecision(
        route_id=None,
        source_ids=(),
        confidence=confidence,
        routed=False,
        reason_code=reason,
        rule_scores=scores,
        threshold=threshold,
    )


def _require_unique_nonempty(
    field_name: str,
    values: tuple[str, ...],
) -> None:
    if not values or any(not value for value in values):
        raise ValueError(f"{field_name} 必须包含非空值。")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} 不能重复。")
