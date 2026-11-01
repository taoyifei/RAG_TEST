"""员工工作助手的受限 HTTP 入口，复用既有 Cookie 与资料权限。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask

from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.models import (
    SEARCH_SCOPE_ORDER,
    SearchScope,
    normalize_search_scopes,
)
from wanshitong_gateway.weknora.client import NativeHttpError
from wanshitong_gateway.workbench import WorkbenchService

_MAX_REQUEST_BYTES = 128 * 1024


def _default_scopes() -> list[SearchScope]:
    return ["public"]


class _BootstrapRequest(BaseModel):
    """只接收逻辑资料范围，原生身份及资料ID由服务端决定。"""

    model_config = ConfigDict(extra="forbid")

    search_scopes: list[SearchScope] = Field(
        default_factory=_default_scopes,
        min_length=1,
        max_length=len(SEARCH_SCOPE_ORDER),
    )

    @field_validator("search_scopes")
    @classmethod
    def _normalize_search_scopes(
        cls, value: list[SearchScope]
    ) -> list[SearchScope]:
        return list(normalize_search_scopes(value))


def _require_service(service: WorkbenchService | None) -> WorkbenchService:
    if service is None:
        raise HTTPException(503, "工作助手尚未启用，请先使用知识问答。")
    return service


def register_workbench_routes(
    app: FastAPI, auth: GatewayAuth, service: WorkbenchService | None
) -> None:
    """仅提供本人工作空间的定向原生能力，不接受客户端原生认证。"""

    @app.post("/api/public/workbench/bootstrap")
    async def bootstrap(
        body: _BootstrapRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        profile = auth.require_profile(request, csrf=True)
        target = _require_service(service)
        if request.query_params:
            raise HTTPException(422, "工作助手不接受额外查询参数。")
        response.headers["Cache-Control"] = "no-store"
        try:
            return await target.bootstrap(profile, tuple(body.search_scopes))
        except NativeHttpError as error:
            raise HTTPException(
                502, "工作助手初始化失败，请稍后重试。"
            ) from error

    @app.api_route(
        "/api/workbench/{workspace_id}/native/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def native_request(
        workspace_id: UUID, path: str, request: Request
    ) -> Response:
        profile = auth.require_profile(request)
        target = _require_service(service)
        content = await request.body()
        if len(content) > _MAX_REQUEST_BYTES:
            raise HTTPException(413, "工作助手请求过长，请缩短内容。")
        try:
            upstream = await target.proxy(
                profile=profile,
                workspace_id=str(workspace_id),
                method=request.method,
                path=path,
                query=request.url.query,
                content=content,
                browser_headers=request.headers,
            )
        except NativeHttpError as error:
            raise HTTPException(502, "工作助手原生服务暂时不可用。") from error
        headers = {**upstream.headers, "Cache-Control": "no-store"}
        if upstream.stream is not None:
            headers["X-Accel-Buffering"] = "no"
            return StreamingResponse(
                upstream.stream,
                status_code=upstream.status_code,
                headers=headers,
                background=BackgroundTask(upstream.stream.aclose),
            )
        return Response(
            upstream.content, status_code=upstream.status_code, headers=headers
        )
