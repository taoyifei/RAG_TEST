"""产品配置的一次解析、请求参数与数学身份。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, field_validator

from rag_app.adapters.providers import (
    AliyunQwen37EmbeddingConfig,
    JinaEmbeddingConfig,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)
from rag_app.core.models.search import RetrievalPolicy
from rag_app.product.catalog import CATALOG_VERSION, validate_model
from rag_app.product.models import ProviderConnection


class _JinaPolicy(FrozenModel):
    task: Literal["retrieval.passage", "retrieval.query"]
    normalized: StrictBool = True


class _QwenDocumentPolicy(FrozenModel):
    text_type: Literal["document"] = "document"
    normalized: StrictBool = True


class _QwenQueryPolicy(FrozenModel):
    text_type: Literal["query"] = "query"
    query_instruct: str = Field(min_length=1, max_length=4000)
    normalized: StrictBool = True


class ResolvedEmbeddingSpec(FrozenModel):
    """不含 Secret 的完整向量行为和连接引用。"""

    connection_id: str
    provider: str
    provider_id: str
    model: str
    dimension: int
    normalization: Literal["l2-v1"]
    adapter_revision: str
    max_input_tokens: int
    document_policy: JsonObject
    query_policy: JsonObject
    catalog_version: str

    @field_validator("document_policy", "query_policy", mode="before")
    @classmethod
    def _freeze_policy(cls, value: object) -> JsonObject:
        return freeze_json_object(value)

    def semantic_identity(self) -> dict[str, object]:
        """返回不含连接元数据的数学身份。

        Args:
            无参数；读取解析结果。

        Returns:
            可稳定序列化的实际请求合同。

        """
        return self.model_dump(mode="json", exclude={"connection_id"})

    def policy_identity(self, operation: str) -> str:
        """为验证记录生成角色绑定身份。

        Args:
            operation: document 或 query 操作。

        Returns:
            含模型、维度和实际参数的稳定摘要。

        """
        return canonical_sha256(
            {
                "provider": self.provider,
                "model": self.model,
                "dimension": self.dimension,
                "operation": operation,
                "normalization": self.normalization,
                "adapter_revision": self.adapter_revision,
                "catalog_version": self.catalog_version,
                "policy": self.document_policy
                if operation == "embedding.document"
                else self.query_policy,
            }
        )


def resolve_embedding(
    connection: ProviderConnection,
    model: str,
    dimension: int,
    document_policy: dict[str, object],
    query_policy: dict[str, object],
) -> ResolvedEmbeddingSpec:
    """一次补齐目录默认值，拒绝适配器不支持的参数。

    Args:
        connection: 非 Secret 连接引用。
        model: 目录模型。
        dimension: 输出维度。
        document_policy: 文档请求参数。
        query_policy: 查询请求参数。

    Returns:
        同时用于 HTTP、槽和指纹的解析结果。

    Raises:
        ValueError: 参数不支持或需要显式迁移历史别名。

    """
    validate_model(connection.provider_type, model, "embedding.document")
    for policy in (document_policy, query_policy):
        if set(policy) & {"instruction", "normalize", "normalization"}:
            raise ValueError(
                "历史参数需显式迁移：Qwen instruction 改为 query_instruct；"
                "归一化使用 normalized=true；Jina 不支持 instruction。"
            )
    defaults: JinaEmbeddingConfig | AliyunQwen37EmbeddingConfig
    if connection.provider_type == "jina":
        defaults = JinaEmbeddingConfig(
            slot_id="resolved",
            request_policy_identity="resolved",
        )
        document = _JinaPolicy.model_validate(
            {
                "task": defaults.document_task,
                **document_policy,
            }
        ).model_dump()
        query = _JinaPolicy.model_validate(
            {
                "task": defaults.query_task,
                **query_policy,
            }
        ).model_dump()
        if (
            document["task"] != "retrieval.passage"
            or query["task"] != "retrieval.query"
        ):
            raise ValueError("Jina task 必须与文档/查询角色一致。")
    else:
        defaults = AliyunQwen37EmbeddingConfig(
            slot_id="resolved",
            request_policy_identity="resolved",
        )
        document = _QwenDocumentPolicy.model_validate(
            document_policy
        ).model_dump()
        query = _QwenQueryPolicy.model_validate(
            {
                "query_instruct": defaults.query_instruct,
                **query_policy,
            }
        ).model_dump()
    if dimension != defaults.dimension:
        raise ValueError("当前适配器只支持目录中的固定维度。")
    if not document["normalized"] or not query["normalized"]:
        raise ValueError("当前适配器只支持 normalized=true（l2-v1）。")
    return ResolvedEmbeddingSpec(
        connection_id=connection.connection_id,
        provider=connection.provider_type,
        provider_id=defaults.provider_id,
        model=model,
        dimension=dimension,
        normalization="l2-v1",
        adapter_revision=defaults.adapter_revision,
        max_input_tokens=defaults.max_input_tokens,
        document_policy=freeze_json_object(document),
        query_policy=freeze_json_object(query),
        catalog_version=CATALOG_VERSION,
    )


def resolve_retrieval_policy(
    retrieval: dict[str, object],
    evidence: dict[str, object],
) -> dict[str, object]:
    """合并等义历史 Evidence 输入并拒绝冲突。

    Args:
        retrieval: 唯一规范策略来源。
        evidence: 兼容输入，不独立参与指纹。

    Returns:
        已补齐默认值的规范策略。

    Raises:
        ValueError: 未支持字段、冲突或自报校准状态。

    """
    merged = dict(retrieval)
    for key, value in evidence.items():
        canonical = "minimum_support_items" if key == "minimum_units" else key
        if canonical in merged and merged[canonical] != value:
            raise ValueError(f"Evidence 参数冲突：{canonical}。")
        merged[canonical] = value
    policy = RetrievalPolicy.model_validate(merged)
    if (
        policy.dense_semantic_calibration_state != "UNCALIBRATED"
        or policy.dense_calibrated_vector_spaces
        or policy.dense_semantic_enabled
    ):
        raise ValueError("校准状态只能来自已接受的持久验证记录。")
    return policy.model_dump(mode="json")
