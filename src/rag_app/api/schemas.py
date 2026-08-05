"""查询与索引 API 的严格输入契约。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from rag_app.state.models import JobKind

__all__ = [
    "ChatRequest",
    "CreateJobRequest",
    "FeedbackRequest",
    "TraceExportRequest",
]

_TraceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class ChatRequest(BaseModel):
    """一次有界问答请求。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    conversation_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)


class CreateJobRequest(BaseModel):
    """幂等创建全量或增量索引任务。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=256)
    kind: JobKind


class FeedbackRequest(BaseModel):
    """一次不含业务内容的回答有用性反馈。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    useful: bool


class TraceExportRequest(BaseModel):
    """一次批量 Trace 导出请求。"""

    model_config = ConfigDict(extra="forbid")

    trace_ids: list[_TraceId] = Field(min_length=1, max_length=100)
