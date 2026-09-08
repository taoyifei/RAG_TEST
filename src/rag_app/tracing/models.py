"""Query Trace v1 的不可变持久契约。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from rag_app.tracing.reasons import DecisionCode

__all__ = [
    "ArtifactContent",
    "ArtifactMetadata",
    "CandidateDecision",
    "DecisionCode",
    "JsonValue",
    "SpanKind",
    "SpanRecord",
    "SpanStatus",
    "TraceDetail",
    "TraceFinish",
    "TraceIdentity",
    "TraceListFilter",
    "TraceMode",
    "TracePage",
    "TraceRecord",
    "TraceStatus",
]

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_TRACE_ID_PATTERN = re.compile(r"^(?:trace_)?[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_TRACE_PAGE_SIZE = 200


class TraceMode(StrEnum):
    """Trace 内容边界。"""

    SAFE = "SAFE"
    DIAGNOSTIC = "DIAGNOSTIC"
    FULL = "FULL"


class TraceStatus(StrEnum):
    """根 Trace 生命周期状态。"""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    ANSWERED = "ANSWERED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class SpanStatus(StrEnum):
    """单个 span 生命周期状态。"""

    RUNNING = "RUNNING"
    OK = "OK"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    INTERRUPTED = "INTERRUPTED"


class SpanKind(StrEnum):
    """可映射到 OpenTelemetry 的 span 类别。"""

    CHAIN = "CHAIN"
    RETRIEVER = "RETRIEVER"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    LLM = "LLM"
    GUARDRAIL = "GUARDRAIL"
    STORAGE = "STORAGE"
    HTTP = "HTTP"


@dataclass(frozen=True, slots=True)
class TraceFinish:
    """关闭根 Trace 所需的终态字段。"""

    status: TraceStatus
    finished_at: datetime
    refusal_code: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        """校验终态和结束时间。

        Raises:
            ValueError: 状态仍为 RUNNING 或结束时间不带时区。

        """
        if self.status is TraceStatus.RUNNING:
            raise ValueError("Trace 终态不能是 RUNNING。")
        _require_aware("finished_at", self.finished_at)


@dataclass(frozen=True, slots=True)
class TraceIdentity:
    """根 Trace 绑定的运行与索引版本身份。"""

    pipeline_fingerprint: str
    serving_fingerprint: str
    release_revision: str
    active_collection: str
    index_manifest_sha256: str
    payload_schema_version: int
    kind: str = "query"
    project_id: str | None = None
    knowledge_base_id: str | None = None
    owner_sha256: str | None = None
    request_id: str | None = None
    job_id: str | None = None
    document_id: str | None = None
    revision_id: str | None = None
    profile_id: str | None = None
    index_fingerprint: str | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        """校验版本身份字段。

        Raises:
            ValueError: 指纹、摘要、字符串或 schema 版本无效。

        """
        _require_pattern(
            "pipeline_fingerprint",
            self.pipeline_fingerprint,
            _FINGERPRINT_PATTERN,
        )
        _require_pattern(
            "serving_fingerprint",
            self.serving_fingerprint,
            _FINGERPRINT_PATTERN,
        )
        _require_pattern(
            "index_manifest_sha256",
            self.index_manifest_sha256,
            _SHA256_PATTERN,
        )
        _require_nonempty("release_revision", self.release_revision)
        _require_nonempty("active_collection", self.active_collection)
        _require_nonempty("kind", self.kind)
        for name, value in (
            ("project_id", self.project_id),
            ("knowledge_base_id", self.knowledge_base_id),
            ("request_id", self.request_id),
            ("job_id", self.job_id),
            ("document_id", self.document_id),
            ("revision_id", self.revision_id),
            ("profile_id", self.profile_id),
            ("source_revision", self.source_revision),
        ):
            if value is not None:
                _require_nonempty(name, value)
        if self.owner_sha256 is not None:
            _require_pattern("owner_sha256", self.owner_sha256, _SHA256_PATTERN)
        if self.index_fingerprint is not None:
            _require_pattern(
                "index_fingerprint",
                self.index_fingerprint,
                _FINGERPRINT_PATTERN,
            )
        if self.payload_schema_version <= 0:
            raise ValueError("payload_schema_version 必须为正整数。")


@dataclass(frozen=True, slots=True)
class TraceListFilter:
    """有界 Trace 列表查询条件。"""

    page: int = 1
    page_size: int = 50
    trace_id: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    status: TraceStatus | None = None
    refusal_code: str | None = None
    error_code: str | None = None
    feedback_useful: bool | None = None
    kind: str | None = None
    project_id: str | None = None
    knowledge_base_id: str | None = None
    request_id: str | None = None
    job_id: str | None = None
    document_id: str | None = None
    revision_id: str | None = None
    capture_mode: TraceMode | None = None
    capture_complete: bool | None = None

    def __post_init__(self) -> None:
        """校验分页和时间过滤边界。

        Raises:
            ValueError: 分页越界、时间无时区或时间范围倒置。

        """
        if self.page <= 0 or not 0 < self.page_size <= _MAX_TRACE_PAGE_SIZE:
            raise ValueError("Trace page/page_size 超出固定边界。")
        if self.trace_id is not None:
            _require_pattern("trace_id", self.trace_id, _TRACE_ID_PATTERN)
        for name, value in (
            ("kind", self.kind),
            ("project_id", self.project_id),
            ("knowledge_base_id", self.knowledge_base_id),
            ("request_id", self.request_id),
            ("job_id", self.job_id),
            ("document_id", self.document_id),
            ("revision_id", self.revision_id),
        ):
            if value is not None:
                _require_nonempty(name, value)
        if self.created_from is not None:
            _require_aware("created_from", self.created_from)
        if self.created_to is not None:
            _require_aware("created_to", self.created_to)
        if (
            self.created_from is not None
            and self.created_to is not None
            and self.created_from > self.created_to
        ):
            raise ValueError("created_from 不能晚于 created_to。")


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """一次 Query Trace 的根持久记录。"""

    trace_id: str
    schema_version: str
    mode: TraceMode
    created_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    pipeline_fingerprint: str
    serving_fingerprint: str
    release_revision: str
    active_collection: str
    index_manifest_sha256: str
    payload_schema_version: int
    status: TraceStatus
    refusal_code: str | None
    error_code: str | None
    feedback_useful: bool | None
    capture_complete: bool
    expires_at: datetime
    kind: str = "query"
    project_id: str | None = None
    knowledge_base_id: str | None = None
    owner_sha256: str | None = None
    request_id: str | None = None
    job_id: str | None = None
    document_id: str | None = None
    revision_id: str | None = None
    profile_id: str | None = None
    index_fingerprint: str | None = None
    source_revision: str | None = None
    capture_incomplete_reason: str | None = None
    dropped_span_count: int = 0
    dropped_decision_count: int = 0
    writer_queue_high_water: int = 0

    def __post_init__(self) -> None:
        """校验根 Trace 的稳定身份、时间和终态一致性。"""
        _require_pattern("trace_id", self.trace_id, _TRACE_ID_PATTERN)
        _require_nonempty("schema_version", self.schema_version)
        _require_aware("created_at", self.created_at)
        _require_aware("expires_at", self.expires_at)
        _require_pattern(
            "pipeline_fingerprint",
            self.pipeline_fingerprint,
            _FINGERPRINT_PATTERN,
        )
        _require_pattern(
            "serving_fingerprint",
            self.serving_fingerprint,
            _FINGERPRINT_PATTERN,
        )
        _require_pattern(
            "index_manifest_sha256",
            self.index_manifest_sha256,
            _SHA256_PATTERN,
        )
        for required_name, required_value in (
            ("release_revision", self.release_revision),
            ("active_collection", self.active_collection),
            ("kind", self.kind),
        ):
            _require_nonempty(required_name, required_value)
        for optional_name, optional_value in (
            ("project_id", self.project_id),
            ("knowledge_base_id", self.knowledge_base_id),
            ("request_id", self.request_id),
            ("job_id", self.job_id),
            ("document_id", self.document_id),
            ("revision_id", self.revision_id),
            ("profile_id", self.profile_id),
            ("source_revision", self.source_revision),
            ("capture_incomplete_reason", self.capture_incomplete_reason),
        ):
            if optional_value is not None:
                _require_nonempty(optional_name, optional_value)
        if self.owner_sha256 is not None:
            _require_pattern("owner_sha256", self.owner_sha256, _SHA256_PATTERN)
        if self.index_fingerprint is not None:
            _require_pattern(
                "index_fingerprint",
                self.index_fingerprint,
                _FINGERPRINT_PATTERN,
            )
        if (
            min(
                self.dropped_span_count,
                self.dropped_decision_count,
                self.writer_queue_high_water,
            )
            < 0
        ):
            raise ValueError("Trace 捕获计数不能为负数。")
        if self.payload_schema_version <= 0:
            raise ValueError("payload_schema_version 必须为正数。")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at 必须晚于 created_at。")
        if self.status is TraceStatus.RUNNING:
            if self.finished_at is not None or self.duration_ms is not None:
                raise ValueError("RUNNING Trace 不能包含结束时间。")
        else:
            _require_terminal_times(
                self.created_at,
                self.finished_at,
                self.duration_ms,
            )

    def as_dict(self) -> dict[str, object]:
        """返回保留 Python 类型的字段映射。

        Args:
            无参数；读取当前不可变记录。

        Returns:
            可用于复制构造的字段映射。

        """
        return {
            field: getattr(self, field) for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """一个具有稳定父子关系和独立耗时的 Query span。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    sequence: int
    name: str
    kind: SpanKind
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    status: SpanStatus
    reason_code: DecisionCode
    attributes: dict[str, JsonValue]
    input_artifact_id: str | None
    output_artifact_id: str | None

    def __post_init__(self) -> None:
        """校验 span 身份、时间、状态和 artifact 引用。"""
        _require_pattern("trace_id", self.trace_id, _TRACE_ID_PATTERN)
        _require_pattern("span_id", self.span_id, _SPAN_ID_PATTERN)
        if self.parent_span_id is not None:
            _require_pattern(
                "parent_span_id",
                self.parent_span_id,
                _SPAN_ID_PATTERN,
            )
            if self.parent_span_id == self.span_id:
                raise ValueError("span 不能以自身作为 parent。")
        if self.sequence <= 0:
            raise ValueError("span sequence 必须为正数。")
        _require_nonempty("span name", self.name)
        _require_aware("started_at", self.started_at)
        for name, artifact_id in (
            ("input_artifact_id", self.input_artifact_id),
            ("output_artifact_id", self.output_artifact_id),
        ):
            if artifact_id is not None:
                _require_pattern(name, artifact_id, _ARTIFACT_ID_PATTERN)
        if self.status is SpanStatus.RUNNING:
            if self.finished_at is not None or self.duration_ms is not None:
                raise ValueError("RUNNING span 不能包含结束时间。")
        else:
            _require_terminal_times(
                self.started_at,
                self.finished_at,
                self.duration_ms,
            )

    def as_dict(self) -> dict[str, object]:
        """返回保留 Python 类型的字段映射。

        Args:
            无参数；读取当前不可变记录。

        Returns:
            可用于复制构造的字段映射。

        """
        return {
            field: getattr(self, field) for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """候选在某一机械阶段的选择或淘汰记录。"""

    trace_id: str
    sequence: int
    stage: str
    chunk_id: str
    selected: bool
    reason_code: DecisionCode
    details: dict[str, JsonValue]
    candidate_id: str | None = None
    evidence_id: str | None = None
    channel: str | None = None
    rank: int | None = None
    score_type: str | None = None
    score: float | None = None
    contribution: float | None = None

    def __post_init__(self) -> None:
        """拒绝不稳定身份或空候选字段。"""
        _require_pattern("trace_id", self.trace_id, _TRACE_ID_PATTERN)
        if self.sequence <= 0:
            raise ValueError("decision sequence 必须为正数。")
        _require_nonempty("decision stage", self.stage)
        _require_nonempty("chunk_id", self.chunk_id)
        for identity_name, identity_value in (
            ("candidate_id", self.candidate_id),
            ("evidence_id", self.evidence_id),
            ("channel", self.channel),
            ("score_type", self.score_type),
        ):
            if identity_value is not None:
                _require_nonempty(identity_name, identity_value)
        if self.rank is not None and self.rank <= 0:
            raise ValueError("candidate rank 必须为正数。")
        for score_name, score_value in (
            ("score", self.score),
            ("contribution", self.contribution),
        ):
            if score_value is not None and not math.isfinite(score_value):
                raise ValueError(f"{score_name} 必须为有限值。")


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """压缩 artifact 的完整性与容量元数据。"""

    artifact_id: str
    trace_id: str
    kind: str
    media_type: str
    sha256: str
    original_bytes: int
    compressed_bytes: int
    created_at: datetime

    def __post_init__(self) -> None:
        """校验 artifact 身份、摘要和非负容量。"""
        _require_pattern(
            "artifact_id",
            self.artifact_id,
            _ARTIFACT_ID_PATTERN,
        )
        _require_pattern("trace_id", self.trace_id, _TRACE_ID_PATTERN)
        _require_pattern("sha256", self.sha256, _SHA256_PATTERN)
        _require_nonempty("artifact kind", self.kind)
        _require_nonempty("artifact media_type", self.media_type)
        _require_aware("artifact created_at", self.created_at)
        if self.original_bytes <= 0 or self.compressed_bytes <= 0:
            raise ValueError("artifact 字节数必须为正数。")


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """已完成摘要校验和解压的 artifact。"""

    metadata: ArtifactMetadata
    payload: bytes


@dataclass(frozen=True, slots=True)
class TraceDetail:
    """管理员详情页所需的一棵完整 Trace 树。"""

    trace: TraceRecord
    spans: tuple[SpanRecord, ...]
    candidate_decisions: tuple[CandidateDecision, ...]
    artifacts: tuple[ArtifactMetadata, ...]


@dataclass(frozen=True, slots=True)
class TracePage:
    """有界且稳定排序的 Trace 列表页。"""

    items: tuple[TraceRecord, ...]
    page: int
    page_size: int
    total: int


def _require_pattern(
    name: str,
    value: str,
    pattern: re.Pattern[str],
) -> None:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{name} 格式无效。")


def _require_nonempty(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} 不能为空。")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须带时区。")


def _require_terminal_times(
    started_at: datetime,
    finished_at: datetime | None,
    duration_ms: int | None,
) -> None:
    if finished_at is None or duration_ms is None:
        raise ValueError("终态必须包含 finished_at 和 duration_ms。")
    _require_aware("finished_at", finished_at)
    expected = max(
        0,
        round((finished_at - started_at).total_seconds() * 1000),
    )
    if duration_ms != expected:
        raise ValueError("duration_ms 与开始/结束时间不一致。")
