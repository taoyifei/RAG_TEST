"""湾事通外壳请求模型；问答内容交由原生 WeKnora 校验。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SearchScope = Literal["public", "hr_public", "personal", "hr_internal"]
SEARCH_SCOPE_ORDER: tuple[SearchScope, ...] = (
    "public",
    "hr_public",
    "personal",
    "hr_internal",
)


def normalize_search_scopes(values: Iterable[str]) -> tuple[SearchScope, ...]:
    """规范逻辑资料范围，公共范围已覆盖的公开人力项不重复保留。"""
    selected = set(values)
    if not selected or not selected.issubset(SEARCH_SCOPE_ORDER):
        raise ValueError("请选择有效的资料范围。")
    if "public" in selected:
        selected.discard("hr_public")
    return tuple(scope for scope in SEARCH_SCOPE_ORDER if scope in selected)


class GatewayChatRequest(BaseModel):
    """只校验外壳会话标识和空问题，不裁剪原生问题。"""

    model_config = ConfigDict(extra="forbid")

    query: str
    conversation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    client_context: object | None = None
    search_scopes: list[SearchScope] | None = Field(
        default=None, min_length=1, max_length=len(SEARCH_SCOPE_ORDER)
    )

    @field_validator("search_scopes")
    @classmethod
    def _normalize_search_scopes(
        cls, value: list[SearchScope] | None
    ) -> list[SearchScope] | None:
        if value is None:
            return None
        return list(normalize_search_scopes(value))

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空。")
        return value
