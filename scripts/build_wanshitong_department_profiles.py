"""离线构建与活动 Index Revision 精确绑定的湾事通部门 Profile。"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleEmbeddingConfig,
)
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.models import EmbeddingRequest, EmbeddingRequestRole
from rag_app.product.models import ProviderConnection
from rag_app.product.resolved_profile import (
    ResolvedEmbeddingSpec,
    resolve_embedding,
)
from rag_app.wanshitong.department_profiles import (
    DepartmentEmbeddingIdentity,
    DepartmentProfileSet,
    DepartmentProfileSourceReader,
    DepartmentProfileSourceSnapshot,
    DepartmentProfileStore,
    build_department_profiles,
    department_profile_embedding_text,
)
from rag_app.wanshitong.internal_model_settings import (
    InternalCredentialSettings,
    InternalModelSettings,
)

_DOCUMENT_POLICY = {
    "encoding_format": "float",
    "normalized": True,
    "role": "document",
}
_QUERY_POLICY = {
    "encoding_format": "float",
    "normalized": True,
    "role": "query",
}


def _credential(settings: InternalCredentialSettings) -> str:
    """按既有内网模型配置解析凭据，不把值写入输出。"""
    if settings.source == "none":
        return ""
    if settings.source == "environment":
        value = os.environ.get(settings.environment_name or "", "")
        if not value:
            raise ValueError("Embedding Credential 环境变量不存在。")
        return value
    return settings.database_secret()


def _resolved_embedding(
    settings: InternalModelSettings,
) -> ResolvedEmbeddingSpec:
    """复用产品解析器取得与主检索相同的向量数学身份。"""
    connection = ProviderConnection(
        connection_id="department-profile-builder",
        display_name="湾事通部门 Profile 离线构建",
        provider_type="openai-compatible",
        credential_id="department-profile-builder",
        endpoint_profile="default",
        enabled=True,
        status="ready",
        endpoint_mode="custom",
        api_base_url=settings.embedding_base_url,
        created_at="offline",
        updated_at="offline",
    )
    return resolve_embedding(
        connection,
        settings.embedding_model,
        settings.embedding_dimension,
        _DOCUMENT_POLICY,
        _QUERY_POLICY,
    )


def _build_vectors(
    snapshot: DepartmentProfileSourceSnapshot,
) -> tuple[DepartmentProfileSet, int]:
    """以一次逻辑批处理调用既有 Embedding adapter 构建部门向量。"""
    lexical_profiles = build_department_profiles(snapshot)
    if not lexical_profiles.profiles:
        return lexical_profiles, 0
    settings = InternalModelSettings.from_environment()
    resolved = _resolved_embedding(settings)
    client = ProviderHttpClient(
        settings.embedding_base_url,
        client=httpx.Client(
            timeout=httpx.Timeout(
                connect=5.0,
                read=30.0,
                write=30.0,
                pool=5.0,
            ),
            follow_redirects=False,
            trust_env=False,
        ),
        max_attempts=3,
        allow_http=urlsplit(settings.embedding_base_url).scheme == "http",
        use_budget_transport=False,
        defer_success_observation=True,
    )
    adapter = OpenAICompatibleEmbeddingAdapter(
        OpenAICompatibleEmbeddingConfig(
            slot_id="primary",
            model=resolved.model,
            dimension=resolved.dimension,
            request_policy_identity=resolved.policy_identity(
                "embedding.document"
            ),
            document_request_policy_identity=resolved.policy_identity(
                "embedding.document"
            ),
            query_request_policy_identity=resolved.policy_identity(
                "embedding.query"
            ),
            document_egress_allowed=True,
            query_egress_allowed=False,
            max_input_tokens=resolved.max_input_tokens,
            adapter_revision=resolved.adapter_revision,
            normalization=resolved.normalization,
        ),
        http_client=client,
        api_key_resolver=lambda: _credential(settings.embedding_credential),
    )
    try:
        result = adapter.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.DOCUMENT,
                texts=tuple(
                    department_profile_embedding_text(profile)
                    for profile in lexical_profiles.profiles
                ),
            )
        )
    finally:
        adapter.close()
    if result.observed_dimension != resolved.dimension:
        raise ValueError("部门 Profile 向量维度与产品解析身份不一致。")
    identity = DepartmentEmbeddingIdentity(
        slot_id="primary",
        provider_id=resolved.provider_id,
        model=resolved.model,
        vector_name="dense_primary",
        dimension=resolved.dimension,
        normalization=resolved.normalization,
        adapter_revision=resolved.adapter_revision,
    )
    vectors = {
        profile.department_key: vector
        for profile, vector in zip(
            lexical_profiles.profiles, result.vectors, strict=True
        )
    }
    return (
        build_department_profiles(
            snapshot,
            embedding_identity=identity,
            vectors=vectors,
        ),
        len(result.calls),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """从候选数据副本构建 lexical-only 或既有模型向量 Profile。

    Args:
        argv: 可选命令行参数；默认读取进程参数。

    Returns:
        成功为 0；参数或数据错误由 argparse/调用栈返回非零。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--embedding-mode",
        choices=("lexical-only", "existing-provider"),
        default="lexical-only",
    )
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve(strict=True)
    if data_dir.is_symlink():
        raise ValueError("部门 Profile 数据目录禁止 symlink。")
    database = data_dir / "universal-rag.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise ValueError("部门 Profile 数据库不存在或不是普通文件。")
    output_root = (
        args.output_root.resolve(strict=False)
        if args.output_root is not None
        else data_dir / "department-profiles"
    )
    snapshot = DepartmentProfileSourceReader(
        SqliteConnectionFactory(database)
    ).snapshot(args.project_id, args.knowledge_base_id)
    if args.embedding_mode == "existing-provider":
        profiles, provider_call_count = _build_vectors(snapshot)
    else:
        profiles = build_department_profiles(snapshot)
        provider_call_count = 0
    output = DepartmentProfileStore(output_root).save(profiles)
    print(
        json.dumps(
            {
                "status": "BUILT",
                "scope_id": profiles.scope_id,
                "index_revision_id": profiles.index_revision_id,
                "metadata_revision": profiles.metadata_revision,
                "profile_revision": profiles.profile_revision,
                "department_count": len(profiles.profiles),
                "document_count": sum(
                    profile.document_count for profile in profiles.profiles
                ),
                "embedding_mode": (
                    "EXISTING_PROVIDER"
                    if profiles.embedding_identity is not None
                    else "LEXICAL_ONLY"
                ),
                "provider_call_count": provider_call_count,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
