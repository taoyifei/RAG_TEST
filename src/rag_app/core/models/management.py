"""P09 项目、文档版本、作业与系统状态公共模型。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, StrictInt

from rag_app.core.models.common import FrozenModel, JsonObject
from rag_app.core.models.document import DocumentRef


class ProjectStatus(StrEnum):
    """项目生命周期状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class KnowledgeBaseStatus(StrEnum):
    """知识库生命周期状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETING = "deleting"


class DocumentStatus(StrEnum):
    """逻辑文档生命周期状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETING = "deleting"
    DELETED = "deleted"


class DocumentVersionStatus(StrEnum):
    """不可变文档版本的索引状态。"""

    CREATED = "created"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class JobStatus(StrEnum):
    """P09 对外稳定的作业状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


class Project(FrozenModel):
    """项目公共视图。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=200)
    status: ProjectStatus
    created_at: str
    updated_at: str


class KnowledgeBase(FrozenModel):
    """知识库公共视图。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    profile_id: str = Field(min_length=1, max_length=200)
    status: KnowledgeBaseStatus
    active_index_revision_id: str | None = Field(
        default=None,
        pattern=r"^irev_[0-9a-f]{32}$",
    )
    created_at: str
    updated_at: str


class Document(FrozenModel):
    """逻辑文档及当前版本公共视图。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    display_name: str = Field(min_length=1, max_length=512)
    status: DocumentStatus
    current_version_id: str | None = Field(
        default=None,
        pattern=r"^dver_[0-9a-f]{32}$",
    )
    active_index_revision_id: str | None = Field(
        default=None,
        pattern=r"^irev_[0-9a-f]{32}$",
    )
    created_at: str
    updated_at: str


class DocumentVersion(FrozenModel):
    """不可变文档版本及来源 Artifact 公共视图。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)
    status: DocumentVersionStatus
    created_at: str


class ArtifactDescriptor(FrozenModel):
    """经逻辑引用授权后的 Artifact 摘要。"""

    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    media_type: str = Field(min_length=1, max_length=200)
    size_bytes: StrictInt = Field(ge=0)
    role: str = Field(min_length=1, max_length=80)


class SlotProgress(FrozenModel):
    """单向量槽的可公开作业进度。"""

    slot_id: str = Field(min_length=1, max_length=80)
    completed: StrictInt = Field(ge=0)
    failed: StrictInt = Field(ge=0)
    total: StrictInt = Field(ge=0)


class Job(FrozenModel):
    """不暴露 fencing token 的持久化作业视图。"""

    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    document_id: str | None = Field(
        default=None,
        pattern=r"^doc_[0-9a-f]{32}$",
    )
    document_version_id: str | None = Field(
        default=None,
        pattern=r"^dver_[0-9a-f]{32}$",
    )
    revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    state: JobStatus
    stage: str = Field(min_length=1, max_length=100)
    attempt: StrictInt = Field(ge=0)
    retryable: bool
    safe_error: str | None = Field(default=None, max_length=500)
    lease_owner: bool
    fencing_safe_status: str = Field(min_length=1, max_length=80)
    slot_progress: tuple[SlotProgress, ...] = ()


class SystemStatus(FrozenModel):
    """不主动调用 Provider 的只读系统状态。"""

    profile_id: str
    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    serving_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    lexical_schema: str
    analyzer_id: str
    reindex_required: bool
    integrity_status: str
    pending_gc_items: StrictInt = Field(ge=0)
    reconciliation_summary: JsonObject
    remote_dense_confidence_calibrated: bool
    remote_production_profile_ready: bool
    components: tuple[JsonObject, ...]
    offline_evaluation_v3_ready: bool = True
    primary_live_evaluation_status: str = Field(
        default="not_verified", min_length=1, max_length=80
    )
    standby_live_evaluation_status: str = Field(
        default="not_verified", min_length=1, max_length=80
    )
    reranker_live_evaluation_status: str = Field(
        default="not_verified", min_length=1, max_length=80
    )
    lexical_analyzer_id: str = Field(
        default="deterministic-cjk-bigram-v2", min_length=1, max_length=160
    )
    active_revision_schema: str = Field(
        default="chunk-v3/fts-v2", min_length=1, max_length=160
    )
    runtime_identity: str = Field(
        default="legacy-runtime", min_length=1, max_length=160
    )
    active_profile_count: StrictInt = Field(default=0, ge=0)
    provider_validation_statuses: JsonObject = ()


class QueuedIngestionDocument(FrozenModel):
    """后台构建可恢复的无正文文档引用。"""

    document: DocumentRef
    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)


class QueuedIngestion(FrozenModel):
    """持久队列中的完整 Revision 构建请求。"""

    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    target_document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    target_document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    retrieval_profile_revision_id: str | None = Field(
        default=None,
        pattern=r"^pfr_[0-9a-f]{32}$",
    )
    documents: tuple[QueuedIngestionDocument, ...] = Field(min_length=1)


__all__ = [
    "ArtifactDescriptor",
    "Document",
    "DocumentStatus",
    "DocumentVersion",
    "DocumentVersionStatus",
    "Job",
    "JobStatus",
    "KnowledgeBase",
    "KnowledgeBaseStatus",
    "Project",
    "ProjectStatus",
    "QueuedIngestion",
    "QueuedIngestionDocument",
    "SlotProgress",
    "SystemStatus",
]
