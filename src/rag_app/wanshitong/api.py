"""湾事通固定 Scope 的管理员只读状态 API。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from rag_app.core.errors import PolicyDenied
from rag_app.wanshitong.model_store import InternalModelBindingStore
from rag_app.wanshitong.models import ScopeStatus
from rag_app.wanshitong.scope_service import FixedScopeService

SCOPE_STATUS_PATH = "/api/v1/admin/wanshitong/scope"
_ADMIN_PRINCIPALS = frozenset({"admin_session", "legacy_admin"})
_LOCKED_EXACT_WRITES = frozenset(
    {
        ("POST", "/api/v1/provider-credentials"),
        ("POST", "/api/v1/provider-connections"),
    }
)


def register_scope_status_routes(
    app: FastAPI, service: FixedScopeService
) -> None:
    """注册依赖既有 Product 管理员鉴权的只读端点。

    Args:
        app: 已安装 Product 认证中间件的 FastAPI 应用。
        service: 已完成启动 ensure 的固定 Scope 服务。

    """

    @app.get(
        SCOPE_STATUS_PATH,
        tags=["wanshitong"],
        response_model=ScopeStatus,
    )
    def _scope_status(request: Request) -> ScopeStatus:
        """返回固定 Scope 状态，拒绝非管理员主体。"""
        if (
            getattr(request.state, "product_principal", None)
            not in _ADMIN_PRINCIPALS
        ):
            raise PolicyDenied(
                "湾事通固定 Scope 状态仅允许管理员读取。",
                stage="wanshitong.scope.status",
            )
        return service.status()


def register_model_configuration_lock(
    app: FastAPI, store: InternalModelBindingStore
) -> None:
    """配置完成后把湾事通模型管理写 API 收敛为只读。

    Args:
        app: 已注册 Universal Product 路由的 FastAPI 应用。
        store: 复用同一控制库的内网模型绑定 Store。

    """

    @app.middleware("http")
    async def _model_configuration_lock(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if store.configuration_locked() and _is_model_configuration_write(
            request.method, request.url.path
        ):
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "MODEL_CONFIGURATION_LOCKED",
                        "message": "湾事通模型配置已锁定为只读。",
                        "stage": "wanshitong.models.lock",
                        "retryable": False,
                        "trace_id": "",
                        "details": {},
                    }
                },
            )
        return await call_next(request)


def _is_model_configuration_write(method: str, path: str) -> bool:
    if (method, path) in _LOCKED_EXACT_WRITES:
        return True
    if path.startswith("/api/v1/provider-credentials/"):
        return method == "POST" and path.endswith(":rotate")
    if path.startswith("/api/v1/provider-connections/"):
        return method == "PATCH" or (
            method == "POST" and path.endswith(":validate")
        )
    if method == "POST" and path.startswith("/api/v1/knowledge-bases/"):
        return path.endswith("/retrieval-profiles")
    if method == "POST" and path.startswith("/api/v1/retrieval-profiles/"):
        return path.endswith(":activate")
    return method == "PUT" and path.endswith("/model-settings")


__all__ = [
    "SCOPE_STATUS_PATH",
    "register_model_configuration_lock",
    "register_scope_status_routes",
]
