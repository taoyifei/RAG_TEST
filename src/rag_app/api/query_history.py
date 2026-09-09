"""经过产品会话及资源范围鉴权的本地历史接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from starlette.background import BackgroundTask

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import PolicyDenied
from rag_app.core.identifiers import new_id
from rag_app.product.history_trace_export import (
    MAX_HISTORY_TRACE_EXPORT_COUNT,
    HistoryTraceArchive,
    HistoryTraceExportLimitError,
    HistoryTraceExportService,
)

_TraceId = Annotated[
    str,
    StringConstraints(pattern=r"^(?:trace_)?[0-9a-f]{32}$"),
]


class HistoryTraceExportRequest(BaseModel):
    """管理员批量支持包请求。"""

    model_config = ConfigDict(extra="forbid")
    trace_ids: list[_TraceId] = Field(
        min_length=1,
        max_length=MAX_HISTORY_TRACE_EXPORT_COUNT,
    )
    include_history_body: bool = False

    @field_validator("trace_ids")
    @classmethod
    def _reject_duplicates(cls, values: list[str]) -> list[str]:
        """去重前校验数量后拒绝重复 ID，避免静默改变请求。"""
        if len(values) != len(set(values)):
            raise ValueError("批量支持包不能包含重复 Trace ID。")
        normalized = [
            value if value.startswith("trace_") else f"trace_{value}"
            for value in values
        ]
        if len(normalized) != len(set(normalized)):
            raise ValueError("批量支持包不能包含等价的新旧重复 Trace ID。")
        return values


def register_query_history_routes(
    app: FastAPI, runtime: ProductRuntime
) -> None:
    """复用现有管理员权限；有范围 Token 只能读取自己的历史。

    Args:
        app: 已启用产品权限中间件的应用。
        runtime: 当前产品运行时。

    Returns:
        注册完成时无返回值。

    """
    export_service = HistoryTraceExportService(
        runtime.history,
        runtime.traces,
        source_revision=runtime.traces.release_revision,
    )

    @app.exception_handler(HistoryTraceExportLimitError)
    async def _history_trace_limit_handler(
        request: Request,
        error: HistoryTraceExportLimitError,
    ) -> JSONResponse:
        del request
        return _export_limit_response(error.reason_code)

    @app.get("/api/v1/history", tags=["history"])
    def _list(  # noqa: PLR0913, PLR0917
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        status: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        keyword: Annotated[str | None, Query(max_length=200)] = None,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        return runtime.history.list_history(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            status=status,
            created_from=created_from,
            created_to=created_to,
            keyword=keyword,
            page_size=page_size,
            offset=offset,
        )

    @app.get("/api/v1/history/{trace_id}", tags=["history"])
    def _detail(trace_id: str) -> dict[str, object]:
        runtime.traces.flush()
        return runtime.history.detail(trace_id)

    @app.delete("/api/v1/history", tags=["history"], status_code=204)
    def _clear() -> Response:
        runtime.history.clear()
        return Response(status_code=204)

    export_base = "/api/v1/admin/history-traces"

    @app.get(export_base + "/{trace_id}/export", tags=["history"])
    def _export_history_trace(
        trace_id: _TraceId,
        request: Request,
        include_history_body: bool = False,
    ) -> StreamingResponse:
        _require_export_admin(request)
        archive = export_service.export(
            (trace_id,),
            include_history_body=include_history_body,
            body_authorized=True,
        )
        return _archive_response(archive)

    @app.post(export_base + ":export", tags=["history"])
    def _export_history_traces(
        body: HistoryTraceExportRequest,
        request: Request,
    ) -> StreamingResponse:
        _require_export_admin(request)
        archive = export_service.export(
            body.trace_ids,
            include_history_body=body.include_history_body,
            body_authorized=True,
        )
        return _archive_response(archive)

    scope_path = "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"

    @app.get(scope_path + "/history", tags=["history"])
    def _scoped_list(
        project_id: str,
        kb_id: str,
        request: Request,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        runtime.sdk.get_knowledge_base(project_id, kb_id)
        return runtime.history.list_history(
            project_id=project_id,
            knowledge_base_id=kb_id,
            owner_id=getattr(request.state, "access_token_id", None),
            page_size=page_size,
            offset=offset,
        )

    @app.get(scope_path + "/history/{trace_id}", tags=["history"])
    def _scoped_detail(
        project_id: str, kb_id: str, trace_id: str, request: Request
    ) -> dict[str, object]:
        runtime.sdk.get_knowledge_base(project_id, kb_id)
        runtime.traces.flush()
        return runtime.history.detail(
            trace_id,
            project_id=project_id,
            knowledge_base_id=kb_id,
            owner_id=getattr(request.state, "access_token_id", None),
        )


def _require_export_admin(request: Request) -> None:
    """双存储支持包只允许本机管理员，外部 Token 不在能力表中。"""
    if getattr(request.state, "product_principal", None) not in {
        "admin_session",
        "legacy_admin",
    }:
        raise PolicyDenied(
            "History + Trace 支持包只允许管理员会话。",
            stage="history_trace.authorization",
        )


def _archive_response(archive: HistoryTraceArchive) -> StreamingResponse:
    """把已完成校验的临时文件绑定到分块下载响应。"""
    return StreamingResponse(
        archive.iter_bytes(),
        media_type="application/zip",
        background=BackgroundTask(archive.close),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{archive.filename}"'
            ),
            "Content-Length": str(archive.size_bytes),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Archive-SHA256": archive.sha256,
        },
    )


def _export_limit_response(reason_code: str) -> JSONResponse:
    """返回不依赖通用 RagError 映射表的稳定 413。"""
    trace_id = new_id("trace")
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": reason_code,
                "message": "History + Trace 支持包超过容量上限。",
                "stage": "history_trace.export",
                "retryable": False,
                "trace_id": trace_id,
                "details": {},
            }
        },
        headers={"Cache-Control": "no-store", "X-Trace-Id": trace_id},
    )
