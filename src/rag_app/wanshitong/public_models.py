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
from rag_app.wanshitong.feedback import FeedbackReasonDetail

_MAX_CONVERSATION_QUERY_CHARS = 2000


class PublicRequest(BaseModel):
    """拒绝公共调用方未声明字段的基类。"""

    model_config = ConfigDict(extra="forbid")


class PublicSessionRequest(PublicRequest):
    """无登录公共会话不接受任何客户端身份参数。"""


class PublicSessionUser(BaseModel):
    """前端只需显示的最小 RDMS 用户摘要。"""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)


class PublicSessionResponse(BaseModel):
    """页面可见的会话摘要；不包含内部 owner 或授权 claims。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=r"^wstsid_[0-9a-f]{32}$")
    csrf_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_in: int = Field(gt=0)
    user: PublicSessionUser | None = None
    deployment_id: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$"
    )


class PublicShortcut(BaseModel):
    """只暴露可见快捷入口的展示字段。"""

    model_config = ConfigDict(extra="forbid")

    shortcut_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    revision: int = Field(ge=1)


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
    feedback_details: Literal[True] = True
    request_usage_context: Literal[True] = True
    shortcuts: tuple[PublicShortcut, ...] = ()


class PublicChatRequest(PublicRequest):
    """公共问答接受问题、会话 ID 和不可信的可选入口提示。"""

    query: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    client_context: object | None = None

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
    reason_detail: FeedbackReasonDetail | None = None
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _validate_reason(self) -> PublicFeedbackRequest:
        if self.useful and any(
            value is not None
            for value in (self.reason_code, self.reason_detail, self.comment)
        ):
            raise ValueError("有用反馈不应附带负向原因或说明。")
        return self


class PublicFeedbackResponse(BaseModel):
    """不暴露固定 Scope 与 owner 的反馈写入结果。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    useful: bool
    reason_code: FeedbackReason | None
    reason_detail: FeedbackReasonDetail | None
    comment_saved: bool
    feedback_revision: int = Field(ge=1)
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
    "PublicSessionUser",
    "PublicShortcut",
]
