"""不影响普通查询的有界单 writer Trace 录制器。"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast

from rag_app.tracing.exporter import NullTraceExporter, TraceExporter
from rag_app.tracing.models import (
    ArtifactMetadata,
    CandidateDecision,
    JsonValue,
    SpanKind,
    SpanRecord,
    SpanStatus,
    TraceFinish,
    TraceIdentity,
    TraceMode,
    TraceRecord,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.store import (
    TraceArtifactLimitError,
    TraceStore,
)

__all__ = [
    "TraceRecorder",
    "TraceRecorderConfig",
    "TraceSession",
    "TraceSpanFinish",
    "TraceSpanHandle",
    "TraceSpanSpec",
    "TraceUnavailableError",
]

_LOGGER = logging.getLogger(__name__)
_STOP: Final = object()
_DEFAULT_QUEUE_SIZE = 256
_DEFAULT_WAIT_SECONDS = 5.0
_DEFAULT_PRUNE_INTERVAL_SECONDS = 300.0
_DEFAULT_MAINTENANCE_IDLE_GRACE_SECONDS = 0.25
_MAINTENANCE_RETRY_SECONDS = 0.25
_DEFAULT_FULL_RESERVATION_BYTES = 1024 * 1024
_BUFFERED_SPAN_LIMIT = 512
_BUFFERED_DECISION_LIMIT = 4096
_BUFFERED_STAGE_DECISION_LIMIT = 512


class TraceUnavailableError(RuntimeError):
    """FULL Debug 查询开始前 Trace 捕获不可用。"""


@dataclass(frozen=True, slots=True)
class TraceRecorderConfig:
    """Trace writer 的固定资源边界。"""

    queue_size: int = _DEFAULT_QUEUE_SIZE
    wait_seconds: float = _DEFAULT_WAIT_SECONDS
    prune_interval_seconds: float = _DEFAULT_PRUNE_INTERVAL_SECONDS
    maintenance_idle_grace_seconds: float = (
        _DEFAULT_MAINTENANCE_IDLE_GRACE_SECONDS
    )
    full_artifact_reservation_bytes: int = _DEFAULT_FULL_RESERVATION_BYTES

    def __post_init__(self) -> None:
        """校验所有资源边界为正数。

        Raises:
            ValueError: 任一资源边界不为正数。

        """
        if (
            self.queue_size <= 0
            or self.wait_seconds <= 0
            or self.prune_interval_seconds <= 0
            or self.maintenance_idle_grace_seconds <= 0
            or self.full_artifact_reservation_bytes <= 0
        ):
            raise ValueError("Trace writer 边界必须为正数。")


@dataclass(slots=True)
class _WriteCommand:
    """writer 线程执行的一次有界操作。"""

    trace_id: str
    action: Callable[[], object]
    completion: threading.Event | None = None
    results: list[object] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TraceSpanHandle:
    """当前查询线程持有的 span 计时状态。"""

    span_id: str
    parent_span_id: str | None
    sequence: int
    name: str
    kind: SpanKind
    started_at: datetime
    started_tick: float


@dataclass(frozen=True, slots=True)
class TraceSpanFinish:
    """关闭活动 span 的终态字段。"""

    status: SpanStatus
    reason_code: DecisionCode
    attributes: dict[str, object] | None = None
    input_artifact_id: str | None = None
    output_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class TraceSpanSpec:
    """已完成确定性子 span 的字段。"""

    name: str
    kind: SpanKind
    parent_span_id: str
    reason_code: DecisionCode
    attributes: dict[str, object] | None = None
    duration_ms: int = 0
    started_offset_ms: int | None = None


@dataclass(slots=True)
class _SpanTimeline:
    """会话内单个 span 的层级和关闭状态。"""

    handle: TraceSpanHandle
    children: set[str] = field(default_factory=set)
    finished_at: datetime | None = None


class TraceSession:
    """一次查询的有序 span、决策和 FULL artifact 录制上下文。"""

    def __init__(  # noqa: PLR0913
        self,
        recorder: TraceRecorder,
        trace: TraceRecord,
        *,
        clock: Callable[[], float] = time.monotonic,
        root_attributes: dict[str, object] | None = None,
        buffer_writes: bool = False,
        completed_elapsed_ms: int | None = None,
    ) -> None:
        """开始根 Trace 和 `rag.query` span。

        Args:
            recorder: 单 writer Trace 录制器。
            trace: 已校验的 RUNNING 根记录。
            clock: span 独立耗时使用的单调时钟。
            root_attributes: 只包含本次查询安全摘要的根 span 属性。
            buffer_writes: 是否把非 FULL Trace 合并为一个 writer 批次。
            completed_elapsed_ms: 延迟构造时已完成查询的实际毫秒数。

        """
        self._recorder = recorder
        self.trace = trace
        self._clock = clock
        self._wall_anchor = trace.created_at
        self._monotonic_anchor = clock()
        self._last_elapsed_ms = 0
        self._completed_elapsed_ms = completed_elapsed_ms
        self._spans: dict[str, _SpanTimeline] = {}
        self._sequence = 0
        self._decision_sequence = 0
        self._finished = False
        self._root_attributes = dict(root_attributes or {})
        if buffer_writes and trace.mode is TraceMode.FULL:
            raise ValueError("FULL Trace 不允许延迟持久化。")
        self._buffer_writes = buffer_writes
        self._buffered_spans: dict[str, SpanRecord] = {}
        self._buffered_decisions: list[CandidateDecision] = []
        self._buffered_decision_stages: dict[str, int] = {}
        self._buffered_dropped_spans = 0
        self._buffered_dropped_decisions = 0
        self._identity_updates: dict[str, str] = {}
        if completed_elapsed_ms is not None and completed_elapsed_ms < 0:
            raise ValueError("已完成 Trace 的实际耗时不能为负数。")
        if not buffer_writes:
            recorder.begin_trace(trace)
        if completed_elapsed_ms is None:
            self.root = self.start_span(
                f"rag.{trace.kind}",
                SpanKind.CHAIN,
                parent_span_id=None,
                attributes=root_attributes,
            )
        else:
            self.root = self._start_completed_root(root_attributes)

    @property
    def strict(self) -> bool:
        """返回当前会话是否要求 FULL fail-closed。

        Args:
            无参数；读取根 Trace 模式。

        Returns:
            FULL 模式返回真。

        """
        return self.trace.mode is TraceMode.FULL

    @property
    def buffered(self) -> bool:
        """返回当前 Trace 是否在请求完成时批量提交。

        Args:
            无参数；读取当前会话写入策略。

        Returns:
            完成时批量写入为 True，否则为 False。

        """
        return self._buffer_writes

    def start_span(
        self,
        name: str,
        kind: SpanKind,
        *,
        parent_span_id: str | None,
        attributes: dict[str, object] | None = None,
        historical_duration_ms: int | None = None,
    ) -> TraceSpanHandle:
        """开始并尽力持久化一个 RUNNING span。

        Args:
            name: 稳定逻辑阶段名。
            kind: 可映射到 OpenTelemetry 的类别。
            parent_span_id: 父 span ID；根 span 使用空值。
            attributes: 不含正文、secret 或大对象的安全属性。
            historical_duration_ms: 已完成外部调用随后补录父阶段时，向前恢复的
                实际持续时间；不会早于父 span 起点。

        Returns:
            供关闭阶段使用的计时状态。

        Raises:
            RuntimeError: 会话或父 span 已关闭，或父 span 不存在。

        """
        if self._finished:
            raise RuntimeError("TraceSession 已关闭，不能创建 span。")
        parent = self._open_parent(parent_span_id)
        self._sequence += 1
        started_at, started_tick = self._timeline_now()
        safe_attributes = dict(attributes or {})
        if historical_duration_ms is not None:
            if historical_duration_ms < 0:
                raise ValueError("历史 span 持续时间不能为负数。")
            if parent is None:
                raise ValueError("根 span 不能使用历史持续时间。")
            available_ms = _duration_ms(parent.handle.started_at, started_at)
            restored_ms = min(historical_duration_ms, available_ms)
            started_at -= timedelta(milliseconds=restored_ms)
            started_tick -= restored_ms / 1000
            if restored_ms != historical_duration_ms:
                safe_attributes["reported_historical_duration_ms"] = (
                    historical_duration_ms
                )
        active = TraceSpanHandle(
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent_span_id,
            sequence=self._sequence,
            name=name,
            kind=kind,
            started_at=started_at,
            started_tick=started_tick,
        )
        self._put_span(
            SpanRecord(
                trace_id=self.trace.trace_id,
                span_id=active.span_id,
                parent_span_id=parent_span_id,
                sequence=active.sequence,
                name=name,
                kind=kind,
                started_at=started_at,
                finished_at=None,
                duration_ms=None,
                status=SpanStatus.RUNNING,
                reason_code=DecisionCode.STARTED,
                attributes=_json_attributes(safe_attributes),
                input_artifact_id=None,
                output_artifact_id=None,
            )
        )
        self._spans[active.span_id] = _SpanTimeline(handle=active)
        if parent is not None:
            parent.children.add(active.span_id)
        return active

    def _start_completed_root(
        self,
        attributes: dict[str, object] | None,
    ) -> TraceSpanHandle:
        """为后台延迟投影建立从真实请求起点开始的根 span。"""
        self._sequence += 1
        active = TraceSpanHandle(
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=None,
            sequence=self._sequence,
            name=f"rag.{self.trace.kind}",
            kind=SpanKind.CHAIN,
            started_at=self._wall_anchor,
            started_tick=self._monotonic_anchor,
        )
        self._put_span(
            SpanRecord(
                trace_id=self.trace.trace_id,
                span_id=active.span_id,
                parent_span_id=None,
                sequence=active.sequence,
                name=active.name,
                kind=active.kind,
                started_at=active.started_at,
                finished_at=None,
                duration_ms=None,
                status=SpanStatus.RUNNING,
                reason_code=DecisionCode.STARTED,
                attributes=_json_attributes(attributes),
                input_artifact_id=None,
                output_artifact_id=None,
            )
        )
        self._spans[active.span_id] = _SpanTimeline(handle=active)
        return active

    def finish_span(
        self,
        active: TraceSpanHandle,
        finish: TraceSpanFinish,
    ) -> None:
        """关闭 span 并保存独立 duration。

        Args:
            active: `start_span` 返回的计时状态。
            finish: 已校验的状态、原因、属性和 artifact 引用。

        Returns:
            无返回值。

        Raises:
            RuntimeError: span 不属于当前会话、已经关闭或仍有活动后代。

        """
        if finish.status is SpanStatus.RUNNING:
            raise ValueError("finish_span 不能使用 RUNNING。")
        timeline = self._require_open_span(active)
        descendants = self._descendants(active.span_id)
        if any(item.finished_at is None for item in descendants):
            raise RuntimeError("父 span 关闭前仍有活动后代。")
        finished_at, _ = self._timeline_now()
        recorded_finishes = [
            item.finished_at
            for item in descendants
            if item.finished_at is not None
        ]
        if recorded_finishes:
            finished_at = max(finished_at, *recorded_finishes)
        duration_ms = _duration_ms(active.started_at, finished_at)
        self._put_span(
            SpanRecord(
                trace_id=self.trace.trace_id,
                span_id=active.span_id,
                parent_span_id=active.parent_span_id,
                sequence=active.sequence,
                name=active.name,
                kind=active.kind,
                started_at=active.started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                status=finish.status,
                reason_code=finish.reason_code,
                attributes=_json_attributes(finish.attributes),
                input_artifact_id=finish.input_artifact_id,
                output_artifact_id=finish.output_artifact_id,
            )
        )
        timeline.finished_at = finished_at

    def completed_span(self, spec: TraceSpanSpec) -> str:
        """保存已完成的确定性子 span。

        Args:
            spec: span 名称、类别、父节点、原因、属性和独立耗时。

        Returns:
            新 span ID。

        Raises:
            RuntimeError: 父 span 不存在或已经关闭。

        """
        parent = self._open_parent(spec.parent_span_id)
        if parent is None:
            raise RuntimeError("completed span 必须提供父 span。")
        self._sequence += 1
        reported_duration_ms = max(0, spec.duration_ms)
        timeline_now, finished_tick = self._timeline_now()
        available_ms = _duration_ms(parent.handle.started_at, timeline_now)
        if spec.started_offset_ms is None:
            duration_ms = min(reported_duration_ms, available_ms)
            finished_at = timeline_now
            started_at = finished_at - timedelta(milliseconds=duration_ms)
        else:
            reported_offset_ms = max(0, spec.started_offset_ms)
            started_offset_ms = min(reported_offset_ms, available_ms)
            duration_ms = min(
                reported_duration_ms,
                available_ms - started_offset_ms,
            )
            started_at = parent.handle.started_at + timedelta(
                milliseconds=started_offset_ms
            )
            finished_at = started_at + timedelta(milliseconds=duration_ms)
        attributes = dict(spec.attributes or {})
        if duration_ms != reported_duration_ms:
            attributes["reported_duration_ms"] = reported_duration_ms
        if (
            spec.started_offset_ms is not None
            and started_offset_ms != spec.started_offset_ms
        ):
            attributes["reported_started_offset_ms"] = spec.started_offset_ms
        span_id = uuid.uuid4().hex[:16]
        handle = TraceSpanHandle(
            span_id=span_id,
            parent_span_id=spec.parent_span_id,
            sequence=self._sequence,
            name=spec.name,
            kind=spec.kind,
            started_at=started_at,
            started_tick=finished_tick - duration_ms / 1000,
        )
        self._put_span(
            SpanRecord(
                trace_id=self.trace.trace_id,
                span_id=span_id,
                parent_span_id=spec.parent_span_id,
                sequence=self._sequence,
                name=spec.name,
                kind=spec.kind,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                status=SpanStatus.OK,
                reason_code=spec.reason_code,
                attributes=_json_attributes(attributes),
                input_artifact_id=None,
                output_artifact_id=None,
            )
        )
        self._spans[span_id] = _SpanTimeline(
            handle=handle,
            finished_at=finished_at,
        )
        parent.children.add(span_id)
        return span_id

    def update_identity(
        self,
        *,
        revision_id: str | None = None,
        index_fingerprint: str | None = None,
        serving_fingerprint: str | None = None,
        active_collection: str | None = None,
    ) -> None:
        """补齐当前 Trace 的活动索引与服务身份。

        Args:
            revision_id: 可选活动 Revision ID。
            index_fingerprint: 可选索引指纹。
            serving_fingerprint: 可选服务指纹。
            active_collection: 可选活动集合身份。

        Returns:
            无返回值。

        """
        updates = {
            "revision_id": revision_id,
            "index_fingerprint": index_fingerprint,
            "serving_fingerprint": serving_fingerprint,
            "active_collection": active_collection,
        }
        if self._buffer_writes:
            self._identity_updates.update(
                {key: value for key, value in updates.items() if value}
            )
            return
        self._recorder.update_trace_identity(
            self.trace.trace_id,
            revision_id=revision_id,
            index_fingerprint=index_fingerprint,
            serving_fingerprint=serving_fingerprint,
            active_collection=active_collection,
        )

    def _put_span(self, span: SpanRecord) -> None:
        """立即提交严格 span，或有界保留非严格 span 的最终状态。"""
        if not self._buffer_writes:
            self._recorder.put_span(span, strict=self.strict)
            return
        if (
            span.span_id not in self._buffered_spans
            and len(self._buffered_spans) >= _BUFFERED_SPAN_LIMIT
        ):
            self._buffered_dropped_spans += 1
            return
        self._buffered_spans[span.span_id] = span

    def _put_decision(self, decision: CandidateDecision) -> None:
        """立即提交严格决策，或按总量与阶段边界保留决策。"""
        if not self._buffer_writes:
            self._recorder.add_candidate_decision(
                decision,
                strict=self.strict,
            )
            return
        stage_count = self._buffered_decision_stages.get(decision.stage, 0)
        if (
            len(self._buffered_decisions) >= _BUFFERED_DECISION_LIMIT
            or stage_count >= _BUFFERED_STAGE_DECISION_LIMIT
        ):
            self._buffered_dropped_decisions += 1
            return
        self._buffered_decisions.append(decision)
        self._buffered_decision_stages[decision.stage] = stage_count + 1

    def _timeline_now(self) -> tuple[datetime, float]:
        """从单调时钟推导当前会话时间点。

        Args:
            无参数；读取初始化时冻结的双时钟锚点。

        Returns:
            毫秒向上量化后的 wall-clock 时间和对应单调时点。

        """
        if self._completed_elapsed_ms is None:
            elapsed_seconds = max(0.0, self._clock() - self._monotonic_anchor)
            elapsed_ms = max(
                self._last_elapsed_ms,
                math.ceil(elapsed_seconds * 1000),
            )
        else:
            elapsed_ms = max(
                self._last_elapsed_ms,
                self._completed_elapsed_ms,
            )
        self._last_elapsed_ms = elapsed_ms
        return (
            self._wall_anchor + timedelta(milliseconds=elapsed_ms),
            self._monotonic_anchor + elapsed_ms / 1000,
        )

    def _open_parent(
        self,
        parent_span_id: str | None,
    ) -> _SpanTimeline | None:
        """返回可接收子 span 的父节点。

        Args:
            parent_span_id: 父 span ID；仅根 span 可为空。

        Returns:
            已校验的活动父节点；根 span 返回空。

        Raises:
            RuntimeError: 根 span 重复、父节点不存在或已经关闭。

        """
        if parent_span_id is None:
            if self._spans:
                raise RuntimeError("TraceSession 只能创建一个根 span。")
            return None
        parent = self._spans.get(parent_span_id)
        if parent is None:
            raise RuntimeError("父 span 不属于当前 TraceSession。")
        if parent.finished_at is not None:
            raise RuntimeError("父 span 已关闭，不能创建 child。")
        return parent

    def _require_open_span(
        self,
        active: TraceSpanHandle,
    ) -> _SpanTimeline:
        """校验 handle 对应当前会话中的活动 span。

        Args:
            active: 调用方持有的 span handle。

        Returns:
            当前 span 的可变生命周期状态。

        Raises:
            RuntimeError: handle 不匹配或 span 已经关闭。

        """
        timeline = self._spans.get(active.span_id)
        if timeline is None or timeline.handle != active:
            raise RuntimeError("span handle 不属于当前 TraceSession。")
        if timeline.finished_at is not None:
            raise RuntimeError("span 已关闭，不能重复关闭。")
        return timeline

    def _descendants(self, span_id: str) -> list[_SpanTimeline]:
        """返回当前已记录的全部后代。

        Args:
            span_id: 待遍历的父 span ID。

        Returns:
            深度优先收集的后代生命周期状态。

        """
        descendants: list[_SpanTimeline] = []
        pending = list(self._spans[span_id].children)
        while pending:
            child_id = pending.pop()
            child = self._spans[child_id]
            descendants.append(child)
            pending.extend(child.children)
        return descendants

    def decision(  # noqa: PLR0913
        self,
        *,
        stage: str,
        chunk_id: str,
        selected: bool,
        reason_code: DecisionCode,
        details: dict[str, object],
        candidate_id: str | None = None,
        evidence_id: str | None = None,
        channel: str | None = None,
        rank: int | None = None,
        score_type: str | None = None,
        score: float | None = None,
        contribution: float | None = None,
    ) -> None:
        """保存一行有序候选漏斗决策。

        Args:
            stage: 稳定阶段名。
            chunk_id: 稳定候选 ID。
            selected: 本阶段是否保留。
            reason_code: 稳定机械原因码。
            details: 不含正文或向量的 rank/score/元数据。
            candidate_id: 当前阶段使用的候选身份。
            evidence_id: 已形成证据时的稳定证据身份。
            channel: 候选进入当前阶段的检索通道。
            rank: 当前通道或阶段内的一基排名。
            score_type: score 的独立语义，例如 rrf 或 rerank。
            score: 当前阶段分数。
            contribution: 当前通道对融合分数的贡献。

        Returns:
            无返回值。

        """
        self._decision_sequence += 1
        self._put_decision(
            CandidateDecision(
                trace_id=self.trace.trace_id,
                sequence=self._decision_sequence,
                stage=stage,
                chunk_id=chunk_id,
                selected=selected,
                reason_code=reason_code,
                details=_json_attributes(details),
                candidate_id=candidate_id,
                evidence_id=evidence_id,
                channel=channel,
                rank=rank,
                score_type=score_type,
                score=score,
                contribution=contribution,
            )
        )

    def artifact(
        self,
        kind: str,
        payload: object,
    ) -> ArtifactMetadata | None:
        """仅在 FULL 模式保存 canonical JSON artifact。

        Args:
            kind: 稳定 artifact 类别。
            payload: 可 JSON 编码且不含 secret/向量/二进制的内容。

        Returns:
            FULL 模式的 metadata；其他模式为空。

        """
        if not self.strict:
            return None
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._recorder.add_artifact(
            self.trace.trace_id,
            kind=kind,
            media_type="application/json",
            payload=encoded,
            strict=True,
        )

    def finish(
        self,
        *,
        status: TraceStatus,
        reason_code: DecisionCode,
        refusal_code: str | None = None,
        error_code: str | None = None,
        attributes: dict[str, object] | None = None,
    ) -> None:
        """关闭根 span 和根 Trace；重复调用安全。

        Args:
            status: 根 Trace 终态。
            reason_code: 根 span 稳定结果码。
            refusal_code: 可选拒答码。
            error_code: 可选失败码。
            attributes: 根 span 的安全终态属性。

        Returns:
            无返回值。

        """
        if self._finished:
            return
        self.finish_span(
            self.root,
            TraceSpanFinish(
                status=(
                    SpanStatus.ERROR
                    if status is TraceStatus.FAILED
                    else (
                        SpanStatus.INTERRUPTED
                        if status is TraceStatus.INTERRUPTED
                        else SpanStatus.OK
                    )
                ),
                reason_code=reason_code,
                attributes={**self._root_attributes, **(attributes or {})},
            ),
        )
        self._finished = True
        root_finished_at = self._spans[self.root.span_id].finished_at
        if root_finished_at is None:
            raise RuntimeError("根 span 未完成，不能关闭 Trace。")
        finish = TraceFinish(
            status=status,
            finished_at=root_finished_at,
            refusal_code=refusal_code,
            error_code=error_code,
        )
        if self._buffer_writes:
            buffered_trace = self.trace
            if self._buffered_dropped_spans or self._buffered_dropped_decisions:
                buffered_trace = replace(
                    buffered_trace,
                    capture_complete=False,
                    capture_incomplete_reason="BUFFER_LIMIT",
                    dropped_span_count=self._buffered_dropped_spans,
                    dropped_decision_count=self._buffered_dropped_decisions,
                )
            self._recorder.write_completed_trace(
                buffered_trace,
                tuple(
                    sorted(
                        self._buffered_spans.values(),
                        key=lambda span: span.sequence,
                    )
                ),
                tuple(self._buffered_decisions),
                finish,
                revision_id=self._identity_updates.get("revision_id"),
                index_fingerprint=self._identity_updates.get(
                    "index_fingerprint"
                ),
                serving_fingerprint=self._identity_updates.get(
                    "serving_fingerprint"
                ),
                active_collection=self._identity_updates.get(
                    "active_collection"
                ),
            )
            return
        self._recorder.finish_trace(
            self.trace.trace_id,
            finish,
            strict=self.strict,
        )


class TraceRecorder:
    """把 Trace 写入与查询线程隔离并支持 FULL fail-closed。"""

    def __init__(
        self,
        store: TraceStore,
        *,
        exporter: TraceExporter | None = None,
        audit_failure: (Callable[[str, DecisionCode], None] | None) = None,
        config: TraceRecorderConfig | None = None,
    ) -> None:
        """启动唯一 writer 并保存失败审计边界。

        Args:
            store: 已初始化的独立 Trace Store。
            exporter: 可选外部导出器，默认不导出。
            audit_failure: 只接收 trace ID 和稳定失败码的回调。
            config: 可选的 writer 固定资源边界。

        """
        resolved_config = config or TraceRecorderConfig()
        self._store = store
        self._exporter = exporter or NullTraceExporter()
        self._audit_failure = audit_failure
        self._queue: queue.Queue[_WriteCommand | object] = queue.Queue(
            maxsize=resolved_config.queue_size
        )
        self._wait_seconds = resolved_config.wait_seconds
        self._prune_interval_seconds = resolved_config.prune_interval_seconds
        self._maintenance_idle_grace_seconds = (
            resolved_config.maintenance_idle_grace_seconds
        )
        self._full_reservation_bytes = (
            resolved_config.full_artifact_reservation_bytes
        )
        self._state_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._pending_incomplete: dict[str, tuple[int, int, int]] = {}
        self._pending_reasons: dict[str, str] = {}
        self._submitted_count = 0
        self._written_count = 0
        self._dropped_count = 0
        self._queue_high_water = 0
        self._accepting = True
        self._closed = False
        self._maintenance_condition = threading.Condition(threading.Lock())
        self._active_query_windows: set[str] = set()
        self._maintenance_active = False
        self._last_query_window_finished_at = (
            time.monotonic() - self._maintenance_idle_grace_seconds
        )
        self._writer = threading.Thread(
            target=self._run,
            name="rag-trace-writer",
            daemon=False,
        )
        self._writer.start()

    @property
    def writer_alive(self) -> bool:
        """返回 writer 线程是否仍存活。

        Args:
            无参数；检查当前 recorder。

        Returns:
            writer 尚未退出时为真。

        """
        return self._writer.is_alive()

    @property
    def metrics(self) -> dict[str, int]:
        """返回不含内容的队列与写入计数。

        Args:
            无参数；读取当前 recorder 的线程安全计数。

        Returns:
            提交、完成、丢弃和队列当前/最高水位。

        """
        with self._metrics_lock:
            return {
                "submitted": self._submitted_count,
                "written": self._written_count,
                "dropped": self._dropped_count,
                "queue_high_water": self._queue_high_water,
                "queue_current": self._queue.qsize(),
            }

    def require_full_capacity(self) -> None:
        """在 Debug 查询执行前确认 Store 和队列可用。

        Args:
            无参数；检查当前 FULL 捕获准入。

        Returns:
            无返回值。

        Raises:
            TraceUnavailableError: Store、recorder 或队列不可用。

        """
        with self._state_lock:
            accepting = self._accepting and not self._closed
        if not accepting or self._queue.full():
            raise TraceUnavailableError("FULL Trace writer 无容量。")
        try:
            self._store.healthcheck()
            self._store.preflight_full(
                reserved_bytes=self._full_reservation_bytes
            )
        except Exception as error:
            raise TraceUnavailableError("FULL Trace Store 不可用。") from error

    def begin_query_window(self, trace_id: str) -> None:
        """在延迟敏感查询开始前取得后台维护互斥窗。

        已经开始的清理会先完成；登记成功后，新的到期清理只能等到所有
        活跃查询结束并经过短静默窗后执行。互斥窗只协调后台维护，不保存
        查询正文，也不持有 Store 锁。

        Args:
            trace_id: 当前请求的公开 Trace ID。

        Returns:
            无返回值。

        Raises:
            RuntimeError: 同一 Trace ID 重复开始查询互斥窗。

        """
        with self._maintenance_condition:
            while self._maintenance_active:
                self._maintenance_condition.wait()
            if trace_id in self._active_query_windows:
                raise RuntimeError("同一 Trace 不能重复开始查询互斥窗。")
            self._active_query_windows.add(trace_id)

    def end_query_window(self, trace_id: str) -> None:
        """结束查询互斥窗并唤醒等待静默期的后台维护。

        未登记的 Trace ID 按幂等清理处理，避免异常终态的二次释放覆盖
        原始业务错误。

        Args:
            trace_id: 当前请求的公开 Trace ID。

        Returns:
            无返回值。

        """
        with self._maintenance_condition:
            if trace_id not in self._active_query_windows:
                return
            self._active_query_windows.remove(trace_id)
            self._last_query_window_finished_at = time.monotonic()
            self._maintenance_condition.notify_all()

    def begin_query(  # noqa: PLR0913
        self,
        trace_id: str,
        mode: TraceMode,
        created_at: datetime,
        identity: TraceIdentity,
        *,
        question_sha256: str | None = None,
        buffer_writes: bool = False,
        completed_elapsed_ms: int | None = None,
    ) -> TraceSession:
        """开始带固定保留期和运行身份的查询 Trace。

        Args:
            trace_id: 32 位小写十六进制 ID。
            mode: SAFE、DIAGNOSTIC 或 FULL 内容边界。
            created_at: 带时区创建时点。
            identity: pipeline、服务和活动索引身份。
            question_sha256: 可选的原始问题 SHA256；不保存问题正文。
            buffer_writes: 是否在完成时作为一个有界事务提交。
            completed_elapsed_ms: 延迟构造时已经完成的查询耗时。

        Returns:
            已开始根 span 的查询录制会话。

        Raises:
            TraceUnavailableError: FULL 捕获无法在查询前建立。

        """
        ttl = (
            timedelta(hours=72)
            if mode is TraceMode.FULL
            else timedelta(days=30)
        )
        trace = TraceRecord(
            trace_id=trace_id,
            schema_version="2",
            mode=mode,
            created_at=created_at,
            finished_at=None,
            duration_ms=None,
            pipeline_fingerprint=identity.pipeline_fingerprint,
            serving_fingerprint=identity.serving_fingerprint,
            release_revision=identity.release_revision,
            active_collection=identity.active_collection,
            index_manifest_sha256=identity.index_manifest_sha256,
            payload_schema_version=identity.payload_schema_version,
            status=TraceStatus.RUNNING,
            refusal_code=None,
            error_code=None,
            feedback_useful=None,
            capture_complete=True,
            expires_at=created_at + ttl,
            kind=identity.kind,
            project_id=identity.project_id,
            knowledge_base_id=identity.knowledge_base_id,
            owner_sha256=identity.owner_sha256,
            request_id=identity.request_id or trace_id,
            job_id=identity.job_id,
            document_id=identity.document_id,
            revision_id=identity.revision_id,
            profile_id=identity.profile_id,
            index_fingerprint=identity.index_fingerprint,
            source_revision=identity.source_revision,
        )
        root_attributes: dict[str, object] | None = (
            None
            if question_sha256 is None
            else {"question_sha256": question_sha256}
        )
        return TraceSession(
            self,
            trace,
            root_attributes=root_attributes,
            buffer_writes=buffer_writes,
            completed_elapsed_ms=completed_elapsed_ms,
        )

    def capture_failed(
        self,
        trace_id: str,
        code: DecisionCode = DecisionCode.TRACE_CAPTURE_FAILED,
    ) -> None:
        """记录不含正文的 Trace 捕获失败。

        Args:
            trace_id: 当前查询 Trace ID。
            code: 稳定 Trace 失败码。

        Returns:
            无返回值。

        """
        self._audit(trace_id, code)

    def update_trace_identity(
        self,
        trace_id: str,
        *,
        revision_id: str | None = None,
        index_fingerprint: str | None = None,
        serving_fingerprint: str | None = None,
        active_collection: str | None = None,
    ) -> None:
        """在检索 snapshot 固定后异步补齐活动身份。

        Args:
            trace_id: 待更新根 Trace ID。
            revision_id: 可选的活动 Revision ID。
            index_fingerprint: 可选的活动索引指纹。
            serving_fingerprint: 可选的查询服务指纹。
            active_collection: 可选的活动集合身份。

        Returns:
            无返回值；更新通过有界 writer 异步提交。

        """
        self._submit(
            trace_id,
            lambda: self._store.update_trace_identity(
                trace_id,
                revision_id=revision_id,
                index_fingerprint=index_fingerprint,
                serving_fingerprint=serving_fingerprint,
                active_collection=active_collection,
            ),
            strict=False,
            wait=False,
        )

    def begin_trace(self, trace: TraceRecord) -> None:
        """开始一条 Trace；FULL 模式同步确认持久化。

        Args:
            trace: 新的 RUNNING 根记录。

        Returns:
            无返回值。

        Raises:
            TraceUnavailableError: FULL Trace 无法持久化。

        """
        strict = trace.mode is TraceMode.FULL
        if strict:
            self.require_full_capacity()
        self._submit(
            trace.trace_id,
            lambda: self._store.create_trace(trace),
            strict=strict,
            wait=strict,
        )

    def put_span(self, span: SpanRecord, *, strict: bool = False) -> None:
        """异步插入或关闭一个 span。

        Args:
            span: 待写入的 span。
            strict: 是否按 FULL 捕获边界等待。

        Returns:
            无返回值。

        """
        self._submit(
            span.trace_id,
            lambda: self._store.put_span(span),
            strict=strict,
            wait=strict,
            drop_kind="span",
        )

    def add_candidate_decision(
        self,
        decision: CandidateDecision,
        *,
        strict: bool = False,
    ) -> None:
        """异步保存一个候选漏斗决策。

        Args:
            decision: 待写入的候选决策。
            strict: 是否按 FULL 捕获边界等待。

        Returns:
            无返回值。

        """
        self._submit(
            decision.trace_id,
            lambda: self._store.add_candidate_decision(decision),
            strict=strict,
            wait=strict,
            drop_kind="decision",
        )

    def add_artifact(
        self,
        trace_id: str,
        *,
        kind: str,
        media_type: str,
        payload: bytes,
        strict: bool,
    ) -> ArtifactMetadata | None:
        """保存完整 artifact，绝不截断超限内容。

        Args:
            trace_id: artifact 所属 Trace。
            kind: 稳定 artifact 类别。
            media_type: 原始媒体类型。
            payload: 完整原始字节。
            strict: FULL 模式应为真并等待结果。

        Returns:
            成功时返回 metadata；普通模式失败时返回空。

        Raises:
            TraceUnavailableError: FULL artifact 无法保存。

        """
        try:
            result = self._submit(
                trace_id,
                lambda: self._store.add_artifact(
                    trace_id,
                    kind=kind,
                    media_type=media_type,
                    payload=payload,
                ),
                strict=strict,
                wait=True,
            )
        except TraceUnavailableError:
            raise
        return result if isinstance(result, ArtifactMetadata) else None

    def finish_trace(
        self,
        trace_id: str,
        finish: TraceFinish,
        *,
        strict: bool = False,
    ) -> None:
        """关闭 Trace，并隔离 exporter 失败。

        Args:
            trace_id: 待关闭 Trace。
            finish: 已校验的 Trace 终态字段。
            strict: 是否按 FULL 捕获边界等待。

        Returns:
            无返回值。

        """

        def finish_and_export() -> None:
            """持久化终态并隔离可选导出失败。

            Args:
                无参数；使用外层已校验的 Trace 终态。

            Returns:
                无返回值。

            """
            self._store.finish_trace(
                trace_id,
                finish,
            )
            try:
                self._exporter.export_trace(self._store.get_trace(trace_id))
            except Exception:
                self._audit(
                    trace_id,
                    DecisionCode.TRACE_EXPORT_FAILED,
                )

        self._submit(
            trace_id,
            finish_and_export,
            strict=strict,
            wait=strict,
        )

    def write_completed_trace(  # noqa: PLR0913
        self,
        trace: TraceRecord,
        spans: Sequence[SpanRecord],
        decisions: Sequence[CandidateDecision],
        finish: TraceFinish,
        *,
        revision_id: str | None,
        index_fingerprint: str | None,
        serving_fingerprint: str | None,
        active_collection: str | None,
    ) -> None:
        """把已完成的非 FULL Trace 作为一个有界 writer 任务提交。

        Args:
            trace: 待建立的根 Trace。
            spans: 已去重且按 sequence 排列的最终 span。
            decisions: 已按硬边界截断的候选决策。
            finish: 根 Trace 终态。
            revision_id: 可选活动 Revision ID。
            index_fingerprint: 可选索引指纹。
            serving_fingerprint: 可选查询服务指纹。
            active_collection: 可选活动集合身份。

        Returns:
            成功进入有界队列后立即返回。

        """

        def write_and_export() -> None:
            """用一个事务写完 Trace，再隔离可选 exporter 失败。

            Args:
                无参数；使用外层冻结的完成快照。

            Returns:
                无返回值。

            """
            self._store.write_completed_trace(
                trace,
                spans,
                decisions,
                finish,
                revision_id=revision_id,
                index_fingerprint=index_fingerprint,
                serving_fingerprint=serving_fingerprint,
                active_collection=active_collection,
            )
            try:
                self._exporter.export_trace(
                    self._store.get_trace(trace.trace_id)
                )
            except Exception:
                self._audit(
                    trace.trace_id,
                    DecisionCode.TRACE_EXPORT_FAILED,
                )

        if threading.current_thread() is self._writer:
            write_and_export()
            return
        self._submit(
            trace.trace_id,
            write_and_export,
            strict=False,
            wait=False,
        )

    def submit_finalization(
        self,
        trace_id: str,
        action: Callable[[], object],
    ) -> None:
        """把非 FULL 的内存投影与单事务落盘合并为一个后台任务。

        Args:
            trace_id: 后台任务所属 Trace ID。
            action: 在唯一 writer 线程执行的完成动作。

        Returns:
            任务进入有界队列后立即返回。

        """
        self._submit(
            trace_id,
            action,
            strict=False,
            wait=False,
        )

    def mark_capture_incomplete(
        self,
        trace_id: str,
        *,
        reason: str = "TRACE_CAPTURE_FAILED",
        dropped_spans: int = 0,
        dropped_decisions: int = 0,
    ) -> None:
        """尽力标记当前 Trace 不完整。

        Args:
            trace_id: 待标记 Trace。
            reason: 稳定的不完整捕获原因码。
            dropped_spans: 本次未写入的 span 数。
            dropped_decisions: 本次未写入的候选决策数。

        Returns:
            无返回值。

        """
        with self._metrics_lock:
            previous = self._pending_incomplete.get(trace_id, (0, 0, 0))
            self._pending_incomplete[trace_id] = (
                previous[0] + max(0, dropped_spans),
                previous[1] + max(0, dropped_decisions),
                max(previous[2], self._queue_high_water),
            )
            self._pending_reasons.setdefault(trace_id, reason)

    def flush(self) -> None:
        """等待当前队列中此前命令全部完成。

        Args:
            无参数；排空当前 writer 队列。

        Returns:
            无返回值。

        Raises:
            TraceUnavailableError: writer 无法在上限内排空。

        """
        self._submit(
            "0" * 32,
            lambda: None,
            strict=True,
            wait=True,
        )
        self._flush_incomplete()

    def close(self) -> None:
        """停止准入、排空队列并幂等关闭 Store。

        Args:
            无参数；关闭当前 recorder。

        Returns:
            无返回值。

        """
        with self._state_lock:
            if self._closed:
                return
            self._accepting = False
            self._closed = True
        try:
            self._queue.put(_STOP, timeout=self._wait_seconds)
        except queue.Full as error:
            raise TraceUnavailableError(
                "Trace writer 无法在关闭前排空。"
            ) from error
        self._queue.join()
        self._flush_incomplete()
        self._writer.join(timeout=self._wait_seconds)
        if self._writer.is_alive():
            raise TraceUnavailableError("Trace writer 关闭超时。")
        self._store.close()

    def _submit(
        self,
        trace_id: str,
        action: Callable[[], object],
        *,
        strict: bool,
        wait: bool,
        drop_kind: Literal["span", "decision", "other"] = "other",
    ) -> object | None:
        """按捕获模式提交持久化命令并处理背压。

        非严格模式允许在不可用时记录审计并丢弃命令；严格模式会把
        关闭、队列拥塞、等待超时和持久化错误暴露给调用方。

        Args:
            trace_id: 用于失败审计的 Trace 标识。
            action: 由单一写线程执行的存储操作。
            strict: 是否要求提交及持久化失败对调用方可见。
            wait: 是否等待命令完成并返回存储操作结果。
            drop_kind: 队列拒绝时用于精确降级计数的命令类型。

        Returns:
            同步命令的存储操作结果；异步提交或容错丢弃时返回 `None`。

        Raises:
            TraceUnavailableError: 严格模式下 recorder 不可用、队列已满、
                等待超时或持久化失败。

        """
        completion = threading.Event() if wait else None
        command = _WriteCommand(
            trace_id=trace_id,
            action=action,
            completion=completion,
        )
        with self._state_lock:
            accepting = self._accepting and not self._closed
        if not accepting:
            self._reject_submission(trace_id, strict)
            return None
        try:
            if strict:
                self._queue.put(command, timeout=self._wait_seconds)
            else:
                self._queue.put_nowait(command)
        except queue.Full as error:
            self._audit(trace_id, DecisionCode.TRACE_QUEUE_FULL)
            with self._metrics_lock:
                self._dropped_count += 1
            self.mark_capture_incomplete(
                trace_id,
                reason=DecisionCode.TRACE_QUEUE_FULL.value,
                dropped_spans=int(drop_kind == "span"),
                dropped_decisions=int(drop_kind == "decision"),
            )
            if strict:
                raise TraceUnavailableError(
                    "FULL Trace writer 队列已满。"
                ) from error
            return None
        with self._metrics_lock:
            self._submitted_count += 1
            self._queue_high_water = max(
                self._queue_high_water,
                self._queue.qsize(),
            )
        if completion is None:
            return None
        if not completion.wait(timeout=self._wait_seconds):
            if strict:
                raise TraceUnavailableError("FULL Trace writer 等待超时。")
            return None
        if command.errors:
            if strict:
                raise TraceUnavailableError(
                    "FULL Trace 持久化失败。"
                ) from command.errors[0]
            return None
        return command.results[0] if command.results else None

    def _reject_submission(
        self,
        trace_id: str,
        strict: bool,
    ) -> None:
        self._audit(trace_id, DecisionCode.TRACE_CAPTURE_FAILED)
        if strict:
            raise TraceUnavailableError("Trace recorder 已关闭。")

    def _run(self) -> None:
        """消费写入队列并周期性清理过期 Trace。

        该后台循环会为每个已取出的队列项完成确认，并在收到停止标记
        后退出。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        next_prune = time.monotonic()
        while True:
            timeout = max(0.0, next_prune - time.monotonic())
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                next_prune = time.monotonic() + self._prune()
                continue
            try:
                if item is _STOP:
                    return
                if not isinstance(item, _WriteCommand):
                    _LOGGER.error("Trace writer 收到未知命令类型。")
                    continue
                self._flush_incomplete()
                self._execute(item)
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + self._prune()
            finally:
                self._queue.task_done()

    def _execute(self, command: _WriteCommand) -> None:
        """执行单条写命令并向等待方发布结果或错误。

        失败会转换为捕获审计并保存在命令对象中；存在完成事件时，无论
        成功或失败都会唤醒等待方。

        Args:
            command: 包含存储操作、结果容器和可选完成事件的写命令。

        Returns:
            无返回值。

        """
        try:
            command.results.append(command.action())
            with self._metrics_lock:
                self._written_count += 1
        except TraceArtifactLimitError as error:
            command.errors.append(error)
            self.mark_capture_incomplete(
                command.trace_id,
                reason=DecisionCode.TRACE_ARTIFACT_LIMIT.value,
            )
            self._audit(
                command.trace_id,
                DecisionCode.TRACE_ARTIFACT_LIMIT,
            )
        except Exception as error:
            command.errors.append(error)
            self.mark_capture_incomplete(
                command.trace_id,
                reason=DecisionCode.TRACE_CAPTURE_FAILED.value,
            )
            self._audit(
                command.trace_id,
                DecisionCode.TRACE_CAPTURE_FAILED,
            )
        finally:
            if command.completion is not None:
                command.completion.set()

    def _flush_incomplete(self) -> None:
        """由 writer 可靠落盘队列降级摘要，不回写敏感内容。"""
        with self._metrics_lock:
            pending = self._pending_incomplete
            pending_reasons = self._pending_reasons
            self._pending_incomplete = {}
            self._pending_reasons = {}
        for trace_id, (spans, decisions, high_water) in pending.items():
            try:
                self._store.mark_capture_incomplete(
                    trace_id,
                    reason=pending_reasons.get(trace_id, "TRACE_QUEUE_FULL"),
                    dropped_spans=spans,
                    dropped_decisions=decisions,
                    queue_high_water=high_water,
                )
            except Exception:
                _LOGGER.error(
                    "Trace 降级摘要无法落盘 trace_id=%s",
                    trace_id,
                )

    def _prune(self) -> float:
        """仅在查询静默窗外清理，并返回下次尝试的等待秒数。

        Args:
            无参数；检查 recorder 内的活跃查询集合。

        Returns:
            清理完成后的正常周期，或当前不宜清理时的有界重试间隔。

        """
        now = time.monotonic()
        with self._maintenance_condition:
            idle_remaining = max(
                0.0,
                self._last_query_window_finished_at
                + self._maintenance_idle_grace_seconds
                - now,
            )
            if self._active_query_windows or self._maintenance_active:
                return min(
                    self._prune_interval_seconds,
                    _MAINTENANCE_RETRY_SECONDS,
                )
            if idle_remaining > 0:
                return min(self._prune_interval_seconds, idle_remaining)
            self._maintenance_active = True
        try:
            self._store.prune(now=datetime.now(UTC))
        except Exception:
            _LOGGER.error(
                "Trace Store 到期清理失败 code=%s",
                DecisionCode.TRACE_CAPTURE_FAILED.value,
            )
        finally:
            with self._maintenance_condition:
                self._maintenance_active = False
                self._maintenance_condition.notify_all()
        return self._prune_interval_seconds

    def _audit(self, trace_id: str, code: DecisionCode) -> None:
        if self._audit_failure is None:
            return
        try:
            self._audit_failure(trace_id, code)
        except Exception:
            _LOGGER.error(
                "Trace 失败审计回调失败 code=%s",
                code.value,
            )


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    """返回同一毫秒时间轴上的非负区间长度。

    Args:
        started_at: 带时区开始时点。
        finished_at: 不早于开始时点的结束时点。

    Returns:
        与两个时点自洽的非负整毫秒数。

    """
    return max(
        0,
        round((finished_at - started_at).total_seconds() * 1000),
    )


def _json_attributes(
    raw: dict[str, object] | None,
) -> dict[str, JsonValue]:
    if raw is None:
        return {}
    try:
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("Trace attributes 必须是 JSON 小对象。") from error
    if not isinstance(decoded, dict):
        raise ValueError("Trace attributes 必须是 JSON object。")
    return cast(dict[str, JsonValue], decoded)
