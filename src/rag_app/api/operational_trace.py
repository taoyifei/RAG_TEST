"""Product Operational Trace 的稳定管理员 API。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import NotFound, PolicyDenied
from rag_app.core.events import TraceEvent
from rag_app.tracing.models import (
    SpanKind,
    SpanStatus,
    TraceListFilter,
    TraceMode,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.store import (
    ArtifactExpiredError,
    ArtifactNotFoundError,
    TraceArtifactLimitError,
    TraceNotFoundError,
)

_MAX_BATCH_COUNT = 50
_MAX_BATCH_BYTES = 16 * 1024 * 1024


class TraceRootResponse(BaseModel):
    """列表和详情共用的根 Trace DTO。"""

    model_config = ConfigDict(extra="forbid")
    trace_id: str
    schema_version: str
    mode: TraceMode
    kind: str
    status: TraceStatus
    created_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    pipeline_fingerprint: str
    serving_fingerprint: str
    release_revision: str
    active_collection: str
    index_manifest_sha256: str
    payload_schema_version: int
    refusal_code: str | None
    error_code: str | None
    feedback_useful: bool | None
    expires_at: datetime
    project_id: str | None
    knowledge_base_id: str | None
    owner_sha256: str | None
    request_id: str | None
    job_id: str | None
    document_id: str | None
    revision_id: str | None
    profile_id: str | None
    index_fingerprint: str | None
    source_revision: str | None
    capture_complete: bool
    capture_incomplete_reason: str | None
    dropped_span_count: int
    dropped_decision_count: int
    writer_queue_high_water: int


class LegacyTraceRootResponse(BaseModel):
    """迁移前只有平面事件时的显式不完整根 DTO。"""

    model_config = ConfigDict(extra="forbid")
    trace_id: str
    schema_version: Literal["legacy-flat-1"]
    capture_complete: Literal[False]
    capture_incomplete_reason: Literal["legacy_flat_events"]


class TraceSpanResponse(BaseModel):
    """Waterfall 中的一个稳定 Span DTO。"""

    model_config = ConfigDict(extra="forbid")
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
    attributes: dict[str, object]
    input_artifact_id: str | None
    output_artifact_id: str | None


class TraceCandidateDecisionResponse(BaseModel):
    """候选漏斗中的一次选择或淘汰 DTO。"""

    model_config = ConfigDict(extra="forbid")
    trace_id: str
    sequence: int
    stage: str
    chunk_id: str
    selected: bool
    reason_code: DecisionCode
    details: dict[str, object]
    candidate_id: str | None
    evidence_id: str | None
    channel: str | None
    rank: int | None
    score_type: str | None
    score: float | None
    contribution: float | None


class TraceArtifactResponse(BaseModel):
    """FULL Artifact 的元数据 DTO，不内联内容。"""

    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    trace_id: str
    kind: str
    media_type: str
    sha256: str
    original_bytes: int
    compressed_bytes: int
    created_at: datetime


class TracePageResponse(BaseModel):
    """稳定倒序的 Operational Trace 列表页。"""

    items: list[TraceRootResponse]
    page: int
    page_size: int
    total: int


class TraceDetailResponse(BaseModel):
    """Trace waterfall、候选漏斗与 Artifact 元数据。"""

    model_config = ConfigDict(extra="forbid")
    trace: TraceRootResponse | LegacyTraceRootResponse
    spans: list[TraceSpanResponse]
    candidate_decisions: list[TraceCandidateDecisionResponse]
    artifacts: list[TraceArtifactResponse]
    legacy_flat_events: list[TraceEvent] = Field(default_factory=list)


class TraceExportRequest(BaseModel):
    """有界批量导出请求。"""

    model_config = ConfigDict(extra="forbid")
    trace_ids: list[str] = Field(
        min_length=1,
        max_length=_MAX_BATCH_COUNT,
    )


def register_operational_trace_routes(
    app: FastAPI, runtime: ProductRuntime
) -> None:
    """注册列表、详情、惰性 Artifact 和稳定导出。

    Args:
        app: 当前 Product FastAPI 应用。
        runtime: 提供 Trace Store、授权和来源重鉴权的 Product runtime。

    Returns:
        无返回值；路由直接注册到应用。

    """
    base = "/api/v1/admin/operational-traces"

    @app.get(base, tags=["operational-trace"], response_model=TracePageResponse)
    def _list(  # noqa: PLR0913, PLR0917
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        trace_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        kind: str | None = None,
        status: TraceStatus | None = None,
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        request_id: str | None = None,
        job_id: str | None = None,
        document_id: str | None = None,
        revision_id: str | None = None,
        refusal_code: str | None = None,
        error_code: str | None = None,
        capture_mode: TraceMode | None = None,
        capture_complete: bool | None = None,
        feedback_useful: bool | None = None,
    ) -> dict[str, object]:
        project_id, knowledge_base_id = _token_list_scope(
            runtime,
            request,
            project_id,
            knowledge_base_id,
        )
        result = runtime.traces.list_traces(
            TraceListFilter(
                page=page,
                page_size=page_size,
                trace_id=trace_id,
                created_from=created_from,
                created_to=created_to,
                status=status,
                refusal_code=refusal_code,
                error_code=error_code,
                feedback_useful=feedback_useful,
                kind=kind,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                request_id=request_id,
                job_id=job_id,
                document_id=document_id,
                revision_id=revision_id,
                capture_mode=capture_mode,
                capture_complete=capture_complete,
            )
        )
        return cast(dict[str, object], jsonable_encoder(result))

    @app.get(
        base + "/{trace_id}",
        tags=["operational-trace"],
        response_model=TraceDetailResponse,
    )
    def _detail(trace_id: str, request: Request) -> dict[str, object]:
        try:
            payload = runtime.traces.legacy_detail(trace_id)
        except TraceNotFoundError as error:
            raise NotFound(
                "Operational Trace 不存在。", stage="trace.read"
            ) from error
        _authorize_root(runtime, request, payload["trace"])
        return payload

    @app.get(
        base + "/{trace_id}/artifacts/{artifact_id}",
        tags=["operational-trace"],
    )
    def _artifact(
        trace_id: str,
        artifact_id: str,
        request: Request,
    ) -> Response:
        root = _root(runtime, trace_id)
        _authorize_root(runtime, request, root)
        _reauthorize_sources(runtime, root)
        try:
            artifact = runtime.traces.artifact(trace_id, artifact_id)
        except (ArtifactExpiredError, ArtifactNotFoundError) as error:
            raise NotFound(
                "Trace Artifact 不存在或已过期。", stage="trace.artifact"
            ) from error
        return Response(
            artifact.payload,
            media_type=artifact.metadata.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": artifact.metadata.sha256,
            },
        )

    @app.get(base + "/{trace_id}/export", tags=["operational-trace"])
    def _export(trace_id: str, request: Request) -> Response:
        root = _root(runtime, trace_id)
        _authorize_root(runtime, request, root)
        _reauthorize_sources(runtime, root)
        try:
            payload = runtime.traces.export(trace_id)
        except TraceArtifactLimitError as error:
            raise PolicyDenied(
                "Trace 导出超过容量上限。", stage="trace.export"
            ) from error
        return Response(
            payload,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{trace_id}.json"'
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(base + ":export", tags=["operational-trace"])
    def _batch_export(body: TraceExportRequest, request: Request) -> Response:
        trace_ids = sorted(set(body.trace_ids))
        if len(trace_ids) != len(body.trace_ids):
            raise ValueError("批量导出不能包含重复 Trace ID。")
        payloads: list[tuple[str, bytes]] = []
        total = 0
        for trace_id in trace_ids:
            root = _root(runtime, trace_id)
            _authorize_root(runtime, request, root)
            _reauthorize_sources(runtime, root)
            payload = runtime.traces.export(trace_id)
            total += len(payload)
            if total > _MAX_BATCH_BYTES:
                raise PolicyDenied(
                    "批量 Trace 导出超过总字节上限。",
                    stage="trace.export",
                )
            payloads.append((trace_id, payload))
        archive_bytes = _trace_zip(payloads)
        if len(archive_bytes) > _MAX_BATCH_BYTES:
            raise PolicyDenied(
                "批量 Trace ZIP 超过总字节上限。", stage="trace.export"
            )
        return Response(
            archive_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    'attachment; filename="operational-traces.zip"'
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(base + ":prune", tags=["operational-trace"])
    def _prune(request: Request) -> dict[str, int]:
        if getattr(request.state, "product_principal", None) not in {
            "admin_session",
            "legacy_admin",
        }:
            raise PolicyDenied(
                "Trace 清理只允许管理员会话。",
                stage="trace.prune",
            )
        return {
            "pruned": runtime.traces.store.prune(
                now=datetime.now().astimezone()
            )
        }


def _root(runtime: ProductRuntime, trace_id: str) -> dict[str, object]:
    try:
        payload = runtime.traces.legacy_detail(trace_id)
    except TraceNotFoundError as error:
        raise NotFound(
            "Operational Trace 不存在。",
            stage="trace.read",
        ) from error
    root = payload["trace"]
    if not isinstance(root, dict):
        raise PolicyDenied("Trace 根身份无效。", stage="trace.scope")
    return cast(dict[str, object], root)


def _token_list_scope(
    runtime: ProductRuntime,
    request: Request,
    project_id: str | None,
    knowledge_base_id: str | None,
) -> tuple[str | None, str | None]:
    token_id = getattr(request.state, "access_token_id", None)
    if token_id is None:
        return project_id, knowledge_base_id
    token = runtime.auth.get_access_token(token_id)
    if token.project_id is not None:
        if project_id not in (None, token.project_id):
            raise PolicyDenied("Trace 项目范围不匹配。", stage="trace.scope")
        project_id = token.project_id
    if token.knowledge_base_id is not None:
        if knowledge_base_id not in (None, token.knowledge_base_id):
            raise PolicyDenied("Trace 知识库范围不匹配。", stage="trace.scope")
        knowledge_base_id = token.knowledge_base_id
    return project_id, knowledge_base_id


def _authorize_root(
    runtime: ProductRuntime,
    request: Request,
    raw_root: object,
) -> None:
    if not isinstance(raw_root, dict):
        raise PolicyDenied("Trace 根身份无效。", stage="trace.scope")
    token_id = getattr(request.state, "access_token_id", None)
    if token_id is None:
        return
    token = runtime.auth.get_access_token(token_id)
    if (
        token.project_id is not None
        and raw_root.get("project_id") != token.project_id
    ):
        raise PolicyDenied("Trace 项目范围不匹配。", stage="trace.scope")
    if (
        token.knowledge_base_id is not None
        and raw_root.get("knowledge_base_id") != token.knowledge_base_id
    ):
        raise PolicyDenied("Trace 知识库范围不匹配。", stage="trace.scope")


def _reauthorize_sources(
    runtime: ProductRuntime, root: dict[str, object]
) -> None:
    project_id = root.get("project_id")
    knowledge_base_id = root.get("knowledge_base_id")
    if isinstance(project_id, str) and isinstance(knowledge_base_id, str):
        try:
            runtime.sdk.get_knowledge_base(project_id, knowledge_base_id)
            document_id = root.get("document_id")
            if isinstance(document_id, str):
                runtime.sdk.get_document(
                    project_id,
                    knowledge_base_id,
                    document_id,
                )
        except NotFound as error:
            raise PolicyDenied(
                "Trace 来源已删除或无权读取。", stage="trace.source"
            ) from error


def _trace_zip(payloads: list[tuple[str, bytes]]) -> bytes:
    manifest = {
        "schema_version": "1",
        "items": [
            {
                "trace_id": trace_id,
                "path": f"traces/{trace_id}.json",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            for trace_id, payload in payloads
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        _write_stable_zip_member(
            archive,
            "manifest.json",
            json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        for trace_id, payload in payloads:
            _write_stable_zip_member(
                archive,
                f"traces/{trace_id}.json",
                payload,
            )
    return buffer.getvalue()


def _write_stable_zip_member(
    archive: zipfile.ZipFile,
    path: str,
    payload: bytes,
) -> None:
    """以固定时间、权限与压缩方式写入可复现 ZIP 成员。"""
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload, compresslevel=9)


__all__ = ["register_operational_trace_routes"]
