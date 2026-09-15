"""湾事通公共 API 的严格输入与裁剪响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from rag_app.product.feedback import FeedbackReason, ProjectionState

_MAX_CONVERSATION_QUERY_CHARS = 2000


class PublicRequest(BaseModel):
    """拒绝公共调用方未声明字段的基类。"""

    model_config = ConfigDict(extra="forbid")


class PublicSessionRequest(PublicRequest):
    """无登录公共会话不接受任何客户端身份参数。"""


class PublicSessionResponse(BaseModel):
    """页面可见的匿名会话摘要；不包含内部 owner。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=r"^wstsid_[0-9a-f]{32}$")
    csrf_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_in: int = Field(gt=0)


class PublicCapabilities(BaseModel):
    """公共前端可依赖的固定服务端能力。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["wanshitong"] = "wanshitong"
    stream: Literal[True] = True
    stream_protocol: Literal["wanshitong-public-sse-v1"] = (
        "wanshitong-public-sse-v1"
    )
    trace_mode: Literal["SAFE"] = "SAFE"
    history_mode: Literal["full"] = "full"
    document_visibility: Literal["all_internal"] = "all_internal"
    conversation_delete: Literal[True] = True
    feedback: Literal[True] = True


class PublicChatRequest(PublicRequest):
    """公共问答只接受问题与可选会话 ID。"""

    query: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("公共问题禁止仅含空白。")
        return value

    @model_validator(mode="after")
    def _bound_conversation_query(self) -> PublicChatRequest:
        if (
            self.conversation_id is not None
            and len(self.query) > _MAX_CONVERSATION_QUERY_CHARS
        ):
            raise ValueError("多轮会话的单轮问题不能超过 2000 字符。")
        return self


class PublicConversationClearResponse(BaseModel):
    """不暴露内部 owner 或固定 Scope 的会话清理回执。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    deleted: Literal[True] = True
    deleted_turns: int = Field(ge=0)


class PublicFeedbackRequest(PublicRequest):
    """将安全 Trace ID 与有限反馈信号提交给既有 Store。"""

    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    useful: bool
    reason_code: FeedbackReason | None = None

    @model_validator(mode="after")
    def _validate_reason(self) -> PublicFeedbackRequest:
        if self.useful and self.reason_code is not None:
            raise ValueError("有用反馈不应附带负向 reason_code。")
        return self


class PublicFeedbackResponse(BaseModel):
    """不暴露固定 Scope 与 owner 的反馈写入结果。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    useful: bool
    reason_code: FeedbackReason | None
    projection_state: ProjectionState
    updated_at: str


__all__ = [
    "PublicCapabilities",
    "PublicChatRequest",
    "PublicConversationClearResponse",
    "PublicFeedbackRequest",
    "PublicFeedbackResponse",
    "PublicSessionRequest",
    "PublicSessionResponse",
]
