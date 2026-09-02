"""索引与服务指纹的规范化输入和计算函数。"""

from __future__ import annotations

from pydantic import Field, StrictInt, field_validator

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.identifiers import canonical_json, canonical_sha256
from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)
from rag_app.core.models.provider import EmbeddingSlotIdentity
from rag_app.core.policies import CircuitBreakerPolicy


class IndexFingerprintInput(FrozenModel):
    """只覆盖索引语义与兼容性的冻结输入。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    parser: ComponentDescriptor
    parsing_policy: JsonObject
    ir_schema_version: str = Field(min_length=1)
    enricher_versions: tuple[str, ...] = ()
    chunker: ComponentDescriptor
    chunker_parameters: JsonObject
    token_counter_identity: str = Field(min_length=1)
    token_count_exact: bool
    embedding_slots: tuple[EmbeddingSlotIdentity, ...] = Field(min_length=1)
    lexical_schema: JsonObject
    vector_schema: JsonObject
    chunk_payload_schema: JsonObject
    absolute_path: str | None = Field(default=None, exclude=True, repr=False)

    @field_validator(
        "parsing_policy",
        "chunker_parameters",
        "lexical_schema",
        "vector_schema",
        "chunk_payload_schema",
        mode="before",
    )
    @classmethod
    def _freeze_objects(cls, value: object) -> JsonObject:
        return freeze_json_object(value)


class ServingFingerprintInput(FrozenModel):
    """只覆盖查询、融合、重排和生成语义的冻结输入。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    query_analyzer: JsonObject
    query_planner: JsonObject
    query_expansion_policy: JsonObject
    embedding_query_policies: JsonObject
    embedding_router: ComponentDescriptor
    circuit_breaker: CircuitBreakerPolicy = CircuitBreakerPolicy()
    retrieval_channels: JsonObject
    fusion: JsonObject
    reranker: ComponentDescriptor
    reranker_model: str = Field(min_length=1)
    rerank_mode: str = Field(min_length=1)
    neighbor_parent_expansion: JsonObject
    evidence_policy: JsonObject
    confidence_policy: JsonObject
    generator: ComponentDescriptor
    generator_policy: JsonObject
    citation_protocol: JsonObject
    cache_schema_version: StrictInt = Field(default=1, gt=0)
    absolute_path: str | None = Field(default=None, exclude=True, repr=False)

    @field_validator(
        "query_analyzer",
        "query_planner",
        "query_expansion_policy",
        "embedding_query_policies",
        "retrieval_channels",
        "fusion",
        "neighbor_parent_expansion",
        "evidence_policy",
        "confidence_policy",
        "generator_policy",
        "citation_protocol",
        mode="before",
    )
    @classmethod
    def _freeze_objects(cls, value: object) -> JsonObject:
        return freeze_json_object(value)


def canonical_index_payload(value: IndexFingerprintInput) -> str:
    """输出可审计且不含路径、secret 或正文的 index payload。

    Args:
        value: 已验证的索引指纹输入。

    Returns:
        规范化 JSON 文本。

    """
    return canonical_json(value.model_dump(mode="json", exclude_none=False))


def canonical_serving_payload(value: ServingFingerprintInput) -> str:
    """输出可审计且不含路径、secret 或正文的 serving payload。

    Args:
        value: 已验证的服务指纹输入。

    Returns:
        规范化 JSON 文本。

    """
    return canonical_json(value.model_dump(mode="json", exclude_none=False))


def compute_index_fingerprint(value: IndexFingerprintInput) -> str:
    """计算索引兼容性 SHA-256 指纹。

    Args:
        value: 已验证的索引指纹输入。

    Returns:
        带 `sha256:` 前缀的稳定摘要。

    """
    return canonical_sha256(value.model_dump(mode="json", exclude_none=False))


def compute_serving_fingerprint(value: ServingFingerprintInput) -> str:
    """计算查询和生成语义 SHA-256 指纹。

    Args:
        value: 已验证的服务指纹输入。

    Returns:
        带 `sha256:` 前缀的稳定摘要。

    """
    return canonical_sha256(value.model_dump(mode="json", exclude_none=False))
