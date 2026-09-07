"""预算 CLI 的只读方案解析；不构造 Runtime 或访问凭据。"""

from __future__ import annotations

from sqlite3 import Row
from typing import Any, cast

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import ChunkingPolicy, RetrievalPolicy
from rag_app.core.policies import ParsingPolicy
from rag_app.product.connection_diagnostics import _CONNECTION_QUERY
from rag_app.product.control_store import _profile
from rag_app.product.models import ProviderConnection
from rag_app.product.resolved_profile import (
    resolve_embedding,
    resolve_retrieval_policy,
)
from rag_app.product.verification import profile_specs

_P11_DIMENSION = 1024
# 只投影连接非秘密列，不 SELECT Credential，事务保证方案与连接同一快照。
METADATA_PROGRAM = """
import json, os, sqlite3, sys
from pathlib import Path
p = json.load(sys.stdin)
path = Path(p['data_dir']).resolve() / 'universal-rag.sqlite3'
wal = Path(str(path) + '-wal')
shm = Path(str(path) + '-shm')
def identity():
    return [
        None if not item.exists() else (
            item.stat().st_ino, item.stat().st_size, item.stat().st_mtime_ns
        )
        for item in (path, wal, shm)
    ]
before = identity()
has_wal = wal.exists() and wal.stat().st_size > 0
if has_wal and (not shm.exists() or os.access(path.parent, os.W_OK)):
    sys.exit('BUDGET_READONLY_WAL: use the existing read-only container volume')
uri = path.as_uri() + ('?mode=ro' if has_wal else '?mode=ro&immutable=1')
db = sqlite3.connect(uri, uri=True)
db.row_factory = sqlite3.Row
try:
    db.execute('BEGIN')
    row = db.execute(
        'SELECT * FROM retrieval_profile_revisions WHERE profile_revision_id=?',
        (p['source_profile_revision_id'],),
    ).fetchone()
    connections = []
    if row is not None:
        for key in ('primary', 'standby', 'reranker'):
            item = db.execute(
                p['connection_query'], (row[key + '_connection_id'],)
            ).fetchone()
            if item is not None:
                connections.append(dict(item))
    result = {
        'profile': None if row is None else dict(row),
        'connections': connections,
    }
finally:
    db.close()
if before != identity():
    sys.exit('BUDGET_METADATA_CHANGED: retry the read-only plan')
print(json.dumps(result))
"""


def metadata_request(config: dict[str, Any]) -> dict[str, Any]:
    """仅向只读进程传入必要定位字段和固定查询。

    Args:
        config: 现有非秘密验收配置。

    Returns:
        不包含授权或凭据材料的输入。

    """
    if not config.get("data_dir"):
        raise ValueError("BUDGET_DATA_DIR_REQUIRED: 指定原产品数据目录。")
    return {
        "data_dir": config["data_dir"],
        "source_profile_revision_id": config["source_profile_revision_id"],
        "connection_query": _CONNECTION_QUERY,
    }


def resolve_budget_profile(
    metadata: dict[str, Any], config: dict[str, Any]
) -> tuple[str, RetrievalPolicy, dict[str, Any]]:
    """复用权威解析器，允许未激活 Draft，拒绝不匹配的两路方案。

    Args:
        metadata: 同一只读事务的方案与连接投影。
        config: 原验收配置中的方案和连接引用。

    Returns:
        实际 instruct、检索策略及可审阅身份。

    Raises:
        ValueError: 方案不存在、模型或拓扑不符、策略非法。

    """
    if metadata["profile"] is None:
        raise ValueError("SOURCE_PROFILE_NOT_FOUND: 指定方案不存在。")
    try:
        profile = _profile(cast(Row, metadata["profile"]))
        connections = {
            row["connection_id"]: ProviderConnection.model_validate(
                {key: value for key, value in row.items() if value is not None}
            )
            for row in metadata["connections"]
        }
        expected = (
            (
                "jina_connection_id",
                "primary",
                "jina",
                "jina-embeddings-v5-text-small",
            ),
            (
                "aliyun_connection_id",
                "standby",
                "aliyun-model-studio",
                "qwen3.7-text-embedding",
            ),
        )
        specs = profile_specs(profile, connections.__getitem__)
        if len(specs) != len(expected) or not profile.failover_enabled:
            raise ValueError("P11_TOPOLOGY_MISMATCH")
        for spec, (key, role, provider, model) in zip(
            specs, expected, strict=True
        ):
            connection = connections[spec.connection_id]
            if (
                spec.connection_id != config.get(key)
                or spec.connection_id
                != getattr(profile, role + "_connection_id")
                or spec.provider != provider
                or connection.provider_type != provider
                or spec.model != model
                or spec.model != getattr(profile, role + "_embedding_model")
                or spec.dimension != _P11_DIMENSION
                or spec.dimension != getattr(profile, role + "_dimension")
            ):
                raise ValueError("P11_MODEL_OR_CONNECTION_MISMATCH")
            resolved = resolve_embedding(
                connection,
                spec.model,
                spec.dimension,
                dict(getattr(profile, role + "_document_policy")),
                dict(getattr(profile, role + "_query_policy")),
            )
            if resolved != spec:
                raise ValueError("P11_RESOLVED_POLICY_MISMATCH")
        if (
            profile.reranker_connection_id != config.get("jina_connection_id")
            or profile.reranker_model != "jina-reranker-v3.5"
        ):
            raise ValueError("P11_RERANKER_MISMATCH")
        policy = RetrievalPolicy.model_validate(
            resolve_retrieval_policy(
                dict(profile.retrieval_policy), dict(profile.evidence_policy)
            )
        )
    except (KeyError, TypeError, ValueError) as error:
        # Pydantic 原始异常可能包含用户输入；只发布稳定错误类型。
        raise ValueError(
            "BUDGET_PROFILE_INVALID: 模型、连接、拓扑或策略不符。"
        ) from error
    summary = {
        "source_profile_revision_id": profile.profile_revision_id,
        "profile_status": profile.status,
        "index_semantic_fingerprint": profile.index_semantic_fingerprint,
        "serving_fingerprint": profile.serving_fingerprint,
        "embedding_roles": {
            role: spec.semantic_identity()
            for role, spec in zip(("primary", "standby"), specs, strict=True)
        },
        "reranker": {"provider": "jina", "model": profile.reranker_model},
        "retrieval_policy": policy.model_dump(mode="json"),
        "parsing_chunking_contract": {
            "mode": "fixed_product_v1",
            "parser": "word-document-v1",
            "parsing_policy": ParsingPolicy().model_dump(mode="json"),
            "chunker": "docx-structural-v3",
            "chunking_policy": ChunkingPolicy().model_dump(mode="json"),
        },
    }
    identity = canonical_sha256(
        {
            key: value
            for key, value in summary.items()
            if key not in {"source_profile_revision_id", "profile_status"}
        }
    )
    return (
        str(dict(specs[1].query_policy)["query_instruct"]),
        policy,
        {
            "actual_profile_bound": True,
            "source_profile_revision_id": profile.profile_revision_id,
            "resolved_profile": summary,
            "budget_policy_identity": identity,
            "profile_revalidation": (
                "审批前与执行前重跑同一命令并核对身份；变化即失效，历史不清零。"
            ),
        },
    )
