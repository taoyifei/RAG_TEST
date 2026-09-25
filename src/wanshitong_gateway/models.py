"""湾事通外壳请求模型；问答内容交由原生 WeKnora 校验。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空。")
        return value
