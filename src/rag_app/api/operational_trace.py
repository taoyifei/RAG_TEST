"""Product Operational Trace 的稳定管理员 API。"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import NotFound, PolicyDenied, ProviderUnavailable
from rag_app.core.identifiers import new_id
from rag_app.product.trace_coordinator import (
    OperationalTracePayloadLimitError,
    OperationalTraceSnapshot,
)
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
    TraceStoreClosedError,
)

_MAX_BATCH_COUNT = 100
_MAX_BATCH_BYTES = 16 * 1024 * 1024
_TraceId = Annotated[
    str,
    StringConstraints(pattern=r"^(?:trace_)?[0-9a-f]{32}$"),
]


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


class MissingPreV3TraceRootResponse(BaseModel):
    """只有 History 时返回的不伪造技术细节占位根。"""

    model_config = ConfigDict(extra="forbid")
    trace_id: str
    schema_version: Literal["missing-pre-v3"]
    capture_complete: Literal[False]
    status: Literal["NOT_CAPTURED_BEFORE_V3"]
    project_id: str
    knowledge_base_id: str


class LegacyTraceEventResponse(BaseModel):
    """兼容两种 Trace ID 的旧 flat event DTO。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"]
    trace_id: _TraceId
    event_name: str
    occurred_at: datetime
    attributes: object


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
    trace: (
        TraceRootResponse
        | LegacyTraceRootResponse
        | MissingPreV3TraceRootResponse
    )
    spans: list[TraceSpanResponse]
    candidate_decisions: list[TraceCandidateDecisionResponse]
    artifacts: list[TraceArtifactResponse]
    legacy_flat_events: list[LegacyTraceEventResponse] = Field(
        default_factory=list
    )


class TraceExportRequest(BaseModel):
    """有界批量导出请求。"""

    model_config = ConfigDict(extra="forbid")
    trace_ids: list[_TraceId] = Field(
        min_length=1,
        max_length=_MAX_BATCH_COUNT,
    )

    @field_validator("trace_ids")
    @classmethod
    def _reject_duplicates(cls, values: list[str]) -> list[str]:
        """在业务路由前拒绝重复 ID，从而稳定返回 422。"""
        if len(values) != len(set(values)):
            raise ValueError("批量导出不能包含重复 Trace ID。")
        normalized = [
            value if value.startswith("trace_") else f"trace_{value}"
            for value in values
        ]
        if len(normalized) != len(set(normalized)):
            raise ValueError("批量导出不能包含等价的新旧重复 Trace ID。")
        return values


class _TechnicalTraceExportLimitError(ValueError):
    """技术 Trace 导出超过成员或总归档硬上限。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def register_operational_trace_routes(  # noqa: PLR0915
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

    @app.exception_handler(_TechnicalTraceExportLimitError)
    async def _technical_trace_limit_handler(
        request: Request,
        error: _TechnicalTraceExportLimitError,
    ) -> JSONResponse:
        del request
        return _trace_limit_response(error.reason_code)

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
    def _detail(trace_id: _TraceId, request: Request) -> dict[str, object]:
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
        trace_id: _TraceId,
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
    def _export(trace_id: _TraceId, request: Request) -> Response:
        try:
            with runtime.traces.export_snapshots(
                (trace_id,),
                max_total_payload_bytes=_MAX_BATCH_BYTES,
                authorize=lambda snapshot: _authorize_export_snapshot(
                    runtime,
                    request,
                    snapshot,
                ),
            ) as snapshots:
                snapshot = snapshots[0]
                if snapshot.state == "missing" or snapshot.payload is None:
                    raise NotFound(
                        "Operational Trace 不存在。",
                        stage="trace.export",
                        details={"missing_trace_ids": [trace_id]},
                    )
                payload = snapshot.payload
        except TraceArtifactLimitError as error:
            raise _TechnicalTraceExportLimitError(
                "TRACE_EXPORT_ITEM_BYTES_EXCEEDED"
            ) from error
        except OperationalTracePayloadLimitError as error:
            raise _TechnicalTraceExportLimitError(
                "TRACE_EXPORT_TOTAL_BYTES_EXCEEDED"
            ) from error
        except (ArtifactExpiredError, ArtifactNotFoundError) as error:
            raise NotFound(
                "Trace Artifact 不存在或已过期。",
                stage="trace.export",
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
        trace_ids = tuple(sorted(body.trace_ids))
        payloads: list[tuple[str, bytes]] = []
        total = 0
        try:
            with runtime.traces.export_snapshots(
                trace_ids,
                max_total_payload_bytes=_MAX_BATCH_BYTES,
                authorize=lambda snapshot: _authorize_export_snapshot(
                    runtime,
                    request,
                    snapshot,
                ),
            ) as snapshots:
                missing = [
                    snapshot.trace_id
                    for snapshot in snapshots
                    if snapshot.state == "missing"
                ]
                if missing:
                    raise NotFound(
                        "部分 Operational Trace 不存在。",
                        stage="trace.export",
                        details={"missing_trace_ids": missing},
                    )
                for snapshot in snapshots:
                    if snapshot.payload is None:
                        raise AssertionError("Trace 导出快照缺少 payload。")
                    total += len(snapshot.payload)
                    if total > _MAX_BATCH_BYTES:
                        raise _TechnicalTraceExportLimitError(
                            "TRACE_EXPORT_TOTAL_BYTES_EXCEEDED"
                        )
                    payloads.append((snapshot.trace_id, snapshot.payload))
        except TraceArtifactLimitError as error:
            raise _TechnicalTraceExportLimitError(
                "TRACE_EXPORT_ITEM_BYTES_EXCEEDED"
            ) from error
        except OperationalTracePayloadLimitError as error:
            raise _TechnicalTraceExportLimitError(
                "TRACE_EXPORT_TOTAL_BYTES_EXCEEDED"
            ) from error
        except (ArtifactExpiredError, ArtifactNotFoundError) as error:
            raise NotFound(
                "Trace Artifact 不存在或已过期。",
                stage="trace.export",
            ) from error
        archive_bytes = _trace_zip(payloads)
        if len(archive_bytes) > _MAX_BATCH_BYTES:
            raise _TechnicalTraceExportLimitError(
                "TRACE_EXPORT_COMPRESSED_BYTES_EXCEEDED"
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
        try:
            pruned = runtime.traces.store.prune(now=datetime.now().astimezone())
        except (sqlite3.Error, TraceStoreClosedError) as error:
            raise ProviderUnavailable(
                "Operational Trace 暂时无法清理。",
                code="TRACE_PERSISTENCE_UNAVAILABLE",
                stage="trace.prune",
                retryable=True,
            ) from error
        return {"pruned": pruned}


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


def _authorize_snapshot(
    runtime: ProductRuntime,
    request: Request,
    snapshot: OperationalTraceSnapshot,
) -> None:
    """使用解析器保留的 scope 鉴权，不依赖 legacy DTO 显示字段。"""
    token_id = getattr(request.state, "access_token_id", None)
    if token_id is None:
        return
    token = runtime.auth.get_access_token(token_id)
    if token.project_id is not None and snapshot.project_id != token.project_id:
        raise PolicyDenied("Trace 项目范围不匹配。", stage="trace.scope")
    if (
        token.knowledge_base_id is not None
        and snapshot.knowledge_base_id != token.knowledge_base_id
    ):
        raise PolicyDenied("Trace 知识库范围不匹配。", stage="trace.scope")


def _authorize_export_snapshot(
    runtime: ProductRuntime,
    request: Request,
    snapshot: OperationalTraceSnapshot,
) -> None:
    """在任何技术 payload 物化前完成 scope 与来源重鉴权。

    Args:
        runtime: 当前 Product Runtime。
        request: 带管理员会话或受限 API Token 的请求。
        snapshot: 不包含导出 payload 的 Trace metadata 快照。

    Returns:
        授权通过时无返回值；缺失项留给路由统一返回 404。

    """
    if snapshot.state == "missing":
        return
    _authorize_snapshot(runtime, request, snapshot)
    _reauthorize_snapshot_sources(runtime, snapshot)


def _reauthorize_snapshot_sources(
    runtime: ProductRuntime,
    snapshot: OperationalTraceSnapshot,
) -> None:
    """在下载时重新验证当前来源；旧/仅 History 也使用保存 scope。"""
    root: dict[str, object] = {
        "project_id": snapshot.project_id,
        "knowledge_base_id": snapshot.knowledge_base_id,
    }
    if snapshot.detail is not None:
        raw_root = snapshot.detail.get("trace")
        if isinstance(raw_root, dict) and isinstance(
            raw_root.get("document_id"), str
        ):
            root["document_id"] = raw_root["document_id"]
    _reauthorize_sources(runtime, root)


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


def _trace_limit_response(reason_code: str) -> JSONResponse:
    """把容量问题映射为独立于通用错误表的稳定 413。"""
    trace_id = new_id("trace")
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": reason_code,
                "message": "Operational Trace 导出超过容量上限。",
                "stage": "trace.export",
                "retryable": False,
                "trace_id": trace_id,
                "details": {},
            }
        },
        headers={"Cache-Control": "no-store", "X-Trace-Id": trace_id},
    )


__all__ = ["register_operational_trace_routes"]
