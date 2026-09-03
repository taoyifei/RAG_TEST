"""P09 稳定 v1 HTTP API 与错误映射。"""

from __future__ import annotations

import hmac
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from rag_app.api.p09_schemas import (
    CreateKnowledgeBaseRequest,
    CreateProjectRequest,
    ErrorEnvelope,
    QueryRequest,
    QueryResponse,
    RenameDocumentRequest,
    UpdateKnowledgeBaseRequest,
    UpdateProjectRequest,
)
from rag_app.composition.p09_runtime import P09Runtime
from rag_app.core.errors import PolicyDenied, RagError
from rag_app.core.identifiers import deterministic_id, new_id
from rag_app.core.models import (
    Document,
    DocumentVersion,
    Job,
    KnowledgeBase,
    Project,
    ProjectStatus,
    SystemStatus,
)
from rag_app.core.models.search import SearchAnswerResult

_MAX_UPLOAD_BYTES = 32 * 1024 * 1024
_HTTP_UNAUTHORIZED = 401
_ERROR_STATUS = {
    "CHANNEL_UNAVAILABLE": 503,
    "CONFLICT": 409,
    "CONFLICT_ACTIVE_WRITER": 409,
    "DENSE_UNCALIBRATED": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "INDEX_CORRUPT": 500,
    "INDEX_NOT_READY": 409,
    "INVALID_DOCUMENT": 422,
    "NOT_FOUND": 404,
    "POLICY_DENIED": 403,
    "PROVIDER_UNAVAILABLE": 503,
    "QUEUE_LIMIT_EXCEEDED": 429,
    "REINDEX_REQUIRED": 409,
    "REVISION_STATE_ERROR": 409,
    "UPLOAD_TOO_LARGE": 413,
    "VALIDATION_FAILED": 422,
}
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope, "description": "统一安全错误结构"}
    for status in (400, 401, 403, 404, 409, 413, 422, 429, 500, 503)
}


def create_p09_app(  # noqa: PLR0915
    runtime: P09Runtime,
    *,
    query_token: str,
    admin_token: str,
    debug_enabled: bool = False,
    max_upload_bytes: int = _MAX_UPLOAD_BYTES,
) -> FastAPI:
    """创建仅暴露稳定 schema、默认无交互文档的 P09 应用。

    Args:
        runtime: 已装配 P06—P09 Application Services 的运行时。
        query_token: Search/Answer Bearer Token。
        admin_token: 生命周期、Job、Probe 与 Debug Bearer Token。
        debug_enabled: 是否允许管理员读取完整安全诊断。
        max_upload_bytes: 单次上传硬上限。

    Returns:
        已注册 v1 路由和统一错误处理的 FastAPI 应用。

    Raises:
        ValueError: 令牌为空或相同。

    """
    if not query_token or not admin_token or query_token == admin_token:
        raise ValueError("查询与管理令牌必须非空且不同。")
    if max_upload_bytes <= 0:
        raise ValueError("上传上限必须为正数。")
    app = FastAPI(
        title="Universal RAG API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        responses=_ERROR_RESPONSES,
    )
    _register_error_handlers(app)

    def _require_query(authorization: str | None) -> None:
        _require_bearer(authorization, query_token)

    def _require_admin(authorization: str | None) -> None:
        _require_bearer(authorization, admin_token)

    @app.get("/live", tags=["status"])
    def _live() -> dict[str, str]:
        """返回进程存活状态。"""
        return {"status": "live"}

    @app.get("/ready", tags=["status"], response_model=SystemStatus)
    def _ready() -> dict[str, object]:
        """返回本地依赖与质量边界，不调用 Provider。"""
        return _model(runtime.sdk.health())

    @app.post(
        "/api/v1/projects",
        status_code=201,
        tags=["projects"],
        response_model=Project,
    )
    def _create_project(
        body: CreateProjectRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """创建项目。"""
        _require_admin(authorization)
        return _model(
            runtime.sdk.create_project(
                body.name, idempotency_key=idempotency_key
            )
        )

    @app.get("/api/v1/projects", tags=["projects"])
    def _list_projects(
        authorization: Annotated[str | None, Header()] = None,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        """稳定分页读取项目。"""
        _require_admin(authorization)
        items = runtime.sdk.list_projects(limit=page_size, offset=offset)
        return _page(items, page_size, offset)

    @app.get(
        "/api/v1/projects/{project_id}",
        tags=["projects"],
        response_model=Project,
    )
    def _get_project(
        project_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取项目。"""
        _require_admin(authorization)
        return _model(runtime.sdk.get_project(project_id))

    @app.patch(
        "/api/v1/projects/{project_id}",
        tags=["projects"],
        response_model=Project,
    )
    def _update_project(
        project_id: str,
        body: UpdateProjectRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """更新项目允许修改的字段。"""
        _require_admin(authorization)
        return _model(
            runtime.sdk.update_project(
                project_id, name=body.name, status=body.status
            )
        )

    @app.delete(
        "/api/v1/projects/{project_id}",
        tags=["projects"],
        response_model=Project,
    )
    def _archive_project(
        project_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """归档项目，不删除物理对象。"""
        _require_admin(authorization)
        return _model(
            runtime.sdk.update_project(
                project_id, status=ProjectStatus.ARCHIVED
            )
        )

    @app.post(
        "/api/v1/projects/{project_id}/knowledge-bases",
        status_code=201,
        tags=["knowledge-bases"],
        response_model=KnowledgeBase,
    )
    def _create_knowledge_base(
        project_id: str,
        body: CreateKnowledgeBaseRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """在项目内创建知识库。"""
        _require_admin(authorization)
        return _model(
            runtime.sdk.create_knowledge_base(
                project_id,
                body.name,
                description=body.description,
                idempotency_key=idempotency_key,
            )
        )

    @app.get(
        "/api/v1/projects/{project_id}/knowledge-bases",
        tags=["knowledge-bases"],
    )
    def _list_knowledge_bases(
        project_id: str,
        authorization: Annotated[str | None, Header()] = None,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        """稳定分页读取知识库。"""
        _require_admin(authorization)
        items = runtime.sdk.list_knowledge_bases(
            project_id, limit=page_size, offset=offset
        )
        return _page(items, page_size, offset)

    @app.get(
        "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}",
        tags=["knowledge-bases"],
        response_model=KnowledgeBase,
    )
    def _get_knowledge_base(
        project_id: str,
        kb_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """按 scope 读取知识库。"""
        _require_admin(authorization)
        return _model(runtime.sdk.get_knowledge_base(project_id, kb_id))

    @app.patch(
        "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}",
        tags=["knowledge-bases"],
        response_model=KnowledgeBase,
    )
    def _update_knowledge_base(
        project_id: str,
        kb_id: str,
        body: UpdateKnowledgeBaseRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """更新知识库显示字段或状态。"""
        _require_admin(authorization)
        return _model(
            runtime.sdk.update_knowledge_base(
                project_id,
                kb_id,
                name=body.name,
                description=body.description,
                status=body.status,
            )
        )

    @app.delete(
        "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}",
        tags=["knowledge-bases"],
        response_model=KnowledgeBase,
    )
    def _delete_knowledge_base(
        project_id: str,
        kb_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """将知识库置为 deleting，不删除物理对象。"""
        _require_admin(authorization)
        return _model(runtime.sdk.delete_knowledge_base(project_id, kb_id))

    _register_document_routes(
        app,
        runtime,
        _require_admin,
        max_upload_bytes=max_upload_bytes,
    )
    _register_job_routes(app, runtime, _require_admin)
    _register_query_routes(
        app,
        runtime,
        _require_query,
        _require_admin,
        debug_enabled=debug_enabled,
    )
    _register_trace_routes(app, runtime, _require_admin)
    _register_status_routes(app, runtime, _require_admin)
    return app


def _register_document_routes(
    app: FastAPI,
    runtime: P09Runtime,
    require_admin: Callable[[str | None], None],
    *,
    max_upload_bytes: int,
) -> None:
    """注册文档、版本和 Artifact 路由。"""
    base = "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"

    @app.post(
        base + "/documents",
        status_code=202,
        tags=["documents"],
        response_model=Job,
    )
    async def _create_document(  # noqa: PLR0913, PLR0917
        project_id: str,
        kb_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        display_name: Annotated[str, Query(min_length=1, max_length=512)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """受控接收 DOCX 并创建新逻辑文档。"""
        require_admin(authorization)
        content = await _spool_upload(
            request, runtime.data_dir, max_upload_bytes=max_upload_bytes
        )
        return _model(
            runtime.sdk.create_document(
                project_id,
                kb_id,
                display_name=_safe_display_name(display_name),
                content=content,
                media_type=_media_type(request),
                idempotency_key=idempotency_key,
            )
        )

    @app.get(base + "/documents", tags=["documents"])
    def _list_documents(
        project_id: str,
        kb_id: str,
        authorization: Annotated[str | None, Header()] = None,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        """稳定分页读取知识库文档。"""
        require_admin(authorization)
        items = runtime.sdk.list_documents(
            project_id, kb_id, limit=page_size, offset=offset
        )
        return _page(items, page_size, offset)

    @app.get(
        base + "/documents/{document_id}",
        tags=["documents"],
        response_model=Document,
    )
    def _get_document(
        project_id: str,
        kb_id: str,
        document_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取逻辑文档。"""
        require_admin(authorization)
        return _model(runtime.sdk.get_document(project_id, kb_id, document_id))

    @app.patch(
        base + "/documents/{document_id}",
        tags=["documents"],
        response_model=Document,
    )
    def _rename_document(
        project_id: str,
        kb_id: str,
        document_id: str,
        body: RenameDocumentRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """只修改 display name。"""
        require_admin(authorization)
        return _model(
            runtime.sdk.rename_document(
                project_id,
                kb_id,
                document_id,
                display_name=body.display_name,
            )
        )

    @app.delete(
        base + "/documents/{document_id}",
        tags=["documents"],
        response_model=Document,
    )
    def _delete_document(
        project_id: str,
        kb_id: str,
        document_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """创建受控删除状态，不直接删除 Blob 或 Collection。"""
        require_admin(authorization)
        return _model(
            runtime.sdk.delete_document(project_id, kb_id, document_id)
        )

    @app.post(
        base + "/documents/{document_id}/versions",
        status_code=202,
        tags=["documents"],
        response_model=Job,
    )
    async def _create_document_version(  # noqa: PLR0913, PLR0917
        project_id: str,
        kb_id: str,
        document_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """受控接收既有逻辑文档的新版本。"""
        require_admin(authorization)
        content = await _spool_upload(
            request, runtime.data_dir, max_upload_bytes=max_upload_bytes
        )
        return _model(
            runtime.sdk.create_document_version(
                project_id,
                kb_id,
                document_id,
                content=content,
                media_type=_media_type(request),
                idempotency_key=idempotency_key,
            )
        )

    @app.get(
        base + "/documents/{document_id}/versions",
        tags=["documents"],
    )
    def _list_versions(
        project_id: str,
        kb_id: str,
        document_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取不可变版本列表。"""
        require_admin(authorization)
        items = runtime.sdk.list_document_versions(
            project_id, kb_id, document_id
        )
        return {"items": [_model(item) for item in items]}

    @app.get(
        base + "/documents/{document_id}/versions/{dver}",
        tags=["documents"],
        response_model=DocumentVersion,
    )
    def _get_version(
        project_id: str,
        kb_id: str,
        document_id: str,
        dver: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取单个不可变版本。"""
        require_admin(authorization)
        return _model(
            runtime.sdk.get_document_version(
                project_id, kb_id, document_id, dver
            )
        )

    @app.get(
        base + "/documents/{document_id}/versions/{dver}/artifacts",
        tags=["artifacts"],
    )
    def _list_artifacts(
        project_id: str,
        kb_id: str,
        document_id: str,
        dver: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """按版本引用授权列出 Artifact。"""
        require_admin(authorization)
        items = runtime.sdk.list_artifacts(project_id, kb_id, document_id, dver)
        return {"items": [_model(item) for item in items]}

    @app.get(base + "/artifacts/{artifact_id}", tags=["artifacts"])
    def _download_artifact(  # noqa: PLR0913, PLR0917
        project_id: str,
        kb_id: str,
        artifact_id: str,
        document_id: str,
        document_version_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """经完整引用 scope 授权后下载 Artifact。"""
        require_admin(authorization)
        blob = runtime.sdk.read_artifact(
            project_id,
            kb_id,
            document_id,
            document_version_id,
            artifact_id,
        )
        return Response(
            content=blob.content,
            media_type=blob.media_type,
            headers={"Cache-Control": "no-store"},
        )


def _register_job_routes(
    app: FastAPI,
    runtime: P09Runtime,
    require_admin: Callable[[str | None], None],
) -> None:
    """注册可恢复 Job 查询与控制路由。"""

    @app.get("/api/v1/jobs/{job_id}", tags=["jobs"], response_model=Job)
    def _get_job(
        job_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取安全 Job 状态。"""
        require_admin(authorization)
        return _model(runtime.sdk.get_job(job_id))

    @app.post("/api/v1/jobs/{job_id}:cancel", tags=["jobs"], response_model=Job)
    def _cancel_job(
        job_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """持久化取消请求。"""
        require_admin(authorization)
        return _model(runtime.sdk.cancel_job(job_id))

    @app.post("/api/v1/jobs/{job_id}:retry", tags=["jobs"], response_model=Job)
    def _retry_job(
        job_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """将可重试 Job 放回队列。"""
        require_admin(authorization)
        return _model(runtime.sdk.retry_job(job_id))


def _register_query_routes(
    app: FastAPI,
    runtime: P09Runtime,
    require_query: Callable[[str | None], None],
    require_admin: Callable[[str | None], None],
    *,
    debug_enabled: bool,
) -> None:
    """注册 Search、Answer 和管理员 Diagnostics 路由。"""
    path = "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"

    @app.post(path + ":search", tags=["query"], response_model=QueryResponse)
    def _search(
        project_id: str,
        kb_id: str,
        body: QueryRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """返回 P08.5 实际路由与最小证据。"""
        require_query(authorization)
        result = runtime.sdk.search(
            project_id, kb_id, body.query, limit=body.limit
        )
        return _query_payload(runtime, result, project_id, kb_id)

    @app.post(
        path + ":answer",
        tags=["query"],
        response_model=None,
        responses={200: {"model": QueryResponse}},
    )
    def _answer(
        project_id: str,
        kb_id: str,
        body: QueryRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object] | StreamingResponse:
        """返回非流式结果或最终一致的 SSE。"""
        require_query(authorization)
        result = runtime.sdk.answer(
            project_id, kb_id, body.query, limit=body.limit
        )
        payload = _query_payload(runtime, result, project_id, kb_id)
        if not body.stream:
            return payload
        return StreamingResponse(
            _sse_events(payload),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, no-transform"},
        )

    @app.get(
        "/api/v1/admin/retrieval-diagnostics/{trace_id}",
        tags=["debug"],
    )
    def _diagnostics(
        trace_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """仅管理员和显式开发开关可读取完整安全诊断。"""
        require_admin(authorization)
        if not debug_enabled:
            raise PolicyDenied(
                "检索诊断端点未启用。", stage="retrieval.diagnostics"
            )
        return _model(runtime.sdk.retrieval_diagnostics(trace_id))


def _register_status_routes(
    app: FastAPI,
    runtime: P09Runtime,
    require_admin: Callable[[str | None], None],
) -> None:
    """注册系统状态与显式 Provider Probe。"""

    @app.get(
        "/api/v1/system/components",
        tags=["status"],
        response_model=SystemStatus,
    )
    def _components(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取 FTS、指纹、质量与对账状态。"""
        require_admin(authorization)
        return _model(runtime.sdk.health())

    @app.post("/api/v1/system/providers:probe", tags=["status"])
    def _probe(
        authorization: Annotated[str | None, Header()] = None,
        allow_network: Annotated[bool, Header(alias="X-Allow-Network")] = False,
        request_budget: Annotated[
            int, Header(alias="X-Request-Budget", ge=1, le=20)
        ] = 1,
    ) -> dict[str, object]:
        """仅在显式授权和预算下执行组件健康探测。"""
        require_admin(authorization)
        if not allow_network:
            raise PolicyDenied(
                "Provider Probe 需要显式网络授权。",
                stage="provider.probe",
            )
        providers = (
            runtime.retrieval_runtime.persistence.components.embedding_primary,
            runtime.retrieval_runtime.persistence.components.reranker,
        )
        results: list[dict[str, object]] = []
        for provider in providers[:request_budget]:
            health = getattr(provider, "health", None)
            if not callable(health):
                results.append(
                    {
                        "component": provider.descriptor.name,
                        "status": "probe_not_supported",
                    }
                )
                continue
            results.append(_model(health(network=True)))
        return {
            "request_budget": request_budget,
            "last_explicit_probe_at": datetime.now(UTC).isoformat(),
            "results": results,
        }


def _register_trace_routes(
    app: FastAPI,
    runtime: P09Runtime,
    require_admin: Callable[[str | None], None],
) -> None:
    """注册按 Trace、Query 或 Job 身份读取的安全事件接口。"""

    @app.get("/api/v1/admin/traces", tags=["debug"])
    def _trace_events(
        trace_id: str | None = None,
        query_id: str | None = None,
        job_id: str | None = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """按且仅按一个稳定身份读取脱敏事件。"""
        requested = tuple(
            value for value in (trace_id, query_id, job_id) if value is not None
        )
        if len(requested) != 1:
            raise ValueError("trace_id、query_id、job_id 必须且只能提供一个。")
        require_admin(authorization)
        job: Job | None = None
        resolved_trace_id = trace_id or query_id
        if job_id is not None:
            job = runtime.sdk.get_job(job_id)
            resolved_trace_id = deterministic_id(
                "trace", job.job_id, job.revision_id
            )
        if resolved_trace_id is None:
            raise AssertionError("Trace 查询身份必须已解析。")
        events = runtime.sdk.trace_events(resolved_trace_id)
        return {
            "trace_id": resolved_trace_id,
            "job": None if job is None else _model(job),
            "events": [_model(event) for event in events],
        }


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RagError)
    async def _rag_error_handler(
        request: Request, error: RagError
    ) -> JSONResponse:
        del request
        return _error_response(
            status_code=_ERROR_STATUS.get(error.code, 500),
            code=error.code,
            message=error.safe_message,
            stage=error.stage,
            retryable=error.retryable,
            trace_id=error.trace_id,
            details=dict(error.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del request, error
        return _error_response(
            status_code=422,
            code="INVALID_INPUT",
            message="请求字段无效。",
            stage="http.validation",
        )

    @app.exception_handler(HTTPException)
    async def _http_error_handler(
        request: Request, error: HTTPException
    ) -> JSONResponse:
        del request
        return _error_response(
            status_code=error.status_code,
            code="AUTHENTICATION_REQUIRED"
            if error.status_code == _HTTP_UNAUTHORIZED
            else "AUTHORIZATION_DENIED",
            message=str(error.detail),
            stage="http.auth",
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(
        request: Request, error: ValueError
    ) -> JSONResponse:
        del request, error
        return _error_response(
            status_code=400,
            code="INVALID_INPUT",
            message="请求语义无效。",
            stage="http.request",
        )

    @app.exception_handler(Exception)
    async def _unexpected_error_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        del request, error
        return _error_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="服务内部错误。",
            stage="http.internal",
        )


async def _spool_upload(
    request: Request, data_dir: Path, *, max_upload_bytes: int
) -> bytes:
    spool_dir = (data_dir / "upload-spool").resolve()
    if spool_dir.parent != data_dir.resolve():
        raise ValueError("上传暂存目录越出数据根。")
    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix="upload-", suffix=".tmp", dir=spool_dir
    )
    temporary_path = Path(temporary_name)
    observed = 0
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            async for chunk in request.stream():
                observed += len(chunk)
                if observed > max_upload_bytes:
                    raise RagError(
                        "上传超过服务端大小上限。",
                        stage="document.upload",
                        code="UPLOAD_TOO_LARGE",
                    )
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary_path.read_bytes()
    finally:
        temporary_path.unlink(missing_ok=True)


def _require_bearer(header: str | None, expected: str) -> None:
    if header is None:
        raise HTTPException(status_code=401, detail="Bearer token required")
    scheme, separator, token = header.partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not hmac.compare_digest(token, expected)
    ):
        raise HTTPException(status_code=403, detail="Bearer token denied")


def _safe_display_name(value: str) -> str:
    normalized = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    if not normalized or normalized in {".", ".."}:
        raise RagError(
            "文档显示名无效。", stage="document.upload", code="INVALID_INPUT"
        )
    return normalized


def _media_type(request: Request) -> str:
    value = request.headers.get("content-type", "application/octet-stream")
    return value.split(";", maxsplit=1)[0]


def _model(value: BaseModel) -> dict[str, object]:
    return cast(
        dict[str, object],
        jsonable_encoder(value.model_dump(mode="json")),
    )


def _page(
    items: Sequence[BaseModel], page_size: int, offset: int
) -> dict[str, object]:
    return {
        "items": [_model(item) for item in items],
        "page_size": page_size,
        "offset": offset,
        "next_offset": offset + len(items) if len(items) == page_size else None,
    }


def _query_payload(
    runtime: P09Runtime,
    result: SearchAnswerResult,
    project_id: str,
    knowledge_base_id: str,
) -> dict[str, object]:
    payload = _model(result)
    payload.update(
        {
            "query_id": result.trace_id,
            "project_id": project_id,
            "knowledge_base_id": knowledge_base_id,
            "index_revision_id": result.active_index_revision_id,
            "vector_name": result.selected_vector_name,
            "dense_available": result.selected_vector_name is not None,
            "rerank_mode": result.rerank_execution_mode,
            "trace_summary": payload.get("diagnostics_summary"),
        }
    )
    payload["evidence_count"] = len(result.evidence)
    payload["quality_profile_status"] = (
        "remote_live_calibrated"
        if runtime.sdk.health().remote_dense_confidence_calibrated
        else "offline_validated_remote_uncalibrated"
    )
    return payload


def _sse_events(payload: dict[str, object]) -> Iterator[bytes]:
    trace_id = payload["trace_id"]
    yield _sse("meta", {"trace_id": trace_id})
    yield _sse(
        "retrieval",
        {
            "trace_id": trace_id,
            "evidence_count": payload["evidence_count"],
            "diagnostics_summary": payload.get("diagnostics_summary"),
        },
    )
    yield _sse("final", payload)


def _sse(event: str, payload: object) -> bytes:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"event: {event}\ndata: {body}\n\n".encode()


def _error_response(  # noqa: PLR0913
    *,
    status_code: int,
    code: str,
    message: str,
    stage: str,
    retryable: bool = False,
    trace_id: str | None = None,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "stage": stage,
                "retryable": retryable,
                "trace_id": trace_id or new_id("trace"),
                "details": details or {},
            }
        },
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["create_p09_app"]
