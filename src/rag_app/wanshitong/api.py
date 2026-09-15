"""湾事通固定 Scope 的管理员只读状态 API。"""

from __future__ import annotations

from fastapi import FastAPI, Request

from rag_app.core.errors import PolicyDenied
from rag_app.wanshitong.errors import ScopeBindingError
from rag_app.wanshitong.models import ScopeStatus
from rag_app.wanshitong.scope_service import FixedScopeService

SCOPE_STATUS_PATH = "/api/v1/admin/wanshitong/scope"
_ADMIN_PRINCIPALS = frozenset({"admin_session", "legacy_admin"})


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
        try:
            return service.status()
        except ScopeBindingError as error:
            return ScopeStatus(
                ready=False,
                blocker_code=error.code,
                blocker_message=error.safe_message,
            )


__all__ = ["SCOPE_STATUS_PATH", "register_scope_status_routes"]
