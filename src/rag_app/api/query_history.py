"""经过产品会话及资源范围鉴权的本地历史接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Query, Request, Response

from rag_app.composition.product_runtime import ProductRuntime


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
        return runtime.history.detail(trace_id)

    @app.delete("/api/v1/history", tags=["history"], status_code=204)
    def _clear() -> Response:
        runtime.history.clear()
        return Response(status_code=204)

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
        return runtime.history.detail(
            trace_id,
            project_id=project_id,
            knowledge_base_id=kb_id,
            owner_id=getattr(request.state, "access_token_id", None),
        )
