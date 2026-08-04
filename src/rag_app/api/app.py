"""FastAPI 应用工厂、鉴权查询与索引管理端点。"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from rag_app.api.schemas import (
    ChatRequest,
    CreateJobRequest,
    FeedbackRequest,
)
from rag_app.api.stream import QueryStreamRequest, stream_query
from rag_app.health import ReadinessService
from rag_app.observability import StructuredAuditLogger
from rag_app.query_executor import QueryAdmissionError, QueryExecutor
from rag_app.query_service import QueryService
from rag_app.settings import RunMode
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.jobs import JobStore
from rag_app.state.models import Job
from rag_app.tracing.models import TraceListFilter, TraceMode, TraceStatus
from rag_app.tracing.recorder import (
    TraceRecorder,
    TraceUnavailableError,
)
from rag_app.tracing.store import (
    ArtifactExpiredError,
    ArtifactNotFoundError,
    TraceNotFoundError,
    TraceStore,
)

__all__ = ["ApiServices", "create_app"]

ServiceT = TypeVar("ServiceT")


@dataclass(frozen=True, slots=True)
class ApiServices:
    """API 的运行依赖与独立令牌。"""

    readiness: ReadinessService
    query_token: str
    admin_token: str
    run_mode: RunMode = RunMode.PRODUCTION
    query: QueryService | None = None
    query_executor: QueryExecutor | None = None
    conversations: ConversationStore | None = None
    jobs: JobStore | None = None
    feedback: FeedbackStore | None = None
    pipeline_fingerprint: str = ""
    frontend_dir: Path | None = None
    audit: StructuredAuditLogger | None = None
    trace_store: TraceStore | None = None
    trace_recorder: TraceRecorder | None = None

    def __post_init__(self) -> None:
        """拒绝令牌错误或不完整的业务 API 依赖。"""
        if not self.query_token or not self.admin_token:
            raise ValueError("查询与管理令牌不能为空。")
        if self.query_token == self.admin_token:
            raise ValueError("查询与管理令牌必须不同。")
        optional = (
            self.query,
            self.query_executor,
            self.conversations,
            self.jobs,
            self.feedback,
            self.pipeline_fingerprint or None,
        )
        if any(value is not None for value in optional) and any(
            value is None for value in optional
        ):
            raise ValueError("业务 API 依赖必须全部配置或全部省略。")
        if self.frontend_dir is not None:
            required_assets: tuple[str, ...] = (
                "index.html",
                "styles.css",
                "app.js",
            )
            if self.trace_store is not None:
                required_assets = (
                    *required_assets,
                    "debug.html",
                    "debug.css",
                    "debug.js",
                )
            if any(
                not (self.frontend_dir / asset).is_file()
                for asset in required_assets
            ):
                raise ValueError("前端目录缺少固定的本地资源。")
        if (self.trace_store is None) != (self.trace_recorder is None):
            raise ValueError("Trace Store 和 recorder 必须同时配置。")


def create_app(services: ApiServices) -> FastAPI:  # noqa: PLR0915
    """创建不暴露交互式文档的生产应用。

    Args:
        services: 已完成配置校验的运行依赖。

    Returns:
        FastAPI 应用。

    """
    app = FastAPI(
        title="DOCX RAG",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services

    frontend_dir = services.frontend_dir
    if frontend_dir is not None:

        @app.get("/", include_in_schema=False)
        def frontend_index() -> FileResponse:
            """返回固定的本地验收页。

            Args:
                无参数；读取固化的前端资源。

            Returns:
                本地 HTML 文件响应。

            """
            return FileResponse(frontend_dir / "index.html")

        @app.get("/assets/styles.css", include_in_schema=False)
        def frontend_styles() -> FileResponse:
            """返回固定的本地样式。

            Args:
                无参数；读取固化的前端资源。

            Returns:
                本地 CSS 文件响应。

            """
            return FileResponse(
                frontend_dir / "styles.css",
                media_type="text/css",
            )

        @app.get("/assets/app.js", include_in_schema=False)
        def frontend_script() -> FileResponse:
            """返回固定的本地脚本。

            Args:
                无参数；读取固化的前端资源。

            Returns:
                本地 JavaScript 文件响应。

            """
            return FileResponse(
                frontend_dir / "app.js",
                media_type="text/javascript",
            )

        if services.trace_store is not None:

            @app.get("/debug/", include_in_schema=False)
            def debug_index() -> FileResponse:
                """返回本地管理员 Trace 页面。

                Args:
                    无参数；读取固化的 Debug HTML。

                Returns:
                    本地 HTML 文件响应。

                """
                return FileResponse(frontend_dir / "debug.html")

            @app.get("/assets/debug.css", include_in_schema=False)
            def debug_styles() -> FileResponse:
                """返回本地 Debug 样式。

                Args:
                    无参数；读取固化的 CSS。

                Returns:
                    本地 CSS 文件响应。

                """
                return FileResponse(
                    frontend_dir / "debug.css",
                    media_type="text/css",
                )

            @app.get("/assets/debug.js", include_in_schema=False)
            def debug_script() -> FileResponse:
                """返回本地 Debug 脚本。

                Args:
                    无参数；读取固化的 JavaScript。

                Returns:
                    本地 JavaScript 文件响应。

                """
                return FileResponse(
                    frontend_dir / "debug.js",
                    media_type="text/javascript",
                )

    @app.get("/live")
    def live() -> dict[str, str]:
        """仅表示应用进程可响应。

        Args:
            无参数；检查当前应用进程。

        Returns:
            固定的存活状态。

        """
        return {"status": "live"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        """严格汇总所有必需依赖。

        Args:
            无参数；读取就绪服务缓存。

        Returns:
            HTTP 200 或 503 的依赖状态响应。

        """
        report = services.readiness.check()
        payload = {
            "ready": report.ready,
            "components": [
                asdict(component) for component in report.components
            ],
        }
        if services.run_mode is RunMode.DEMO:
            payload.update(
                run_mode=RunMode.DEMO.value,
                production_ready=False,
            )
        return JSONResponse(
            status_code=200 if report.ready else 503,
            content=payload,
        )

    @app.post("/api/chat")
    def chat(
        request: ChatRequest,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> StreamingResponse:
        """只流式发阶段状态，最后发布完整校验结果。

        Args:
            request: 已校验的会话和问题请求。
            authorization: 查询 API 的 Bearer 认证头。

        Returns:
            按 NDJSON 输出阶段和最终回答的流响应。

        """
        _require_bearer(authorization, services.query_token)
        if not services.readiness.check().ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service not ready",
            )
        trace_id = uuid.uuid4().hex
        stream = _admit_query_stream(
            services=services,
            request=request,
            trace_id=trace_id,
        )
        return StreamingResponse(
            stream,
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Trace-ID": trace_id,
            },
        )

    @app.post("/api/admin/debug/chat")
    def debug_chat(
        request: ChatRequest,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> StreamingResponse:
        """执行开始前已确认 FULL 捕获可用的管理员查询。

        Args:
            request: 与普通 chat 相同的会话和问题。
            authorization: 管理 API 的 Bearer 认证头。

        Returns:
            与普通 chat 相同的 NDJSON 阶段和最终协议。

        """
        _require_bearer(authorization, services.admin_token)
        recorder = _require_service(services.trace_recorder)
        if not services.readiness.check().ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service not ready",
            )
        try:
            recorder.require_full_capacity()
        except TraceUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="trace capture unavailable",
            ) from error
        trace_id = uuid.uuid4().hex
        stream = _admit_query_stream(
            services=services,
            request=request,
            trace_id=trace_id,
            trace_mode=TraceMode.FULL,
        )
        return StreamingResponse(
            stream,
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Trace-ID": trace_id,
            },
        )

    _register_trace_endpoints(app, services)

    @app.delete(
        "/api/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def clear_conversation(
        conversation_id: str,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> Response:
        """清空指定会话的历史用户问题。

        Args:
            conversation_id: 待清空的稳定会话标识。
            authorization: 查询 API 的 Bearer 认证头。

        Returns:
            HTTP 204 空响应。

        """
        _require_bearer(authorization, services.query_token)
        conversations = _require_service(services.conversations)
        conversations.clear(conversation_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    _register_feedback_endpoint(app, services)

    @app.post(
        "/api/index/jobs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_index_job(
        request: CreateJobRequest,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> dict[str, object]:
        """按幂等键创建全量或增量索引任务。

        Args:
            request: 已校验的索引任务请求。
            authorization: 管理 API 的 Bearer 认证头。

        Returns:
            不含敏感信息的任务状态。

        """
        _require_bearer(authorization, services.admin_token)
        jobs = _require_service(services.jobs)
        job = jobs.create_job(
            idempotency_key=request.idempotency_key,
            kind=request.kind,
            pipeline_fingerprint=services.pipeline_fingerprint,
        )
        if services.audit is not None:
            services.audit.index_job(job)
        return _job_payload(job)

    @app.get("/api/index/jobs/{job_id}")
    def get_index_job(
        job_id: str,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> dict[str, object]:
        """读取索引任务的非敏感状态。

        Args:
            job_id: 待读取的索引任务标识。
            authorization: 管理 API 的 Bearer 认证头。

        Returns:
            不含敏感信息的任务状态。

        """
        _require_bearer(authorization, services.admin_token)
        jobs = _require_service(services.jobs)
        try:
            job = jobs.get_job(job_id)
        except LookupError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="job not found",
            ) from error
        if services.audit is not None:
            services.audit.index_job(job)
        return _job_payload(job)

    return app


def _admit_query_stream(
    *,
    services: ApiServices,
    request: ChatRequest,
    trace_id: str,
    trace_mode: TraceMode = TraceMode.SAFE,
) -> Iterator[bytes]:
    """校验查询依赖并创建受容量限制的流式响应。

    Args:
        services: API 已配置的运行时服务。
        request: 已通过 schema 校验的聊天请求。
        trace_id: 本次查询的稳定追踪标识。
        trace_mode: 本次请求允许使用的 Trace 模式。

    Returns:
        按需生成 NDJSON 消息的字节迭代器。

    Raises:
        HTTPException: 查询服务不可用或执行容量已耗尽。

    """
    query = _require_service(services.query)
    query_executor = _require_service(services.query_executor)
    try:
        return stream_query(
            executor=query_executor,
            service=query,
            request=QueryStreamRequest(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                question=request.question,
                audit=services.audit,
                trace_mode=trace_mode,
            ),
        )
    except QueryAdmissionError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="query capacity unavailable",
            headers={
                "Retry-After": str(query_executor.retry_after_seconds)
            },
        ) from error


def _register_trace_endpoints(
    app: FastAPI,
    services: ApiServices,
) -> None:
    """注册只接受管理员令牌的 Trace 查询接口。

    Args:
        app: 当前 FastAPI 应用。
        services: 含独立 Trace Store 的运行依赖。

    Returns:
        无返回值。

    """

    @app.get("/api/admin/traces")
    def list_traces(  # noqa: PLR0913, PLR0917
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        trace_id: Annotated[str | None, Query()] = None,
        created_from: Annotated[datetime | None, Query()] = None,
        created_to: Annotated[datetime | None, Query()] = None,
        trace_status: Annotated[
            TraceStatus | None,
            Query(alias="status"),
        ] = None,
        refusal_code: Annotated[str | None, Query()] = None,
        error_code: Annotated[str | None, Query()] = None,
        feedback_useful: Annotated[bool | None, Query()] = None,
    ) -> JSONResponse:
        """分页读取稳定倒序的 Trace 摘要。

        Args:
            authorization: 管理 API Bearer 认证头。
            page: 从 1 开始的页码。
            page_size: 固定上限内的页大小。
            trace_id: 可选精确 Trace ID。
            created_from: 可选创建时间下界。
            created_to: 可选创建时间上界。
            trace_status: 可选 Trace 终态。
            refusal_code: 可选拒答码。
            error_code: 可选失败码。
            feedback_useful: 可选用户有用性状态。

        Returns:
            `Cache-Control: no-store` 的 Trace 列表页。

        """
        _require_bearer(authorization, services.admin_token)
        store = _require_service(services.trace_store)
        try:
            result = store.list_traces(
                TraceListFilter(
                    page=page,
                    page_size=page_size,
                    trace_id=trace_id,
                    created_from=created_from,
                    created_to=created_to,
                    status=trace_status,
                    refusal_code=refusal_code,
                    error_code=error_code,
                    feedback_useful=feedback_useful,
                )
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid trace filter",
            ) from error
        return _no_store_json(
            {
                "items": [
                    jsonable_encoder(asdict(item))
                    for item in result.items
                ],
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
            }
        )

    @app.get("/api/admin/traces/{trace_id}")
    def get_trace(
        trace_id: str,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> JSONResponse:
        """读取一个 Trace 的 span 树、漏斗和 artifact 元数据。

        Args:
            trace_id: 32 位 Trace ID。
            authorization: 管理 API Bearer 认证头。

        Returns:
            不内联大对象内容的完整 Trace 详情。

        """
        _require_bearer(authorization, services.admin_token)
        store = _require_service(services.trace_store)
        try:
            detail = store.get_trace(trace_id)
        except TraceNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="trace not found",
            ) from error
        return _no_store_json(
            {
                "trace": jsonable_encoder(asdict(detail.trace)),
                "spans": [
                    jsonable_encoder(asdict(span))
                    for span in detail.spans
                ],
                "candidate_decisions": [
                    jsonable_encoder(asdict(decision))
                    for decision in detail.candidate_decisions
                ],
                "artifacts": [
                    jsonable_encoder(asdict(artifact))
                    for artifact in detail.artifacts
                ],
            }
        )

    @app.get(
        "/api/admin/traces/{trace_id}/artifacts/{artifact_id}"
    )
    def get_trace_artifact(
        trace_id: str,
        artifact_id: str,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> Response:
        """读取已校验归属、TTL 和 SHA256 的完整 artifact。

        Args:
            trace_id: artifact 所属 Trace ID。
            artifact_id: 待读取的 artifact ID。
            authorization: 管理 API Bearer 认证头。

        Returns:
            原始完整 payload，绝不截断。

        """
        _require_bearer(authorization, services.admin_token)
        store = _require_service(services.trace_store)
        try:
            artifact = store.get_artifact(trace_id, artifact_id)
        except ArtifactExpiredError as error:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="artifact expired",
            ) from error
        except ArtifactNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="artifact not found",
            ) from error
        return Response(
            content=artifact.payload,
            media_type=artifact.metadata.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": artifact.metadata.sha256,
            },
        )

    @app.get("/api/admin/traces/{trace_id}/export")
    def export_trace(
        trace_id: str,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> Response:
        """导出且只导出当前 Trace 的 canonical JSON。

        Args:
            trace_id: 待导出的 Trace ID。
            authorization: 管理 API Bearer 认证头。

        Returns:
            禁止缓存的 canonical JSON 响应。

        """
        _require_bearer(authorization, services.admin_token)
        store = _require_service(services.trace_store)
        try:
            payload = store.export_trace(trace_id)
        except TraceNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="trace not found",
            ) from error
        return Response(
            content=payload,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


def _no_store_json(payload: object) -> JSONResponse:
    return JSONResponse(
        content=jsonable_encoder(payload),
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _register_feedback_endpoint(
    app: FastAPI,
    services: ApiServices,
) -> None:
    """注册只保存非敏感有用性信号的查询鉴权端点。"""

    @app.post(
        "/api/feedback",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def record_feedback(
        request: FeedbackRequest,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> Response:
        """幂等记录最终回答是否有用，不保存问题或答案。

        Args:
            request: 追踪标识和有用性反馈。
            authorization: 查询 API 的 Bearer 认证头。

        Returns:
            HTTP 204 空响应。

        """
        _require_bearer(authorization, services.query_token)
        feedback = _require_service(services.feedback)
        feedback.record(
            request.trace_id,
            useful=request.useful,
            now=datetime.now(UTC),
        )
        if services.trace_store is not None:
            with suppress(TraceNotFoundError):
                services.trace_store.set_feedback(
                    request.trace_id,
                    useful=request.useful,
                )
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_bearer(header: str | None, expected: str) -> None:
    """使用常量时间比较独立 Bearer token。"""
    prefix = "Bearer "
    supplied = "" if header is None else header
    if not supplied.startswith(prefix) or not hmac.compare_digest(
        supplied[len(prefix) :],
        expected,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _require_service(service: ServiceT | None) -> ServiceT:
    """拒绝未配置的可选业务端点。"""
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service unavailable",
        )
    return service


def _job_payload(job: Job) -> dict[str, object]:
    """序列化不含租约所有者的索引任务状态。"""
    return {
        "job_id": job.job_id,
        "kind": job.kind.value,
        "state": job.state.value,
        "pipeline_fingerprint": job.pipeline_fingerprint,
        "attempt": job.attempt,
        "error_code": job.error_code,
    }
