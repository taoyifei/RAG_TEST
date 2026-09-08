"""协调 Product Query History 与 Operational Trace 的唯一适配层。"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum

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
from rag_app.product.query_history import ProductQueryHistory
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
from rag_app.tracing.store import TraceNotFoundError, TraceStore

_LOGGER = logging.getLogger(__name__)


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
        self._sessions: dict[str, TraceSession] = {}
        self._ingestion_spans: dict[
            tuple[str, str, str, int], TraceSpanHandle
        ] = {}
        self._prepared_modes: dict[str, TraceMode] = {}
        self.store.recover_running()

    def prepare(self, trace_id: str, mode: TraceMode) -> None:
        """在查询读取 snapshot 前冻结 capture mode 并执行 FULL 准入。"""
        if mode is TraceMode.FULL:
            self.recorder.require_full_capacity()
        with self._lock:
            self._prepared_modes[trace_id] = mode

    def start(
        self,
        trace_id: str,
        scope: KnowledgeBaseScope,
        question: str,
        *,
        owner_id: str,
        save_body: bool,
    ) -> None:
        """先建立权威 History，再以相同 ID 开始 Operational Trace。"""
        mode = self._take_mode(trace_id)
        self.history.start(
            trace_id,
            scope,
            question,
            owner_id=owner_id,
            save_body=save_body,
        )
        identity = self._identity(
            kind="query",
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            owner_id=owner_id,
            request_id=trace_id,
        )
        try:
            session = self.recorder.begin_query(
                trace_id,
                mode,
                datetime.now(UTC),
                identity,
                question_sha256=hashlib.sha256(question.encode()).hexdigest(),
            )
        except Exception as error:
            self.recorder.capture_failed(trace_id)
            if mode is TraceMode.FULL:
                self._finish_history_after_start_failure(trace_id, error)
                raise
            self._record_capture_degradation(trace_id)
            return
        with self._lock:
            self._sessions[trace_id] = session
        session.completed_span(
            TraceSpanSpec(
                name="request.admission",
                kind=SpanKind.HTTP,
                parent_span_id=session.root.span_id,
                reason_code=DecisionCode.ADMISSION_ALLOWED,
                attributes={
                    "project_id": scope.project_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "owner_sha256": identity.owner_sha256,
                },
            )
        )

    def finish(
        self,
        trace_id: str,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
    ) -> None:
        """一次结算 History 与 Operational Trace，并保留原查询错误。"""
        history_failure: RagError | None = None
        try:
            self.history.finish(
                trace_id,
                result=result,
                error=error,
                cancelled=cancelled,
            )
        except RagError as failure:
            history_failure = failure
            if error is not None or cancelled:
                _LOGGER.error(
                    "History 终态写入失败但保留原查询终态 trace_id=%s",
                    trace_id,
                )
        with self._lock:
            session = self._sessions.pop(trace_id, None)
        if session is not None:
            try:
                self._finalize_query_trace(
                    session,
                    result=result,
                    error=error or history_failure,
                    cancelled=cancelled,
                    history_written=history_failure is None,
                )
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
        """保留旧平面事件，并同步形成有界层级 Operational Trace。"""
        try:
            self.history.record(event)
        except RagError:
            self.recorder.capture_failed(event.trace_id)
            self.recorder.mark_capture_incomplete(
                event.trace_id,
                reason=DecisionCode.TRACE_CAPTURE_FAILED.value,
            )
        session = self._session_for_event(event)
        if session is None:
            return
        if event.event_name == "retrieval.snapshot":
            attributes = dict(event.attributes)
            self.recorder.update_trace_identity(
                event.trace_id,
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
        """继续提供旧 flat event 读取能力。"""
        return self.history.events(trace_id)

    def diagnostics(self, trace_id: str) -> RetrievalDiagnostics:
        """继续从加密 History 元数据读取检索诊断。"""
        return self.history.diagnostics(trace_id)

    def list_traces(self, filters: TraceListFilter) -> TracePage:
        """排空已确认写入后读取有界 Operational Trace 列表。"""
        self.recorder.flush()
        return self.store.list_traces(filters)

    def detail(self, trace_id: str) -> TraceDetail:
        """读取稳定 span tree、候选与 Artifact 元数据。"""
        self.recorder.flush()
        return self.store.get_trace(trace_id)

    def artifact(self, trace_id: str, artifact_id: str) -> ArtifactContent:
        """惰性读取并验证 FULL Artifact。"""
        self.recorder.flush()
        return self.store.get_artifact(trace_id, artifact_id)

    def export(self, trace_id: str) -> bytes:
        """导出单条 canonical JSON。"""
        self.recorder.flush()
        return self.store.export_trace(trace_id)

    def metrics(self) -> dict[str, int]:
        """返回 Trace writer 性能计数。"""
        return self.recorder.metrics

    def legacy_detail(self, trace_id: str) -> dict[str, object]:
        """为迁移前 flat events 返回明确不完整的兼容视图。"""
        try:
            detail = self.detail(trace_id)
        except TraceNotFoundError:
            events = self.history.events(trace_id)
            if not events:
                raise
            return {
                "trace": {
                    "trace_id": trace_id,
                    "schema_version": "legacy-flat-1",
                    "capture_complete": False,
                    "capture_incomplete_reason": "legacy_flat_events",
                },
                "spans": (),
                "candidate_decisions": (),
                "artifacts": (),
                "legacy_flat_events": [
                    event.model_dump(mode="json") for event in events
                ],
            }
        return _detail_dict(detail)

    def close(self) -> None:
        """按 writer flush、Store、History 的顺序幂等关闭。"""
        self.recorder.close()
        self.history.close()

    def _take_mode(self, trace_id: str) -> TraceMode:
        with self._lock:
            return self._prepared_modes.pop(trace_id, TraceMode.SAFE)

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

    def _session_for_event(self, event: TraceEvent) -> TraceSession | None:
        with self._lock:
            current = self._sessions.get(event.trace_id)
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
        with self._lock:
            existing = self._sessions.setdefault(event.trace_id, session)
        return existing

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
            with self._lock:
                self._sessions.pop(event.trace_id, None)
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

    def _finalize_query_trace(
        self,
        session: TraceSession,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
        history_written: bool,
    ) -> None:
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
        session.completed_span(
            TraceSpanSpec(
                name="history.settlement",
                kind=SpanKind.STORAGE,
                parent_span_id=session.root.span_id,
                reason_code=(
                    DecisionCode.PUBLISHED
                    if history_written
                    else DecisionCode.TRACE_CAPTURE_FAILED
                ),
                attributes={"written": history_written},
            )
        )
        if cancelled:
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
                "history_written": history_written,
                "provider_call_count": _provider_call_count(result, error),
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
                if call.call_count > 0
                and call.status_category not in {"error", "failed"}
                else DecisionCode.PROVIDER_FAILED
            ),
            attributes=attributes,
            duration_ms=call.elapsed_ms,
        )
    )
    failed = call.status_category in {"error", "failed"}
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
    result: SearchAnswerResult | None, error: RagError | None
) -> int:
    if result is not None and result.diagnostics is not None:
        return sum(
            item.call_count for item in result.diagnostics.provider_calls
        )
    calls = () if error is None else error.provider_calls
    return sum(call.call_count for call in calls)


__all__ = ["ProductTraceCoordinator", "TraceMode", "TraceUnavailableError"]
