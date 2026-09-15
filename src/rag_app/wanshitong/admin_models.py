"""湾事通管理员薄 Facade 的稳定响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, StrictInt

from rag_app.core.models.common import FrozenModel
from rag_app.core.models.management import DocumentStatus, Job
from rag_app.tracing.models import TraceMode, TraceStatus


class WanshitongDocumentView(FrozenModel):
    """固定 Scope 文档及其湾事通产品元数据。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    display_name: str = Field(min_length=1, max_length=512)
    relative_path: str | None = Field(default=None, max_length=4096)
    department: str | None = Field(default=None, max_length=200)
    category_path: tuple[str, ...] = ()
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


__all__ = [
    "WanshitongDocumentPage",
    "WanshitongDocumentView",
    "WanshitongTraceQuery",
    "WanshitongUploadReceipt",
]
