"""把湾事通固定 Scope 壳层接入既有 Product API。"""

from __future__ import annotations

from fastapi import FastAPI

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.sdk import RagSdk
from rag_app.wanshitong.api import (
    register_model_configuration_lock,
    register_scope_status_routes,
)
from rag_app.wanshitong.model_store import InternalModelBindingStore
from rag_app.wanshitong.scope_service import FixedScopeService
from rag_app.wanshitong.scope_store import ScopeBindingStore
from rag_app.wanshitong.settings import WanshitongSettings


def configure_wanshitong_app(
    app: FastAPI,
    *,
    sdk: RagSdk,
    connections: SqliteConnectionFactory,
    settings: WanshitongSettings | None = None,
) -> FixedScopeService | None:
    """按显式产品模式引导 Scope 并注册湾事通状态 API。

    Args:
        app: 复用 Universal 路由与认证的 Product API。
        sdk: Product Runtime 唯一 Universal SDK。
        connections: Product Runtime 当前控制库连接工厂。
        settings: 可选显式设置；默认从进程环境读取。

    Returns:
        湾事通模式返回固定 Scope 服务，Universal 模式返回 None。

    """
    resolved = settings or WanshitongSettings.from_environment()
    if not resolved.enabled:
        return None
    service = FixedScopeService(sdk, ScopeBindingStore(connections))
    binding = service.ensure()
    app.state.wanshitong_scope_binding = binding
    app.state.wanshitong_scope_service = service
    register_model_configuration_lock(
        app, InternalModelBindingStore(connections)
    )
    register_scope_status_routes(app, service)
    return service


__all__ = ["configure_wanshitong_app"]
