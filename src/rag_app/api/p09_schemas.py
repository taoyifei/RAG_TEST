"""P09 HTTP 请求 DTO。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_app.core.models import KnowledgeBaseStatus, ProjectStatus
from rag_app.core.models.search import (
    RetrievalDiagnosticsSummary,
    SearchAnswerResult,
)

_MAX_CONVERSATION_QUERY_CHARS = 2000


class RequestModel(BaseModel):
    """拒绝未声明请求字段的基类。"""

    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(RequestModel):
    """创建项目请求。"""

    name: str = Field(
        min_length=1,
        max_length=200,
        description="项目显示名，不参与内容身份计算。",
    )


class UpdateProjectRequest(RequestModel):
    """更新项目请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: ProjectStatus | None = None


class CreateKnowledgeBaseRequest(RequestModel):
    """创建知识库请求。"""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class UpdateKnowledgeBaseRequest(RequestModel):
    """更新知识库请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: KnowledgeBaseStatus | None = None


class RenameDocumentRequest(RequestModel):
    """只修改文档显示信息的请求。"""

    display_name: str = Field(
        min_length=1,
        max_length=512,
        description="仅修改显示名，不创建版本或 IndexRevision。",
    )


class QueryRequest(RequestModel):
    """有界 Search/Answer 请求。"""

    query: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    limit: int = Field(default=10, ge=1, le=50)
    stream: bool = False
    stream_protocol: Literal["rag-answer-sse-v1"] | None = None
    include_related_content: bool = False
    history_mode: Literal["full", "metadata_only"] | None = None
    trace_mode: Literal["SAFE", "DIAGNOSTIC", "FULL"] = "SAFE"

    @model_validator(mode="after")
    def _validate_stream_negotiation(self) -> QueryRequest:
        """新协议只能在显式启用流式响应时协商。"""
        if self.stream_protocol is not None and not self.stream:
            raise ValueError("stream_protocol 需要 stream=true。")
        if (
            self.conversation_id is not None
            and len(self.query) > _MAX_CONVERSATION_QUERY_CHARS
        ):
            raise ValueError("多轮会话的单轮问题不能超过 2000 字符。")
        return self


class QueryResponse(SearchAnswerResult):
    """Search/Answer 的公开有界响应，不含完整诊断。"""

    query_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    vector_name: str | None = None
    dense_available: bool
    rerank_mode: str
    evidence_count: int = Field(ge=0)
    trace_summary: RetrievalDiagnosticsSummary | None = None
    quality_profile_status: Literal[
        "offline_validated_remote_uncalibrated",
        "remote_live_calibrated",
    ]


class ErrorDetail(RequestModel):
    """安全、稳定且可机器处理的错误明细。"""

    code: str
    message: str
    stage: str
    retryable: bool
    trace_id: str
    details: dict[str, object]


class ErrorEnvelope(RequestModel):
    """所有非成功响应的统一外层结构。"""

    error: ErrorDetail


__all__ = [
    "CreateKnowledgeBaseRequest",
    "CreateProjectRequest",
    "ErrorEnvelope",
    "QueryRequest",
    "QueryResponse",
    "RenameDocumentRequest",
    "UpdateKnowledgeBaseRequest",
    "UpdateProjectRequest",
]
