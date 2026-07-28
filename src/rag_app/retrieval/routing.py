"""可配置关键词软路由与强制全库回退。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "KeywordRouteRule",
    "KeywordSoftRouter",
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
            (
                sum(
                    keyword.casefold() in normalized
                    for keyword in rule.keywords
                )
                / len(rule.keywords),
                rule,
            )
            for rule in self._rules
        )
        if not scored:
            return _fallback(0.0)
        best_score = max(score for score, _ in scored)
        winners = tuple(
            rule for score, rule in scored if score == best_score
        )
        if (
            best_score < self._minimum_confidence
            or len(winners) != 1
        ):
            return _fallback(best_score)
        winner = winners[0]
        return SoftRouteDecision(
            route_id=winner.route_id,
            source_ids=winner.source_ids,
            confidence=best_score,
            routed=True,
        )


def _fallback(confidence: float) -> SoftRouteDecision:
    return SoftRouteDecision(
        route_id=None,
        source_ids=(),
        confidence=confidence,
        routed=False,
    )


def _require_unique_nonempty(
    field_name: str,
    values: tuple[str, ...],
) -> None:
    if not values or any(not value for value in values):
        raise ValueError(f"{field_name} 必须包含非空值。")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} 不能重复。")
