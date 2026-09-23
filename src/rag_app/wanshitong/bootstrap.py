"""把湾事通固定 Scope 壳层接入既有 Product API。"""

from __future__ import annotations

from fastapi import FastAPI

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.product.crypto import load_master_key
from rag_app.wanshitong.admin_api import register_admin_routes
from rag_app.wanshitong.api import register_scope_status_routes
from rag_app.wanshitong.department_profiles import (
    DepartmentProfileSourceReader,
    DepartmentProfileStore,
)
from rag_app.wanshitong.department_shadow import (
    WanshitongDepartmentShadowObserver,
)
from rag_app.wanshitong.document_metadata import (
    WanshitongDocumentMetadataStore,
)
from rag_app.wanshitong.errors import ScopeBindingError
from rag_app.wanshitong.public_api import register_public_routes
from rag_app.wanshitong.public_session import (
    PublicSessionProvider,
    PublicSessionService,
)
from rag_app.wanshitong.question_analytics import QuestionAnalyticsService
from rag_app.wanshitong.question_recommendations import (
    QuestionRecommendationService,
)
from rag_app.wanshitong.scope_service import FixedScopeService
from rag_app.wanshitong.scope_store import ScopeBindingStore
from rag_app.wanshitong.settings import WanshitongSettings
from rag_app.wanshitong.sso_api import register_sso_routes
from rag_app.wanshitong.sso_client import SsoClient, load_client_secret
from rag_app.wanshitong.sso_session import SsoSessionService


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
    app.state.wanshitong_department_shadow_enabled = (
        resolved.department_shadow_enabled
    )
    if not resolved.enabled:
        return None
    master_key_file = runtime.settings.master_key_file
    if master_key_file is None:
        raise ValueError("湾事通公共会话必须配置 RAG_MASTER_KEY_FILE。")
    master_key = load_master_key(master_key_file)
    public_sessions: PublicSessionProvider
    if resolved.sso.enabled:
        if (
            resolved.sso.deployment_id is None
            or resolved.sso.validate_url is None
            or resolved.sso.client_id is None
            or resolved.sso.client_secret_file is None
        ):
            raise AssertionError("SSO 设置未完成 fail-fast 校验。")
        unknown_origins = {
            entry.origin for entry in resolved.sso.entries
        }.difference(runtime.settings.trusted_origins)
        if unknown_origins:
            raise ValueError("SSO 入口必须同时列入 RAG_TRUSTED_ORIGINS。")
        public_sessions = SsoSessionService(
            master_key,
            deployment_id=resolved.sso.deployment_id,
            base_path=resolved.sso.base_path,
        )
        sso_client = SsoClient(
            validate_url=resolved.sso.validate_url,
            client_id=resolved.sso.client_id,
            client_secret=load_client_secret(resolved.sso.client_secret_file),
        )
        register_sso_routes(
            app,
            runtime=runtime,
            settings=resolved.sso,
            sessions=public_sessions,
            client=sso_client,
        )
    else:
        public_sessions = PublicSessionService(master_key)
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
    question_analytics = QuestionAnalyticsService(
        runtime.connections,
        runtime.history,
        deployment_id=public_sessions.deployment_id or "LEGACY_UNKNOWN",
        retention_days=runtime.settings.history_retention_days,
    )
    app.state.wanshitong_question_analytics = question_analytics
    recommendations = QuestionRecommendationService(
        runtime.connections,
        deployment_id=public_sessions.deployment_id or "LEGACY_UNKNOWN",
        public_enabled=resolved.popular_questions_enabled,
    )
    app.state.wanshitong_question_recommendations = recommendations
    document_metadata = WanshitongDocumentMetadataStore(runtime.connections)
    document_metadata.synchronize_legacy_rows()
    app.state.wanshitong_document_metadata = document_metadata
    department_shadow = (
        WanshitongDepartmentShadowObserver(
            DepartmentProfileSourceReader(runtime.connections),
            DepartmentProfileStore(
                runtime.settings.data_dir / "department-profiles"
            ),
        )
        if resolved.department_shadow_enabled
        else None
    )
    runtime.profiles.configure_department_shadow(department_shadow)
    app.state.wanshitong_department_shadow = department_shadow
    register_scope_status_routes(app, service)
    register_admin_routes(
        app,
        runtime=runtime,
        scope_service=service,
        document_metadata=document_metadata,
        question_analytics=question_analytics,
        question_recommendations=recommendations,
    )
    register_public_routes(
        app,
        runtime=runtime,
        scope_service=service,
        sessions=public_sessions,
        recommendations=recommendations,
    )
    return service


__all__ = ["configure_wanshitong_app"]
