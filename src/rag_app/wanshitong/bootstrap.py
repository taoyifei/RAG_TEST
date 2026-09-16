"""把湾事通固定 Scope 壳层接入既有 Product API。"""

from __future__ import annotations

from fastapi import FastAPI

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.product.crypto import load_master_key
from rag_app.wanshitong.admin_api import register_admin_routes
from rag_app.wanshitong.api import register_scope_status_routes
from rag_app.wanshitong.document_metadata import (
    WanshitongDocumentMetadataStore,
)
from rag_app.wanshitong.errors import ScopeBindingError
from rag_app.wanshitong.public_api import register_public_routes
from rag_app.wanshitong.public_session import PublicSessionService
from rag_app.wanshitong.scope_service import FixedScopeService
from rag_app.wanshitong.scope_store import ScopeBindingStore
from rag_app.wanshitong.settings import WanshitongSettings


def configure_wanshitong_app(
    app: FastAPI,
    *,
    runtime: ProductRuntime,
    settings: WanshitongSettings | None = None,
) -> FixedScopeService | None:
    """按显式产品模式引导 Scope 并注册湾事通状态 API。

    Args:
        app: 复用 Universal 路由与认证的 Product API。
        runtime: 唯一 Universal Product Runtime。
        settings: 可选显式设置；默认从进程环境读取。

    Returns:
        湾事通模式返回固定 Scope 服务，Universal 模式返回 None。

    """
    resolved = settings or WanshitongSettings.from_environment()
    app.state.wanshitong_enabled = resolved.enabled
    app.state.wanshitong_demo_allow_http = resolved.demo_allow_http
    if not resolved.enabled:
        return None
    master_key_file = runtime.settings.master_key_file
    if master_key_file is None:
        raise ValueError("湾事通公共会话必须配置 RAG_MASTER_KEY_FILE。")
    public_sessions = PublicSessionService(load_master_key(master_key_file))
    service = FixedScopeService(
        runtime.sdk, ScopeBindingStore(runtime.connections)
    )
    try:
        binding = service.ensure()
        scope_error: ScopeBindingError | None = None
    except ScopeBindingError as error:
        # 首次无绑定仍由 ensure 创建；已有绑定损坏时保留管理员诊断入口。
        binding = None
        scope_error = error
    app.state.wanshitong_scope_binding = binding
    app.state.wanshitong_scope_error = scope_error
    app.state.wanshitong_scope_service = service
    app.state.wanshitong_public_session_service = public_sessions
    document_metadata = WanshitongDocumentMetadataStore(runtime.connections)
    document_metadata.synchronize_legacy_rows()
    app.state.wanshitong_document_metadata = document_metadata
    register_scope_status_routes(app, service)
    register_admin_routes(
        app,
        runtime=runtime,
        scope_service=service,
        document_metadata=document_metadata,
    )
    register_public_routes(
        app,
        runtime=runtime,
        scope_service=service,
        sessions=public_sessions,
    )
    return service


__all__ = ["configure_wanshitong_app"]
