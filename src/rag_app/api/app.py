"""FastAPI 应用工厂、鉴权查询与索引管理端点。"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import FastAPI, Header, HTTPException, Response, status
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
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.jobs import JobStore
from rag_app.state.models import Job

__all__ = ["ApiServices", "create_app"]

ServiceT = TypeVar("ServiceT")


@dataclass(frozen=True, slots=True)
class ApiServices:
    """API 的运行依赖与独立令牌。"""

    readiness: ReadinessService
    query_token: str
    admin_token: str
    query: QueryService | None = None
    query_executor: QueryExecutor | None = None
    conversations: ConversationStore | None = None
    jobs: JobStore | None = None
    feedback: FeedbackStore | None = None
    pipeline_fingerprint: str = ""
    frontend_dir: Path | None = None
    audit: StructuredAuditLogger | None = None

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
            required_assets = ("index.html", "styles.css", "app.js")
            if any(
                not (self.frontend_dir / asset).is_file()
                for asset in required_assets
            ):
                raise ValueError("前端目录缺少固定的本地资源。")


def create_app(services: ApiServices) -> FastAPI:
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
) -> Iterator[bytes]:
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
