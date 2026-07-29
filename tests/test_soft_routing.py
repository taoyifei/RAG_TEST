"""软路由只在高置信时缩小范围，存疑必须回退全库。"""

from datetime import UTC, datetime

from rag_app.retrieval.filters import MetadataPolicy
from rag_app.retrieval.routing import KeywordRouteRule, KeywordSoftRouter
from rag_app.tracing.reasons import DecisionCode


def test_high_confidence_route_adds_source_filter() -> None:
    """唯一高分路由可增加来源预过滤。"""
    router = KeywordSoftRouter(
        (
            KeywordRouteRule(
                route_id="procurement",
                keywords=("采购", "验收"),
                source_ids=("src_" + "1" * 32,),
            ),
        ),
        minimum_confidence=0.75,
    )

    decision = router.route("采购项目如何验收")
    query_filter = MetadataPolicy(
        allowed_statuses=("active",),
        allowed_authority_levels=("official",),
    ).to_qdrant_filter(
        as_of=datetime(2026, 7, 27, tzinfo=UTC),
        source_ids=decision.source_ids,
    )

    assert decision.routed is True
    assert decision.route_id == "procurement"
    assert decision.confidence == 1.0
    assert decision.reason_code is DecisionCode.UNIQUE_MATCH
    assert decision.rule_scores[0].matched_keywords == 2
    assert decision.rule_scores[0].coverage == 1.0
    assert query_filter.must[-1].key == "source_id"


def test_low_confidence_or_tie_falls_back_to_full_library() -> None:
    """低分或并列路由不得错误排除全库证据。"""
    router = KeywordSoftRouter(
        (
            KeywordRouteRule(
                route_id="a",
                keywords=("采购", "验收"),
                source_ids=("src_" + "1" * 32,),
            ),
            KeywordRouteRule(
                route_id="b",
                keywords=("采购", "付款"),
                source_ids=("src_" + "2" * 32,),
            ),
        ),
        minimum_confidence=0.75,
    )

    low = router.route("采购要求")
    tie = router.route("采购验收付款")

    assert low.routed is False
    assert low.source_ids == ()
    assert low.reason_code is DecisionCode.BELOW_THRESHOLD
    assert tie.routed is False
    assert tie.source_ids == ()
    assert tie.reason_code is DecisionCode.TIE


def test_empty_route_rules_report_no_rules() -> None:
    router = KeywordSoftRouter((), minimum_confidence=0.75)

    decision = router.route("任意问题")

    assert decision.routed is False
    assert decision.reason_code is DecisionCode.NO_RULES
