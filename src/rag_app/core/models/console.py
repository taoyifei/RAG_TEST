"""P10 控制台使用的只读 Revision、Chunk 与报告模型。"""

from __future__ import annotations

from pydantic import Field, StrictFloat, StrictInt

from rag_app.core.models.chunk import Chunk, ChunkingReport
from rag_app.core.models.common import FrozenModel, JsonObject
from rag_app.core.models.document import ParseReport
from rag_app.core.models.management import Job


class RevisionSlotCoverage(FrozenModel):
    """一个 Revision 的实际向量槽覆盖状态。"""

    slot_id: str = Field(min_length=1, max_length=80)
    vector_name: str = Field(min_length=1, max_length=160)
    required: bool
    expected_chunk_count: StrictInt = Field(ge=0)
    valid_vector_count: StrictInt = Field(ge=0)
    failed_count: StrictInt = Field(ge=0)
    coverage_ratio: StrictFloat = Field(ge=0.0, le=1.0)
    state: str = Field(min_length=1, max_length=80)


class RevisionActivation(FrozenModel):
    """一次原子激活的只读历史记录。"""

    old_revision_id: str | None = Field(
        default=None, pattern=r"^irev_[0-9a-f]{32}$"
    )
    new_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    activated_at: str
    reason: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")


class RevisionInspection(FrozenModel):
    """不含 Secret、向量或物理路径的 Revision 检查视图。"""

    revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    state: str = Field(min_length=1, max_length=80)
    active: bool
    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    serving_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    serving_compatibility_version: str = Field(min_length=1, max_length=80)
    expected_document_count: StrictInt = Field(ge=0)
    expected_chunk_count: StrictInt = Field(ge=0)
    actual_document_count: StrictInt = Field(ge=0)
    actual_chunk_count: StrictInt = Field(ge=0)
    fts_count: StrictInt = Field(ge=0)
    lexical_schema: JsonObject
    vector_schema: JsonObject
    chunk_payload_schema: str = Field(min_length=1, max_length=160)
    validation_report: JsonObject | None = None
    validation_evidence_hash: str | None = None
    writer_status: str = Field(min_length=1, max_length=80)
    created_at: str
    validated_at: str | None = None
    activated_at: str | None = None
    retired_at: str | None = None
    failure_code: str | None = None
    safe_message: str | None = Field(default=None, max_length=500)
    slot_coverages: tuple[RevisionSlotCoverage, ...] = ()
    activation_history: tuple[RevisionActivation, ...] = ()


class RevisionDocumentReport(FrozenModel):
    """一个 Revision 文档的解析与分块质量报告。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    parse_report: ParseReport
    chunking_report: ChunkingReport


class ChunkPage(FrozenModel):
    """带总数的 canonical Chunk 分页。"""

    items: tuple[Chunk, ...]
    total: StrictInt = Field(ge=0)
    page_size: StrictInt = Field(gt=0, le=200)
    offset: StrictInt = Field(ge=0)
    next_offset: StrictInt | None = Field(default=None, ge=0)


class JobPage(FrozenModel):
    """带总数的安全 Job 分页。"""

    items: tuple[Job, ...]
    total: StrictInt = Field(ge=0)
    page_size: StrictInt = Field(gt=0, le=200)
    offset: StrictInt = Field(ge=0)
    next_offset: StrictInt | None = Field(default=None, ge=0)


__all__ = [
    "ChunkPage",
    "JobPage",
    "RevisionActivation",
    "RevisionDocumentReport",
    "RevisionInspection",
    "RevisionSlotCoverage",
]
