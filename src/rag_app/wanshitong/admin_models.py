"""湾事通管理员薄 Facade 的稳定响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, StrictInt, StringConstraints, model_validator

from rag_app.core.models.common import FrozenModel
from rag_app.core.models.management import DocumentStatus, Job
from rag_app.tracing.models import TraceMode, TraceStatus
from rag_app.wanshitong.feedback import (
    FeedbackReviewStatus,
    FeedbackRootCause,
)

_TraceId = Annotated[
    str, StringConstraints(pattern=r"^trace_[0-9a-f]{32}$")
]
_Reference = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=500
    ),
]


class WanshitongDocumentView(FrozenModel):
    """固定 Scope 文档及其湾事通产品元数据。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    display_name: str = Field(min_length=1, max_length=512)
    relative_path: str | None = Field(default=None, max_length=4096)
    department: str | None = Field(default=None, max_length=200)
    department_key: str | None = Field(default=None, max_length=240)
    department_name: str | None = Field(default=None, max_length=200)
    category_path: tuple[str, ...] = ()
    document_title: str | None = Field(default=None, max_length=512)
    source_relative_path: str | None = Field(default=None, max_length=4096)
    topic_keys: tuple[str, ...] = ()
    visibility_scope: str = "all_internal"
    allowed_roles: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    metadata_revision: str | None = None
    status: DocumentStatus
    current_version_id: str | None = None
    current_version_status: str | None = None
    active_index_revision_id: str | None = None
    latest_job: Job | None = None
    retrievable: bool
    created_at: str
    updated_at: str


class WanshitongDocumentPage(FrozenModel):
    """固定 Scope 文档分页。"""

    items: tuple[WanshitongDocumentView, ...]
    total: StrictInt = Field(ge=0)
    page_size: StrictInt = Field(gt=0, le=200)
    offset: StrictInt = Field(ge=0)
    next_offset: StrictInt | None = Field(default=None, ge=0)
    next_cursor: str | None = None


class WanshitongUploadReceipt(FrozenModel):
    """单个 DOCX 请求对应的真实 Universal Job 回执。"""

    document: WanshitongDocumentView
    job: Job


class WanshitongTraceQuery(FrozenModel):
    """完整 Trace 页面允许使用的有界筛选参数。"""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)
    trace_id: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    kind: str | None = None
    status: TraceStatus | None = None
    request_id: str | None = None
    job_id: str | None = None
    document_id: str | None = None
    revision_id: str | None = None
    refusal_code: str | None = None
    error_code: str | None = None
    capture_mode: TraceMode | None = None
    capture_complete: bool | None = None
    feedback_useful: bool | None = None


class WanshitongFeedbackReviewRequest(FrozenModel):
    """管理员复核的乐观锁与有界字段。"""

    expected_version: int = Field(ge=0)
    review_status: FeedbackReviewStatus
    root_cause: FeedbackRootCause | None = None
    note: str | None = Field(default=None, max_length=2000)
    selected_source_document_id: str | None = Field(
        default=None, pattern=r"^doc_[0-9a-f]{32}$"
    )
    selected_source_version_id: str | None = Field(
        default=None, pattern=r"^dver_[0-9a-f]{32}$"
    )
    evaluation_candidate: bool = False
    fix_reference: str | None = Field(default=None, max_length=500)
    verification_references: tuple[_Reference, ...] = Field(
        default=(), max_length=20
    )

    @model_validator(mode="after")
    def _source_identity_is_complete(self) -> WanshitongFeedbackReviewRequest:
        if (self.selected_source_document_id is None) != (
            self.selected_source_version_id is None
        ):
            raise ValueError("选中来源必须同时提供文档和版本 ID。")
        return self


class WanshitongFeedbackExportRequest(FrozenModel):
    """可选限定 Trace 的安全元数据导出请求。"""

    trace_ids: tuple[_TraceId, ...] = Field(default=(), max_length=1000)


class WanshitongFeedbackQuery(FrozenModel):
    """反馈待办列表的有界筛选和分页。"""

    reason: str | None = Field(default=None, max_length=40)
    review_status: FeedbackReviewStatus | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    page_size: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


__all__ = [
    "WanshitongDocumentPage",
    "WanshitongDocumentView",
    "WanshitongFeedbackExportRequest",
    "WanshitongFeedbackQuery",
    "WanshitongFeedbackReviewRequest",
    "WanshitongTraceQuery",
    "WanshitongUploadReceipt",
]
