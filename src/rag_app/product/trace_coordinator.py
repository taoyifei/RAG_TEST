"""协调 Product Query History 与 Operational Trace 的唯一适配层。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from time import monotonic
from typing import Literal

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import ProviderUnavailable, RagError
from rag_app.core.events import TraceEvent
from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.search import RetrievalDiagnostics, SearchAnswerResult
from rag_app.product.feedback import normalize_trace_id
from rag_app.product.query_history import (
    HistorySnapshotLimitError,
    ProductQueryHistory,
)
from rag_app.tracing.models import (
    ArtifactContent,
    SpanKind,
    SpanStatus,
    TraceDetail,
    TraceIdentity,
    TraceListFilter,
    TraceMode,
    TracePage,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import (
    TraceRecorder,
    TraceSession,
    TraceSpanFinish,
    TraceSpanHandle,
    TraceSpanSpec,
    TraceUnavailableError,
)
from rag_app.tracing.store import (
    ArtifactIntegrityError,
    TraceNotFoundError,
    TraceStore,
    TraceStoreClosedError,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_COMPAT_EXPORT_BYTES = 16 * 1024 * 1024
_MAX_PENDING_QUERY_EVENTS = 512
_QUERY_QUIESCENCE_SECONDS = 1.0
_QUERY_IDLE_GRACE_SECONDS = 0.25
_SESSION_SHARD_COUNT = 64


@dataclass(frozen=True, slots=True)
class _QueryTraceSettlement:
    """一次查询终态写入 Trace 所需的不可变上下文。"""

    result: SearchAnswerResult | None
    error: RagError | None
    cancelled: bool
    cancelled_calls: tuple[ProviderCall, ...]
    history_written: bool


@dataclass(slots=True)
class _BufferedQueryCapture:
    """延后非 FULL Trace 构造并限制请求私有兼容事件。"""

    trace_id: str
    mode: TraceMode
    created_at: datetime
    started_tick: float
    identity: TraceIdentity
    question_sha256: str
    admission_attributes: dict[str, object]
    events: list[TraceEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, event: TraceEvent) -> bool:
        """在固定上限内追加一个事件；达到上限时返回假。

        Args:
            event: 当前请求的安全兼容事件。

        Returns:
            事件已追加时为 True；容量耗尽时为 False。

        """
        with self._lock:
            if len(self.events) >= _MAX_PENDING_QUERY_EVENTS:
                return False
            self.events.append(event)
            return True

    def snapshot(self) -> tuple[TraceEvent, ...]:
        """在终态边界冻结当前事件序列。

        Args:
            无参数；读取当前请求捕获。

        Returns:
            当前已接收事件的不可变快照。

        """
        with self._lock:
            return tuple(self.events)


@dataclass(frozen=True, slots=True)
class OperationalTraceSnapshot:
    """统一解析后的当前、遗留、仅 History 或缺失 Trace 快照。"""

    trace_id: str
    state: Literal["current", "legacy-flat", "history-only", "missing"]
    schema_version: str | None
    capture_complete: bool
    project_id: str | None
    knowledge_base_id: str | None
    detail: dict[str, object] | None
    payload: bytes | None
    missing_reason: str | None


class OperationalTracePayloadLimitError(ValueError):
    """兼容批量解析在保留下一条前命中累计 payload 上限。"""


def _snapshot_authorization_identity(
    snapshot: OperationalTraceSnapshot,
) -> tuple[object, ...]:
    """返回 payload 物化前后必须保持不变的授权身份。

    Args:
        snapshot: metadata-only 或已物化的同一 Trace 快照。

    Returns:
        存储世代、scope 与捕获语义组成的不可变比较元组。

    """
    return (
        snapshot.trace_id,
        snapshot.state,
        snapshot.schema_version,
        snapshot.capture_complete,
        snapshot.project_id,
        snapshot.knowledge_base_id,
        snapshot.missing_reason,
    )


class ProductTraceCoordinator:
    """共享公开 trace_id，同时保持 History 与技术 Trace 的独立语义。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.TRACE_SINK,
        name="sqlite-product-operational-trace",
        version="2",
        mode=ProviderMode.LOCAL,
    )

    def __init__(  # noqa: PLR0913
        self,
        history: ProductQueryHistory,
        recorder: TraceRecorder,
        store: TraceStore,
        connections: SqliteConnectionFactory,
        *,
        pipeline_fingerprint: str,
        serving_fingerprint: str,
        release_revision: str,
        profile_id: str,
    ) -> None:
        """保存两个持久端并恢复遗留 Operational Trace。"""
        self.history = history
        self.recorder = recorder
        self.store = store
        self.connections = connections
        self.pipeline_fingerprint = pipeline_fingerprint
        self.serving_fingerprint = serving_fingerprint
        self.release_revision = release_revision
        self.profile_id = profile_id
        self._lock = threading.RLock()
        self._query_idle = threading.Condition(self._lock)
        self._last_query_finished_at = monotonic()
        self._session_shards: tuple[
            dict[str, TraceSession | _BufferedQueryCapture], ...
        ] = tuple({} for _ in range(_SESSION_SHARD_COUNT))
        self._session_locks = tuple(
            threading.Lock() for _ in range(_SESSION_SHARD_COUNT)
        )
        self._active_buffered_queries = 0
        self._ingestion_spans: dict[
            tuple[str, str, str, int], TraceSpanHandle
        ] = {}
        self._prepared_modes: dict[str, TraceMode] = {}
        self.store.recover_running()

    def prepare(self, trace_id: str, mode: TraceMode) -> None:
        """在查询读取 snapshot 前冻结 capture mode 并执行 FULL 准入。

        Args:
            trace_id: 当前请求的公开 Trace ID。
            mode: 本次 SAFE、DIAGNOSTIC 或 FULL 模式。

        Returns:
            无返回值。

        """
        if mode is TraceMode.FULL:
            self.recorder.require_full_capacity()
        with self._lock:
            self._prepared_modes[trace_id] = mode

    def start(  # noqa: PLR0913
        self,
        trace_id: str,
        scope: KnowledgeBaseScope,
        question: str,
        *,
        owner_id: str,
        save_body: bool,
        conversation_context_digest: str | None = None,
    ) -> None:
        """先建立权威 History，再以相同 ID 开始 Operational Trace。

        Args:
            trace_id: History 与技术 Trace 共享的公开 ID。
            scope: 当前 Project/KB 范围。
            question: 待查询问题；技术 Trace 只保存摘要。
            owner_id: 当前主体 ID；技术 Trace 只保存摘要。
            save_body: 是否按 History 政策加密保存正文。
            conversation_context_digest: 可选的会话上下文 SHA256；不保存正文。

        Returns:
            无返回值。

        """
        self.recorder.begin_query_window(trace_id)
        try:
            self._start_query(
                trace_id,
                scope,
                question,
                owner_id=owner_id,
                save_body=save_body,
                conversation_context_digest=conversation_context_digest,
            )
        except BaseException:
            self.recorder.end_query_window(trace_id)
            raise

    def _start_query(  # noqa: PLR0913
        self,
        trace_id: str,
        scope: KnowledgeBaseScope,
        question: str,
        *,
        owner_id: str,
        save_body: bool,
        conversation_context_digest: str | None,
    ) -> None:
        """在已登记维护互斥窗内建立 History 与 Trace 会话。"""
        mode = self._take_mode(trace_id)
        self.history.start(
            trace_id,
            scope,
            question,
            owner_id=owner_id,
            save_body=save_body,
            conversation_context_digest=conversation_context_digest,
        )
        identity = self._identity(
            kind="query",
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            owner_id=owner_id,
            request_id=trace_id,
        )
        question_sha256 = hashlib.sha256(question.encode()).hexdigest()
        created_at = datetime.now(UTC)
        started_tick = monotonic()
        admission_attributes: dict[str, object] = {
            "project_id": scope.project_id,
            "knowledge_base_id": scope.knowledge_base_id,
            "owner_sha256": identity.owner_sha256,
            "conversation_context_present": (
                conversation_context_digest is not None
            ),
        }
        if conversation_context_digest is not None:
            admission_attributes["conversation_context_digest"] = (
                conversation_context_digest
            )
        if mode is not TraceMode.FULL:
            capture = _BufferedQueryCapture(
                trace_id=trace_id,
                mode=mode,
                created_at=created_at,
                started_tick=started_tick,
                identity=identity,
                question_sha256=question_sha256,
                admission_attributes=admission_attributes,
            )
            with self._query_idle:
                self._active_buffered_queries += 1
                self._query_idle.notify_all()
            self._set_session(trace_id, capture)
            return
        try:
            session = self.recorder.begin_query(
                trace_id,
                mode,
                created_at,
                identity,
                question_sha256=question_sha256,
            )
        except Exception as error:
            self.recorder.capture_failed(trace_id)
            self._finish_history_after_start_failure(trace_id, error)
            raise
        session.completed_span(
            TraceSpanSpec(
                name="request.admission",
                kind=SpanKind.HTTP,
                parent_span_id=session.root.span_id,
                reason_code=DecisionCode.ADMISSION_ALLOWED,
                attributes=admission_attributes,
            )
        )
        self._set_session(trace_id, session)

    def finish(
        self,
        trace_id: str,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
        cancelled_calls: tuple[ProviderCall, ...] = (),
    ) -> None:
        """一次结算 History 与 Operational Trace，并保留原查询错误。

        Args:
            trace_id: 待结算的共享公开 ID。
            result: 可选的统一查询结果。
            error: 可选的原业务错误。
            cancelled: 请求是否被取消。
            cancelled_calls: 取消前已经结算的脱敏 Provider 调用。

        Returns:
            无返回值。

        """
        try:
            self._finish_query(
                trace_id,
                result=result,
                error=error,
                cancelled=cancelled,
                cancelled_calls=cancelled_calls,
            )
        finally:
            self.recorder.end_query_window(trace_id)

    def _finish_query(
        self,
        trace_id: str,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
        cancelled_calls: tuple[ProviderCall, ...],
    ) -> None:
        """结算查询持久化；维护互斥窗由公开入口统一释放。"""
        history_failure: RagError | None = None
        try:
            self.history.finish(
                trace_id,
                result=result,
                error=error,
                cancelled=cancelled,
                cancelled_calls=cancelled_calls,
            )
        except RagError as failure:
            history_failure = failure
            if error is not None or cancelled:
                _LOGGER.error(
                    "History 终态写入失败但保留原查询终态 trace_id=%s",
                    trace_id,
                )
        finished_tick = monotonic()
        stored_session = self._pop_session(trace_id)
        with self._query_idle:
            if isinstance(stored_session, _BufferedQueryCapture):
                self._active_buffered_queries -= 1
                if self._active_buffered_queries < 0:
                    raise RuntimeError("活跃缓冲查询计数不能为负数。")
            self._last_query_finished_at = finished_tick
            self._query_idle.notify_all()
        if stored_session is not None:
            settlement = _QueryTraceSettlement(
                result=result,
                error=error or history_failure,
                cancelled=cancelled,
                cancelled_calls=cancelled_calls,
                history_written=history_failure is None,
            )
            if isinstance(stored_session, _BufferedQueryCapture):
                flat_events = stored_session.snapshot()
                elapsed_ms = max(
                    0,
                    round((finished_tick - stored_session.started_tick) * 1000),
                )
                self.recorder.submit_finalization(
                    trace_id,
                    lambda: self._finalize_buffered_query(
                        stored_session,
                        flat_events,
                        settlement,
                        elapsed_ms,
                    ),
                )
            else:
                session = stored_session
                try:
                    self._finalize_query_trace(session, settlement)
                except TraceUnavailableError as trace_failure:
                    if error is not None or cancelled:
                        _LOGGER.error(
                            "FULL Trace 结算失败但保留原查询终态 trace_id=%s",
                            trace_id,
                        )
                    else:
                        raise ProviderUnavailable(
                            "FULL Trace 终态无法持久化。",
                            code="TRACE_PERSISTENCE_UNAVAILABLE",
                            stage="trace.finish",
                            trace_id=trace_id,
                        ) from trace_failure
        if history_failure is not None and error is None and not cancelled:
            raise history_failure

    def record(self, event: TraceEvent) -> None:
        """保留旧平面事件，并同步形成有界层级 Operational Trace。

        Args:
            event: 当前查询或 ingestion 的安全结构化事件。

        Returns:
            无返回值。

        """
        stored_session = self._session_for_event(event)
        if isinstance(stored_session, _BufferedQueryCapture):
            if not stored_session.append(event):
                self.recorder.mark_capture_incomplete(
                    event.trace_id,
                    reason="FLAT_EVENT_LIMIT",
                    dropped_spans=1,
                )
            return
        try:
            self.history.record(event)
        except RagError:
            self.recorder.capture_failed(event.trace_id)
            self.recorder.mark_capture_incomplete(
                event.trace_id,
                reason=DecisionCode.TRACE_CAPTURE_FAILED.value,
            )
        if stored_session is None:
            return
        self._record_operational_event(stored_session, event)

    def _record_operational_event(
        self,
        session: TraceSession,
        event: TraceEvent,
    ) -> None:
        """把一个安全事件投影到当前层级 Operational Trace。"""
        if event.event_name == "retrieval.snapshot":
            attributes = dict(event.attributes)
            session.update_identity(
                revision_id=_text(attributes.get("revision_id")),
                index_fingerprint=_text(attributes.get("index_fingerprint")),
                serving_fingerprint=_text(
                    attributes.get("serving_fingerprint")
                ),
                active_collection=_text(attributes.get("revision_id")),
            )
        if event.event_name.startswith("ingestion."):
            self._record_ingestion_event(session, event)
        else:
            self._record_query_event(session, event)

    def events(self, trace_id: str) -> tuple[TraceEvent, ...]:
        """继续提供旧 flat event 读取能力。

        Args:
            trace_id: 待读取的共享 Trace ID。

        Returns:
            按发生顺序排列的旧平面事件。

        """
        self.recorder.flush()
        return self.history.events(trace_id)

    def flush(self) -> None:
        """排空此前已提交的非 FULL Trace 与兼容事件投影。

        Args:
            无参数；等待当前 recorder 队列。

        Returns:
            无返回值。

        """
        self.recorder.flush()

    def diagnostics(self, trace_id: str) -> RetrievalDiagnostics:
        """继续从加密 History 元数据读取检索诊断。

        Args:
            trace_id: 待读取的共享 Trace ID。

        Returns:
            History 中保存的安全检索诊断。

        """
        return self.history.diagnostics(trace_id)

    def list_traces(self, filters: TraceListFilter) -> TracePage:
        """排空已确认写入后读取有界 Operational Trace 列表。

        Args:
            filters: 分页、时间、状态、scope 和身份过滤条件。

        Returns:
            有界且稳定排序的 Trace 页面。

        """
        if filters.trace_id is not None:
            filters = replace(
                filters,
                trace_id=normalize_trace_id(filters.trace_id),
            )
        self.recorder.flush()
        return self.store.list_traces(filters)

    def detail(self, trace_id: str) -> TraceDetail:
        """读取稳定 span tree、候选与 Artifact 元数据。

        Args:
            trace_id: 目标 Operational Trace ID。

        Returns:
            根 Trace、span、候选决策和 Artifact 元数据。

        """
        self.recorder.flush()
        return self.store.get_trace(normalize_trace_id(trace_id))

    def artifact(self, trace_id: str, artifact_id: str) -> ArtifactContent:
        """惰性读取并验证 FULL Artifact。

        Args:
            trace_id: Artifact 所属 Trace ID。
            artifact_id: 待读取 Artifact ID。

        Returns:
            通过摘要和大小复核的解压内容。

        """
        self.recorder.flush()
        try:
            return self.store.get_artifact(
                normalize_trace_id(trace_id), artifact_id
            )
        except ArtifactIntegrityError as error:
            raise ProviderUnavailable(
                "Operational Trace Artifact 完整性校验失败。",
                code="TRACE_ARTIFACT_CORRUPT",
                stage="trace.artifact",
                retryable=False,
            ) from error

    def export(self, trace_id: str) -> bytes:
        """经统一兼容解析导出单条 canonical JSON。

        Args:
            trace_id: 待导出的 Operational Trace ID。

        Returns:
            UTF-8 canonical JSON 字节。

        """
        with self.export_snapshots(
            (trace_id,),
            include_payload=True,
            max_total_payload_bytes=_DEFAULT_COMPAT_EXPORT_BYTES,
        ) as snapshots:
            snapshot = snapshots[0]
            if snapshot.state == "missing" or snapshot.payload is None:
                raise TraceNotFoundError(trace_id)
            return snapshot.payload

    @contextmanager
    def export_guard(self, trace_ids: Sequence[str]) -> Iterator[None]:
        """以稳定错误语义暴露底层 Trace 导出 lease。

        Args:
            trace_ids: 已验证且不重复的 Trace ID。

        Yields:
            Trace prune 不会删除这些 ID 的临界区。

        Returns:
            管理持久 Trace 导出 lease 的上下文迭代器。

        Raises:
            ValueError: ID 为空、重复或格式无效。
            ProviderUnavailable: Trace Store 无法建立或释放 lease。

        """
        canonical_ids = tuple(normalize_trace_id(value) for value in trace_ids)
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("Trace 导出 guard 不接受等价的新旧重复 ID。")
        try:
            with self.store.export_guard(canonical_ids):
                yield
        except (sqlite3.Error, TraceStoreClosedError) as error:
            raise ProviderUnavailable(
                "Operational Trace 暂时无法建立导出快照。",
                code="TRACE_PERSISTENCE_UNAVAILABLE",
                stage="trace.export",
                retryable=True,
            ) from error

    @contextmanager
    def export_snapshots(
        self,
        trace_ids: Sequence[str],
        *,
        include_payload: bool = True,
        max_total_payload_bytes: int | None = None,
        authorize: Callable[[OperationalTraceSnapshot], None] | None = None,
    ) -> Iterator[tuple[OperationalTraceSnapshot, ...]]:
        """在 Trace lease 内用唯一解析器冻结一组兼容快照。

        Args:
            trace_ids: 已校验、无重复的 Trace ID，保持调用方顺序。
            include_payload: 是否同时生成 canonical 导出字节。
            max_total_payload_bytes: 可选的累计 payload 字节硬上限。
            authorize: 可选逐条授权回调；全部 metadata 通过后才读取 payload。

        Yields:
            与输入顺序相同的 Trace 快照。

        Returns:
            管理 Trace lease 与快照生命周期的上下文迭代器。

        Raises:
            ValueError: ID 为空、重复或格式无效。
            ProviderUnavailable: Trace/History Store 无法稳定读取。

        """
        ordered = tuple(trace_ids)
        if max_total_payload_bytes is not None and max_total_payload_bytes <= 0:
            raise ValueError("Trace 导出累计字节上限必须为正数。")
        try:
            with self.export_guard(ordered):
                self.recorder.flush()
                metadata_snapshots = tuple(
                    self._resolve_trace(
                        trace_id,
                        include_payload=False,
                        max_payload_bytes=None,
                    )
                    for trace_id in ordered
                )
                if authorize is not None:
                    for snapshot in metadata_snapshots:
                        authorize(snapshot)
                if not include_payload:
                    yield metadata_snapshots
                    return
                snapshots: list[OperationalTraceSnapshot] = []
                total_payload_bytes = 0
                for metadata in metadata_snapshots:
                    snapshot = self._resolve_trace(
                        metadata.trace_id,
                        include_payload=True,
                        max_payload_bytes=max_total_payload_bytes,
                    )
                    if _snapshot_authorization_identity(snapshot) != (
                        _snapshot_authorization_identity(metadata)
                    ):
                        raise ProviderUnavailable(
                            "Operational Trace 在导出授权后发生变化，请重试。",
                            code="TRACE_SNAPSHOT_CHANGED",
                            stage="trace.export",
                            retryable=True,
                        )
                    if snapshot.payload is not None:
                        total_payload_bytes += len(snapshot.payload)
                    if (
                        max_total_payload_bytes is not None
                        and total_payload_bytes > max_total_payload_bytes
                    ):
                        raise OperationalTracePayloadLimitError(
                            "Trace 导出超过累计字节上限。"
                        )
                    snapshots.append(snapshot)
                yield tuple(snapshots)
        except (sqlite3.Error, TraceStoreClosedError) as error:
            raise ProviderUnavailable(
                "Operational Trace 暂时无法读取。",
                code="TRACE_PERSISTENCE_UNAVAILABLE",
                stage="trace.export",
                retryable=True,
            ) from error

    def set_feedback(self, trace_id: str, *, useful: bool) -> bool:
        """只更新当前层级 Trace；兼容记录明确返回未投影。

        Args:
            trace_id: 待关联反馈的公开 Trace ID。
            useful: 用户是否认为回答有用。

        Returns:
            当前 Trace 成功更新时为 True；遗留或仅 History 时为 False。

        Raises:
            TraceNotFoundError: 两个存储都不存在该 ID。
            ProviderUnavailable: Store 无法稳定读取或写入。

        """
        with self.export_snapshots(
            (trace_id,), include_payload=False
        ) as snapshots:
            snapshot = snapshots[0]
            if snapshot.state == "missing":
                raise TraceNotFoundError(trace_id)
            if snapshot.state != "current":
                return False
            try:
                self.store.set_feedback(
                    normalize_trace_id(trace_id), useful=useful
                )
            except (sqlite3.Error, TraceStoreClosedError) as error:
                raise ProviderUnavailable(
                    "Operational Trace 反馈暂时无法保存。",
                    code="TRACE_PERSISTENCE_UNAVAILABLE",
                    stage="trace.feedback",
                    trace_id=(
                        trace_id if trace_id.startswith("trace_") else None
                    ),
                    retryable=True,
                ) from error
            return True

    def metrics(self) -> dict[str, int]:
        """返回 Trace writer 性能计数。

        Args:
            无参数；读取线程安全的当前计数。

        Returns:
            submitted、written、dropped 和队列水位计数。

        """
        return self.recorder.metrics

    def legacy_detail(self, trace_id: str) -> dict[str, object]:
        """为迁移前 flat events 返回明确不完整的兼容视图。

        Args:
            trace_id: 待读取的新旧共享 Trace ID。

        Returns:
            当前层级详情或标记为不完整的旧事件视图。

        """
        with self.export_snapshots(
            (trace_id,), include_payload=False
        ) as snapshots:
            snapshot = snapshots[0]
            if snapshot.detail is None:
                raise TraceNotFoundError(trace_id)
            return snapshot.detail

    def _resolve_trace(
        self,
        trace_id: str,
        *,
        include_payload: bool,
        max_payload_bytes: int | None,
    ) -> OperationalTraceSnapshot:
        """在调用方持有 lease 时解析一条 Trace 的实际存储世代。"""
        canonical_trace_id = normalize_trace_id(trace_id)
        try:
            current = self.store.get_trace(canonical_trace_id)
        except TraceNotFoundError:
            current = None
        if current is not None:
            detail = _detail_dict(current)
            try:
                payload = (
                    self.store.export_trace(canonical_trace_id)
                    if include_payload
                    else None
                )
            except ArtifactIntegrityError as error:
                raise ProviderUnavailable(
                    "Operational Trace Artifact 完整性校验失败。",
                    code="TRACE_ARTIFACT_CORRUPT",
                    stage="trace.export",
                    retryable=False,
                ) from error
            return OperationalTraceSnapshot(
                trace_id=trace_id,
                state="current",
                schema_version=current.trace.schema_version,
                capture_complete=current.trace.capture_complete,
                project_id=current.trace.project_id,
                knowledge_base_id=current.trace.knowledge_base_id,
                detail=detail,
                payload=payload,
                missing_reason=None,
            )

        try:
            events = self.history.event_payloads(
                trace_id,
                max_total_bytes=max_payload_bytes if include_payload else None,
            )
        except HistorySnapshotLimitError as error:
            raise OperationalTracePayloadLimitError(
                "Trace flat events 超过累计字节上限。"
            ) from error
        scope = self.history.record_scope(trace_id)
        if events:
            detail = {
                "trace": {
                    "trace_id": trace_id,
                    "schema_version": "legacy-flat-1",
                    "capture_complete": False,
                    "capture_incomplete_reason": "legacy_flat_events",
                },
                "spans": (),
                "candidate_decisions": (),
                "artifacts": (),
                "legacy_flat_events": list(events),
            }
            return OperationalTraceSnapshot(
                trace_id=trace_id,
                state="legacy-flat",
                schema_version="legacy-flat-1",
                capture_complete=False,
                project_id=None if scope is None else scope[0],
                knowledge_base_id=None if scope is None else scope[1],
                detail=detail,
                payload=(
                    _canonical_json_bytes(detail) if include_payload else None
                ),
                missing_reason="legacy_flat_events",
            )
        if scope is not None:
            root: dict[str, object] = {
                "trace_id": trace_id,
                "schema_version": "missing-pre-v3",
                "capture_complete": False,
                "status": "NOT_CAPTURED_BEFORE_V3",
                "project_id": scope[0],
                "knowledge_base_id": scope[1],
            }
            detail = {
                "trace": root,
                "spans": (),
                "candidate_decisions": (),
                "artifacts": (),
                "legacy_flat_events": (),
            }
            return OperationalTraceSnapshot(
                trace_id=trace_id,
                state="history-only",
                schema_version="missing-pre-v3",
                capture_complete=False,
                project_id=scope[0],
                knowledge_base_id=scope[1],
                detail=detail,
                payload=(
                    _canonical_json_bytes(root) if include_payload else None
                ),
                missing_reason="NOT_CAPTURED_BEFORE_V3",
            )
        return OperationalTraceSnapshot(
            trace_id=trace_id,
            state="missing",
            schema_version=None,
            capture_complete=False,
            project_id=None,
            knowledge_base_id=None,
            detail=None,
            payload=None,
            missing_reason="TRACE_AND_HISTORY_MISSING",
        )

    def close(self) -> None:
        """按 writer flush、Store、History 的顺序幂等关闭。

        Args:
            无参数；关闭 coordinator 持有的资源。

        Returns:
            无返回值。

        """
        self.recorder.close()
        self.history.close()

    def _take_mode(self, trace_id: str) -> TraceMode:
        with self._lock:
            return self._prepared_modes.pop(trace_id, TraceMode.SAFE)

    @staticmethod
    def _session_slot(trace_id: str) -> int:
        """把一个 Trace ID 映射到进程内会话分片。"""
        return hash(trace_id) & (_SESSION_SHARD_COUNT - 1)

    def _get_session(
        self, trace_id: str
    ) -> TraceSession | _BufferedQueryCapture | None:
        """只锁定目标分片读取会话。"""
        slot = self._session_slot(trace_id)
        with self._session_locks[slot]:
            return self._session_shards[slot].get(trace_id)

    def _set_session(
        self,
        trace_id: str,
        session: TraceSession | _BufferedQueryCapture,
    ) -> None:
        """只锁定目标分片登记会话。"""
        slot = self._session_slot(trace_id)
        with self._session_locks[slot]:
            self._session_shards[slot][trace_id] = session

    def _setdefault_session(
        self,
        trace_id: str,
        session: TraceSession,
    ) -> TraceSession | _BufferedQueryCapture:
        """只锁定目标分片登记或返回并发建立的会话。"""
        slot = self._session_slot(trace_id)
        with self._session_locks[slot]:
            return self._session_shards[slot].setdefault(trace_id, session)

    def _pop_session(
        self, trace_id: str
    ) -> TraceSession | _BufferedQueryCapture | None:
        """只锁定目标分片移除会话。"""
        slot = self._session_slot(trace_id)
        with self._session_locks[slot]:
            return self._session_shards[slot].pop(trace_id, None)

    def _identity(  # noqa: PLR0913
        self,
        *,
        kind: str,
        project_id: str | None,
        knowledge_base_id: str | None,
        owner_id: str | None = None,
        request_id: str | None = None,
        job_id: str | None = None,
        document_id: str | None = None,
        revision_id: str | None = None,
    ) -> TraceIdentity:
        return TraceIdentity(
            pipeline_fingerprint=self.pipeline_fingerprint,
            serving_fingerprint=self.serving_fingerprint,
            release_revision=self.release_revision,
            active_collection=revision_id or "pending",
            index_manifest_sha256="0" * 64,
            payload_schema_version=3,
            kind=kind,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            owner_sha256=(
                None
                if owner_id is None
                else hashlib.sha256(owner_id.encode()).hexdigest()
            ),
            request_id=request_id,
            job_id=job_id,
            document_id=document_id,
            revision_id=revision_id,
            profile_id=self.profile_id,
            index_fingerprint=self.pipeline_fingerprint,
            source_revision=self.release_revision,
        )

    def _session_for_event(
        self, event: TraceEvent
    ) -> TraceSession | _BufferedQueryCapture | None:
        current = self._get_session(event.trace_id)
        if current is not None:
            return current
        if not event.event_name.startswith("ingestion."):
            return None
        attributes = dict(event.attributes)
        job_id = _text(attributes.get("job_id"))
        revision_id = _text(attributes.get("revision_id"))
        if job_id is None or revision_id is None:
            self.recorder.capture_failed(event.trace_id)
            return None
        project_id, knowledge_base_id, document_id = self._job_scope(job_id)
        try:
            session = self.recorder.begin_query(
                event.trace_id,
                TraceMode.SAFE,
                event.occurred_at,
                self._identity(
                    kind="ingestion",
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                    job_id=job_id,
                    document_id=document_id,
                    revision_id=revision_id,
                ),
            )
        except Exception:
            self.recorder.capture_failed(event.trace_id)
            return None
        return self._setdefault_session(event.trace_id, session)

    def _job_scope(
        self, job_id: str
    ) -> tuple[str | None, str | None, str | None]:
        with self.connections.transaction() as connection:
            row = connection.execute(
                "SELECT project_id, knowledge_base_id, document_id "
                "FROM ingestion_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None, None, None
        return _text(row[0]), _text(row[1]), _text(row[2])

    def _record_query_event(
        self, session: TraceSession, event: TraceEvent
    ) -> None:
        attributes: dict[str, object] = dict(event.attributes)
        stage = event.event_name.removeprefix("retrieval.")
        reason = _event_reason(stage, attributes)
        session.completed_span(
            TraceSpanSpec(
                name=event.event_name,
                kind=_span_kind(stage),
                parent_span_id=session.root.span_id,
                reason_code=reason,
                attributes=attributes,
                duration_ms=_duration(attributes),
            )
        )

    def _record_ingestion_event(
        self, session: TraceSession, event: TraceEvent
    ) -> None:
        attributes: dict[str, object] = dict(event.attributes)
        name = event.event_name.removeprefix("ingestion.")
        attempt = _positive_int(attributes.get("attempt"), default=1)
        document_id = _text(attributes.get("document_id")) or "revision"
        stage = name.removesuffix(".started").removesuffix(".finished")
        key = (event.trace_id, stage, document_id, attempt)
        if name.endswith(".started"):
            handle = session.start_span(
                f"ingestion.{stage}",
                _span_kind(stage),
                parent_span_id=session.root.span_id,
                attributes=attributes,
            )
            with self._lock:
                self._ingestion_spans[key] = handle
            return
        if name.endswith(".finished"):
            with self._lock:
                finished_handle = (
                    self._ingestion_spans.pop(key)
                    if key in self._ingestion_spans
                    else None
                )
            if finished_handle is not None:
                status = str(attributes.get("status") or "failed")
                session.finish_span(
                    finished_handle,
                    TraceSpanFinish(
                        status=(
                            SpanStatus.OK
                            if status == "success"
                            else SpanStatus.ERROR
                        ),
                        reason_code=(
                            DecisionCode.INGESTION_STAGE_SUCCEEDED
                            if status == "success"
                            else (
                                DecisionCode.INGESTION_STAGE_CANCELLED
                                if status == "cancelled"
                                else DecisionCode.INGESTION_STAGE_FAILED
                            )
                        ),
                        attributes=attributes,
                    ),
                )
            return
        if name in {"completed", "failed", "cancelled"}:
            status = {
                "completed": TraceStatus.SUCCEEDED,
                "failed": TraceStatus.FAILED,
                "cancelled": TraceStatus.CANCELLED,
            }[name]
            session.finish(
                status=status,
                reason_code={
                    "completed": DecisionCode.ANSWERED,
                    "failed": DecisionCode.ERROR,
                    "cancelled": DecisionCode.CANCELLED,
                }[name],
                error_code=_text(attributes.get("error_code")),
                attributes=attributes,
            )
            self._pop_session(event.trace_id)
            return
        session.completed_span(
            TraceSpanSpec(
                name=event.event_name,
                kind=_span_kind(stage),
                parent_span_id=session.root.span_id,
                reason_code=DecisionCode.STARTED,
                attributes=attributes,
                duration_ms=_duration(attributes),
            )
        )

    def _finalize_buffered_query(
        self,
        capture: _BufferedQueryCapture,
        events: Sequence[TraceEvent],
        settlement: _QueryTraceSettlement,
        elapsed_ms: int,
    ) -> None:
        """在单 writer 线程内投影并提交一条非 FULL 查询 Trace。"""
        self._wait_for_query_quiescence()
        try:
            session = self.recorder.begin_query(
                capture.trace_id,
                capture.mode,
                capture.created_at,
                capture.identity,
                question_sha256=capture.question_sha256,
                buffer_writes=True,
                completed_elapsed_ms=elapsed_ms,
            )
            session.completed_span(
                TraceSpanSpec(
                    name="request.admission",
                    kind=SpanKind.HTTP,
                    parent_span_id=session.root.span_id,
                    reason_code=DecisionCode.ADMISSION_ALLOWED,
                    attributes=capture.admission_attributes,
                )
            )
        except Exception:
            self.recorder.capture_failed(capture.trace_id)
            self.recorder.mark_capture_incomplete(
                capture.trace_id,
                reason=DecisionCode.TRACE_CAPTURE_FAILED.value,
            )
            self._record_capture_degradation(capture.trace_id)
            return
        try:
            self.history.record_many(events)
        except RagError:
            self.recorder.capture_failed(capture.trace_id)
            self.recorder.mark_capture_incomplete(
                capture.trace_id,
                reason=DecisionCode.TRACE_CAPTURE_FAILED.value,
            )
        for event in events:
            self._record_operational_event(session, event)
        self._finalize_query_trace(session, settlement)

    def _wait_for_query_quiescence(self) -> None:
        """等待突发批次和短静默窗结束，超时后继续避免持续流量饿死。"""
        deadline = monotonic() + _QUERY_QUIESCENCE_SECONDS
        with self._query_idle:
            while True:
                now = monotonic()
                deadline_remaining = deadline - now
                if deadline_remaining <= 0:
                    return
                active = self._active_buffered_queries > 0
                idle_remaining = (
                    self._last_query_finished_at
                    + _QUERY_IDLE_GRACE_SECONDS
                    - now
                )
                if not active and idle_remaining <= 0:
                    return
                wait_seconds = (
                    deadline_remaining
                    if active
                    else min(deadline_remaining, idle_remaining)
                )
                self._query_idle.wait(timeout=wait_seconds)

    def _finalize_query_trace(
        self,
        session: TraceSession,
        settlement: _QueryTraceSettlement,
    ) -> None:
        result = settlement.result
        error = settlement.error
        cancelled_calls = settlement.cancelled_calls
        if result is not None and result.diagnostics is not None:
            diagnostics = result.diagnostics
            _record_stage_timings(session, diagnostics)
            if session.trace.mode is not TraceMode.SAFE:
                _record_diagnostics(session, diagnostics)
            else:
                for call in diagnostics.provider_call_details:
                    _provider_span(session, call)
            if session.trace.mode is TraceMode.FULL:
                session.artifact(
                    "retrieval_diagnostics",
                    diagnostics.model_dump(mode="json"),
                )
        elif cancelled_calls:
            for call in cancelled_calls:
                _provider_span(session, call)
        session.completed_span(
            TraceSpanSpec(
                name="history.settlement",
                kind=SpanKind.STORAGE,
                parent_span_id=session.root.span_id,
                reason_code=(
                    DecisionCode.PUBLISHED
                    if settlement.history_written
                    else DecisionCode.TRACE_CAPTURE_FAILED
                ),
                attributes={"written": settlement.history_written},
            )
        )
        if settlement.cancelled:
            status = TraceStatus.CANCELLED
            reason = DecisionCode.CANCELLED
        elif error is not None:
            status = TraceStatus.FAILED
            reason = DecisionCode.ERROR
        elif result is not None and result.answer:
            status = TraceStatus.ANSWERED
            reason = DecisionCode.ANSWERED
        else:
            status = TraceStatus.REFUSED
            reason = DecisionCode.REFUSED
        session.finish(
            status=status,
            reason_code=reason,
            refusal_code=(
                result.reason_code
                if status is TraceStatus.REFUSED and result is not None
                else None
            ),
            error_code=None if error is None else error.code,
            attributes={
                "history_written": settlement.history_written,
                "provider_call_count": _provider_call_count(
                    result,
                    error,
                    cancelled_calls,
                ),
            },
        )

    def _finish_history_after_start_failure(
        self, trace_id: str, error: Exception
    ) -> None:
        failure = ProviderUnavailable(
            "FULL Trace 无法在查询前建立。",
            code="TRACE_PERSISTENCE_UNAVAILABLE",
            stage="trace.preflight",
            trace_id=trace_id,
            details={"exception_type": type(error).__name__},
        )
        self.history.finish(
            trace_id,
            result=None,
            error=failure,
            cancelled=False,
        )

    def _record_capture_degradation(self, trace_id: str) -> None:
        """在 Operational Store 不可用时把降级事实写入权威 History。"""
        try:
            self.history.record(
                TraceEvent(
                    trace_id=trace_id,
                    event_name="trace.capture_failed",
                    occurred_at=datetime.now(UTC),
                    attributes=freeze_json_object(
                        {
                            "reason_code": (
                                DecisionCode.TRACE_CAPTURE_FAILED.value
                            )
                        }
                    ),
                )
            )
        except RagError:
            _LOGGER.error(
                "Trace 捕获降级事件也无法落盘 trace_id=%s",
                trace_id,
            )


def _record_diagnostics(
    session: TraceSession, diagnostics: RetrievalDiagnostics
) -> None:
    """从当前链既有诊断生成候选漏斗和 Provider child spans。"""
    fused_ids = set(diagnostics.fused_chunk_ids)
    reranked_ids = {item.chunk_id for item in diagnostics.reranked}
    evidence_by_chunk = {item.chunk_id: item for item in diagnostics.evidence}
    for channel, chunk_ids in diagnostics.channel_chunk_ids:
        for rank, chunk_id in enumerate(chunk_ids, start=1):
            selected = chunk_id in fused_ids
            session.decision(
                stage="channel",
                chunk_id=chunk_id,
                selected=selected,
                reason_code=(
                    _channel_reason(channel)
                    if selected
                    else DecisionCode.DROPPED_FINAL_LIMIT
                ),
                details={},
                candidate_id=chunk_id,
                channel=channel,
                rank=rank,
                score_type="channel_rank",
            )
    for fusion_item in diagnostics.fusion:
        for contribution in fusion_item.contributions:
            session.decision(
                stage="fusion_channel",
                chunk_id=fusion_item.chunk_id,
                selected=True,
                reason_code=DecisionCode.FUSION_SELECTED,
                details={},
                candidate_id=fusion_item.chunk_id,
                channel=contribution.channel,
                rank=contribution.rank,
                score_type="rrf_contribution",
                contribution=contribution.contribution,
            )
        session.decision(
            stage="fusion",
            chunk_id=fusion_item.chunk_id,
            selected=fusion_item.chunk_id in reranked_ids,
            reason_code=(
                DecisionCode.FUSION_SELECTED
                if fusion_item.chunk_id in reranked_ids
                else DecisionCode.RERANK_DROP
            ),
            details={
                "contributions": [
                    contribution.model_dump(mode="json")
                    for contribution in fusion_item.contributions
                ]
            },
            candidate_id=fusion_item.chunk_id,
            rank=fusion_item.rank,
            score_type="rrf",
            score=fusion_item.score,
        )
    for rerank_item in diagnostics.reranked:
        session.decision(
            stage="rerank",
            chunk_id=rerank_item.chunk_id,
            selected=rerank_item.chunk_id in evidence_by_chunk,
            reason_code=(
                DecisionCode.RERANK_SELECTED
                if rerank_item.chunk_id in evidence_by_chunk
                else DecisionCode.EVIDENCE_BUDGET_DROP
            ),
            details={},
            candidate_id=rerank_item.chunk_id,
            rank=rerank_item.rank,
            score_type="rerank",
            score=rerank_item.score,
        )
    for expansion_item in diagnostics.expanded:
        selected = expansion_item.chunk_id in evidence_by_chunk
        session.decision(
            stage=(
                "neighbor_expansion"
                if expansion_item.reason is not None
                else "evidence_candidate"
            ),
            chunk_id=expansion_item.chunk_id,
            selected=selected,
            reason_code=(
                DecisionCode.NEIGHBOR_EXPANDED
                if expansion_item.reason is not None and selected
                else (
                    DecisionCode.EVIDENCE_SELECTED
                    if selected
                    else DecisionCode.EVIDENCE_BUDGET_DROP
                )
            ),
            details={"expansion_reason": expansion_item.reason},
            candidate_id=expansion_item.chunk_id,
        )
    for evidence_item in diagnostics.evidence:
        session.decision(
            stage="evidence",
            chunk_id=evidence_item.chunk_id,
            selected=True,
            reason_code=DecisionCode.EVIDENCE_SELECTED,
            details={"source_range_count": len(evidence_item.source_ranges)},
            candidate_id=evidence_item.chunk_id,
            evidence_id=evidence_item.evidence_id,
        )
    for call in diagnostics.provider_call_details:
        _provider_span(session, call)


def _record_stage_timings(
    session: TraceSession,
    diagnostics: RetrievalDiagnostics,
) -> None:
    """按真实阶段顺序写入可比较、非 Provider 的耗时瀑布。"""
    offset_ms = 0
    for timing in diagnostics.stage_timings:
        duration_ms = max(0, round(timing.elapsed_ms))
        session.completed_span(
            TraceSpanSpec(
                name=f"timing.{timing.stage}",
                kind=_span_kind(timing.stage),
                parent_span_id=session.root.span_id,
                reason_code=DecisionCode.PUBLISHED,
                attributes={"reported_elapsed_ms": timing.elapsed_ms},
                duration_ms=duration_ms,
                started_offset_ms=offset_ms,
            )
        )
        offset_ms += duration_ms


def _provider_span(session: TraceSession, call: ProviderCall) -> None:
    attributes = call.model_dump(mode="json")
    attributes.pop("request_id", None)
    succeeded = (call.status_category or "").casefold() in {
        "success",
        "completed",
    }
    phase = session.start_span(
        f"provider-phase.{call.operation}",
        _provider_kind(call.operation),
        parent_span_id=session.root.span_id,
        attributes={"operation": call.operation},
        historical_duration_ms=call.elapsed_ms,
    )
    session.completed_span(
        TraceSpanSpec(
            name=f"provider.{call.operation}",
            kind=_provider_kind(call.operation),
            parent_span_id=phase.span_id,
            reason_code=(
                DecisionCode.PROVIDER_CALLED
                if call.call_count > 0 and succeeded
                else DecisionCode.PROVIDER_FAILED
            ),
            attributes=attributes,
            duration_ms=call.elapsed_ms,
        )
    )
    failed = not succeeded
    session.finish_span(
        phase,
        TraceSpanFinish(
            status=SpanStatus.ERROR if failed else SpanStatus.OK,
            reason_code=(
                DecisionCode.PROVIDER_FAILED
                if failed
                else DecisionCode.PROVIDER_CALLED
            ),
            attributes={"operation": call.operation},
        ),
    )


def _detail_dict(detail: TraceDetail) -> dict[str, object]:
    return {
        "trace": _enum_dict(asdict(detail.trace)),
        "spans": [_enum_dict(asdict(span)) for span in detail.spans],
        "candidate_decisions": [
            _enum_dict(asdict(item)) for item in detail.candidate_decisions
        ],
        "artifacts": [_enum_dict(asdict(item)) for item in detail.artifacts],
        "legacy_flat_events": (),
    }


def _canonical_json_bytes(value: object) -> bytes:
    """把兼容占位和 flat event 视图编码为稳定 UTF-8 JSON。"""
    return json.dumps(
        _enum_dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _enum_dict(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum) and isinstance(value.value, str):
        return value.value
    if isinstance(value, dict):
        return {str(key): _enum_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enum_dict(item) for item in value]
    return value


def _event_reason(stage: str, attributes: dict[str, object]) -> DecisionCode:
    if stage == "cache":
        reason = (
            DecisionCode.CACHE_HIT
            if attributes.get("result") == "hit"
            else DecisionCode.CACHE_MISS
        )
    elif stage == "rewrite":
        if attributes.get("accepted") is True:
            reason = DecisionCode.REWRITE_OK
        elif attributes.get("attempted") is True:
            reason = DecisionCode.MODEL_ABSTAINED
        else:
            reason = DecisionCode.SKIPPED
    elif stage == "confidence" and attributes.get("status") not in {
        "answerable",
        "ANSWERABLE",
    }:
        reason = DecisionCode.BELOW_THRESHOLD
    elif stage == "generate" and attributes.get("mode") in {None, "none"}:
        reason = DecisionCode.MODEL_ABSTAINED
    else:
        reason = {
            "snapshot": DecisionCode.AUTHORIZED_SCOPE,
            "analyze": DecisionCode.CONTEXT_LOADED,
            "plan": DecisionCode.RETRIEVAL_OK,
            "exact": DecisionCode.EXACT_MATCH,
            "lexical": DecisionCode.LEXICAL_MATCH,
            "query_embedding_route": DecisionCode.DENSE_MATCH,
            "dense": DecisionCode.DENSE_MATCH,
            "fuse": DecisionCode.FUSION_SELECTED,
            "hydrate": DecisionCode.HYDRATED,
            "rerank": DecisionCode.RERANK_SELECTED,
            "expand_neighbors": DecisionCode.NEIGHBOR_EXPANDED,
            "assemble_evidence": DecisionCode.EVIDENCE_SELECTED,
            "confidence": DecisionCode.CONFIDENCE_ACCEPTED,
            "generate": DecisionCode.GENERATION_CALLED,
            "validate": DecisionCode.VALIDATION_OK,
            "complete": DecisionCode.PUBLISHED,
        }.get(stage, DecisionCode.STARTED)
    return reason


def _channel_reason(channel: str) -> DecisionCode:
    if channel.startswith("exact"):
        return DecisionCode.EXACT_MATCH
    if channel.startswith("lexical"):
        return DecisionCode.LEXICAL_MATCH
    if channel.startswith("dense"):
        return DecisionCode.DENSE_MATCH
    return DecisionCode.RETRIEVAL_OK


def _span_kind(stage: str) -> SpanKind:
    if "embed" in stage or stage == "dense":
        return SpanKind.EMBEDDING
    if "rerank" in stage:
        return SpanKind.RERANKER
    if stage in {"generate", "rewrite"}:
        return SpanKind.LLM
    if stage in {"confidence", "validate", "repair"}:
        return SpanKind.GUARDRAIL
    if any(word in stage for word in ("persist", "hydrate", "snapshot")):
        return SpanKind.STORAGE
    return SpanKind.RETRIEVER


def _provider_kind(operation: str) -> SpanKind:
    if "embed" in operation:
        return SpanKind.EMBEDDING
    if "rerank" in operation:
        return SpanKind.RERANKER
    return SpanKind.LLM


def _duration(attributes: dict[str, object]) -> int:
    value = attributes.get("elapsed_ms")
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _positive_int(value: object, *, default: int) -> int:
    """只接受正整数事件字段，拒绝将正文或容器强转为数字。"""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _provider_call_count(
    result: SearchAnswerResult | None,
    error: RagError | None,
    cancelled_calls: tuple[ProviderCall, ...] = (),
) -> int:
    if result is not None and result.diagnostics is not None:
        return sum(
            item.call_count for item in result.diagnostics.provider_calls
        )
    calls = (
        cancelled_calls
        if cancelled_calls
        else (() if error is None else error.provider_calls)
    )
    return sum(call.call_count for call in calls)


__all__ = ["ProductTraceCoordinator", "TraceMode", "TraceUnavailableError"]
