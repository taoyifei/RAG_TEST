"""公共入口提示的有界解析；提示不参与鉴权或问答语义。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from rag_app.core.models.usage_audit import Entrypoint

_RECOMMENDATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENTRYPOINTS = frozenset(
    {"manual", "suggestion", "popular", "retry", "unknown"}
)


@dataclass(frozen=True, slots=True)
class ClientUsageHint:
    """可供审计使用的有限客户端提示。"""

    entrypoint: Entrypoint = "unknown"
    source: str = "NO_HINT"
    recommendation_id: str | None = None
    diagnostic: str | None = None


def parse_client_context(value: object | None) -> ClientUsageHint:
    """把格式错误或未知提示收敛为 unknown，不影响问题请求。"""
    if value is None:
        return ClientUsageHint()
    if not isinstance(value, dict):
        return ClientUsageHint(
            source="INVALID_HINT", diagnostic="INVALID_SHAPE"
        )
    if set(value) - {"entrypoint", "recommendation_id"}:
        return ClientUsageHint(
            source="INVALID_HINT", diagnostic="UNEXPECTED_FIELD"
        )
    entrypoint = value.get("entrypoint")
    if not isinstance(entrypoint, str) or entrypoint not in _ENTRYPOINTS:
        return ClientUsageHint(
            source="INVALID_HINT", diagnostic="INVALID_ENTRYPOINT"
        )
    recommendation_id = value.get("recommendation_id")
    if recommendation_id is not None and (
        entrypoint not in {"suggestion", "popular"}
        or not isinstance(recommendation_id, str)
        or _RECOMMENDATION_ID.fullmatch(recommendation_id) is None
    ):
        return ClientUsageHint(
            source="INVALID_HINT", diagnostic="INVALID_RECOMMENDATION_ID"
        )
    return ClientUsageHint(
        entrypoint=cast(Entrypoint, entrypoint),
        source="CLIENT_HINT",
        recommendation_id=recommendation_id,
    )
