"""连接验证必须绑定实际方案使用的模型、角色和请求策略。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from rag_app.adapters.providers.aliyun_endpoint import (
    AliyunEndpointConfig,
    resolve_endpoint,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.product.models import (
    ProviderConnection,
    ProviderValidationRun,
    RetrievalProfileRevision,
)
from rag_app.product.resolved_profile import (
    ResolvedEmbeddingSpec,
    resolve_embedding,
)


def endpoint_identity(connection: ProviderConnection) -> str:
    """对适配器实际端点计算非 Secret 身份。

    Args:
        connection: 当前连接配置。

    Returns:
        规范化端点摘要。

    """
    endpoint = (
        "https://api.jina.ai"
        if connection.provider_type == "jina"
        else resolve_endpoint(
            AliyunEndpointConfig.model_validate(
                {
                    "workspace_id": connection.workspace_id,
                    "region": connection.region,
                    "endpoint_mode": connection.endpoint_mode,
                    "api_host": connection.api_host,
                }
            )
        )
    )
    return canonical_sha256(endpoint)


def profile_specs(
    profile: RetrievalProfileRevision,
    lookup: Callable[[str], ProviderConnection],
) -> tuple[ResolvedEmbeddingSpec, ...]:
    """读取持久解析对象，历史对象只走显式受支持输入。

    Args:
        profile: 不可变方案。
        lookup: 非 Secret 连接读取函数。

    Returns:
        主槽和可选备用槽的完整策略。

    """
    if profile.primary_resolved:
        specs = [
            ResolvedEmbeddingSpec.model_validate(dict(profile.primary_resolved))
        ]
        if profile.standby_resolved:
            specs.append(
                ResolvedEmbeddingSpec.model_validate(
                    dict(profile.standby_resolved)
                )
            )
        return tuple(specs)
    specs = [
        resolve_embedding(
            lookup(profile.primary_connection_id),
            profile.primary_embedding_model,
            profile.primary_dimension,
            dict(profile.primary_document_policy),
            dict(profile.primary_query_policy),
        )
    ]
    if profile.standby_connection_id is not None:
        if (
            profile.standby_embedding_model is None
            or profile.standby_dimension is None
        ):
            raise ValueError("Standby Profile 合同不完整。")
        specs.append(
            resolve_embedding(
                lookup(profile.standby_connection_id),
                profile.standby_embedding_model,
                profile.standby_dimension,
                dict(profile.standby_document_policy),
                dict(profile.standby_query_policy),
            )
        )
    return tuple(specs)


def validation_is_current(
    run: ProviderValidationRun,
    connection: ProviderConnection,
    key_version: int,
) -> bool:
    """核对连接版本、端点、凭据版本与验证有效期。

    Args:
        run: 待检查的持久验证。
        connection: 当前连接元数据。
        key_version: 当前 Credential 版本。

    Returns:
        24 小时内同一连接配置和传输模式的记录是否有效。

    """
    try:
        identity = endpoint_identity(connection)
    except ValueError:
        return False
    return (
        connection.enabled
        and run.connection_id == connection.connection_id
        and run.configuration_version == connection.configuration_version
        and run.credential_key_version == key_version
        and run.endpoint_identity == identity
        and run.validation_mode in {"live", "mock"}
        and datetime.now(UTC) - timedelta(hours=24)
        <= datetime.fromisoformat(run.finished_at)
        <= datetime.now(UTC)
    )
