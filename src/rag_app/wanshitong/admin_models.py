"""湾事通管理员薄 Facade 的稳定响应模型。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from rag_app.core.models.common import FrozenModel
from rag_app.core.models.management import DocumentStatus, Job
from rag_app.core.models.usage_audit import TrafficClass
from rag_app.tracing.models import TraceMode, TraceStatus
from rag_app.wanshitong.feedback import (
    FeedbackReviewStatus,
    FeedbackRootCause,
)

_TraceId = Annotated[str, StringConstraints(pattern=r"^trace_[0-9a-f]{32}$")]
_Reference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
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


class WanshitongTrafficOverrideItem(FrozenModel):
    """单条分类修正的乐观锁输入。"""

    trace_id: _TraceId
    expected_metadata_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class WanshitongTrafficOverrideRequest(FrozenModel):
    """有界批量分类修正，不接受问题正文或身份字段。"""

    items: tuple[WanshitongTrafficOverrideItem, ...] = Field(
        min_length=1, max_length=100
    )
    traffic_class: TrafficClass
    reason: str = Field(min_length=1, max_length=1000)


class RecommendationSource(BaseModel):
    """管理员实际核查的当前文档版本，不参与公共检索过滤。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")


class RecommendationCreateRequest(BaseModel):
    """从 F05 精确题组复制供编辑的公共候选。"""

    model_config = ConfigDict(extra="forbid")

    source_group_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    question_text: str = Field(min_length=1, max_length=500)
    question_style: Literal["SHORT", "STANDARD", "COMPOUND"] = "SHORT"
    topic_key: str = Field(min_length=1, max_length=100)


class RecommendationUpdateRequest(BaseModel):
    """全字段版本更新；批准必须同时确认题面、映射和来源。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    question_text: str = Field(min_length=1, max_length=500)
    question_style: Literal["SHORT", "STANDARD", "COMPOUND"]
    topic_key: str = Field(min_length=1, max_length=100)
    state: Literal["DRAFT", "APPROVED", "DISABLED", "NEEDS_REVIEW"]
    alias_keys: tuple[str, ...] = Field(max_length=20)
    validated_sources: tuple[RecommendationSource, ...] = Field(max_length=20)
    review_confirmed: bool = False
    disabled_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _validate_content(self) -> RecommendationUpdateRequest:
        if not self.question_text.strip() or not self.topic_key.strip():
            raise ValueError("题面与主题不能为空。")
        if len(set(self.alias_keys)) != len(self.alias_keys):
            raise ValueError("等义键不可重复。")
        if any(
            re.fullmatch(r"[0-9a-f]{64}", key) is None
            for key in self.alias_keys
        ):
            raise ValueError("问题键格式无效。")
        if (
            self.state == "DISABLED"
            and not (self.disabled_reason or "").strip()
        ):
            raise ValueError("下架须记录原因。")
        if self.state == "APPROVED" and (
            not self.review_confirmed
            or not self.alias_keys
            or not self.validated_sources
        ):
            raise ValueError("审核须确认题面、等义键和已核对资料版本。")
        return self


__all__ = [
    "RecommendationCreateRequest",
    "RecommendationSource",
    "RecommendationUpdateRequest",
    "WanshitongDocumentPage",
    "WanshitongDocumentView",
    "WanshitongFeedbackExportRequest",
    "WanshitongFeedbackQuery",
    "WanshitongFeedbackReviewRequest",
    "WanshitongTraceQuery",
    "WanshitongTrafficOverrideItem",
    "WanshitongTrafficOverrideRequest",
    "WanshitongUploadReceipt",
]
