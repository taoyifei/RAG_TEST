"""P07 可解释置信与拒答公共模型。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from rag_app.core.models.common import FrozenModel


class ConfidenceStatus(StrEnum):
    """查询可以发布或必须失败关闭的稳定结果。"""

    ANSWERABLE = "ANSWERABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    POLICY_DENIED = "POLICY_DENIED"
    INDEX_NOT_READY = "INDEX_NOT_READY"
    INDEX_CORRUPT = "INDEX_CORRUPT"
    AMBIGUOUS_NEEDS_CLARIFICATION = "AMBIGUOUS_NEEDS_CLARIFICATION"


class ConfidenceDecision(FrozenModel):
    """由可审计规则特征产生的 provisional 决策。"""

    status: ConfidenceStatus
    score: float = Field(ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = ()
    feature_values: tuple[tuple[str, float], ...] = ()
    provisional: bool = True


__all__ = ["ConfidenceDecision", "ConfidenceStatus"]
