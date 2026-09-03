"""严格 JSON Profile 与内置离线/生产候选配置。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from rag_app.core.errors import ConfigurationError
from rag_app.core.models import (
    ChunkingPolicy,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
)
from rag_app.core.models.common import JsonObject, freeze_json_object
from rag_app.core.policies import EgressPolicy, ParsingPolicy

_QUERY_INSTRUCTION = (
    "Given a user query, retrieve the most relevant passages from enterprise "
    "DOCX knowledge bases."
)


class _ProfileModel(BaseModel):
    """Profile schema 的严格冻结基类。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class EmbeddingSlotProfile(_ProfileModel):
    """Profile 中一个 Provider slot 的可审计配置。"""

    slot_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    provider: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    model: str = Field(min_length=1)
    dimension: StrictInt = Field(gt=0)
    max_input_tokens: StrictInt = Field(default=32768, gt=0)
    vector_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    normalization: str = Field(default="l2-v1", min_length=1)
    adapter_revision: str = Field(default="1", min_length=1, max_length=80)
    request_policy_revision: str = Field(
        default="1",
        min_length=1,
        max_length=80,
    )
    document_task: str | None = None
    query_task: str | None = None
    embedding_type: str | None = None
    transport: str | None = None
    document_text_type: str | None = None
    query_text_type: str | None = None
    query_instruct: str | None = None
    output_type: str | None = None
    document_request_policy: JsonObject = ()
    query_request_policy: JsonObject = ()
    api_key_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{2,127}$",
    )
    workspace_id_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{2,127}$",
    )
    region: str | None = Field(default=None, pattern=r"^cn-beijing$")
    region_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{2,127}$",
    )

    @field_validator(
        "document_request_policy",
        "query_request_policy",
        mode="before",
    )
    @classmethod
    def _freeze_policies(cls, value: object) -> JsonObject:
        return freeze_json_object(value)

    @model_validator(mode="after")
    def _validate_supported_provider_contract(self) -> Self:
        supported = {
            "jina-embedding": {
                "model": "jina-embeddings-v5-text-small",
                "dimension": 1024,
                "normalization": "l2-v1",
                "document_task": (None, "retrieval.passage"),
                "query_task": (None, "retrieval.query"),
                "embedding_type": (None, "float"),
                "transport": (None,),
                "document_text_type": (None,),
                "query_text_type": (None,),
                "query_instruct": (None,),
                "output_type": (None,),
                "workspace_id_env": (None,),
                "region": (None,),
                "region_env": (None,),
            },
            "aliyun-qwen37-embedding": {
                "model": "qwen3.7-text-embedding",
                "dimension": 1024,
                "normalization": "l2-v1",
                "document_task": (None,),
                "query_task": (None,),
                "embedding_type": (None,),
                "transport": (None, "dashscope-native"),
                "document_text_type": (None, "document"),
                "query_text_type": (None, "query"),
                "output_type": (None, "dense"),
                "region": (None, "cn-beijing"),
            },
        }.get(self.provider)
        if supported is None:
            return self
        for field_name, expected in supported.items():
            value = getattr(self, field_name)
            allowed = expected if isinstance(expected, tuple) else (expected,)
            if value not in allowed:
                raise ValueError(
                    f"{self.provider} 不支持 {field_name}={value!r}。"
                )
        return self

    def to_identity(self, role: EmbeddingSlotRole) -> EmbeddingSlotIdentity:
        """转换为不含 secret 的 Core slot 身份。

        Args:
            role: primary 或 standby 角色。

        Returns:
            可参与指纹和 Store 校验的 slot 身份。

        """
        document_policy = dict(self.document_request_policy)
        query_policy = dict(self.query_request_policy)
        if self.document_task is not None:
            document_policy["task"] = self.document_task
        if self.query_task is not None:
            query_policy["task"] = self.query_task
        if self.embedding_type is not None:
            document_policy["embedding_type"] = self.embedding_type
            query_policy["embedding_type"] = self.embedding_type
        if self.transport is not None:
            document_policy["transport"] = self.transport
            query_policy["transport"] = self.transport
        if self.document_text_type is not None:
            document_policy["text_type"] = self.document_text_type
        if self.query_text_type is not None:
            query_policy["text_type"] = self.query_text_type
        if self.query_instruct is not None:
            query_policy["query_instruct"] = self.query_instruct
        if self.output_type is not None:
            document_policy["output_type"] = self.output_type
            query_policy["output_type"] = self.output_type
        document_policy["request_policy_revision"] = (
            self.request_policy_revision
        )
        query_policy["request_policy_revision"] = self.request_policy_revision
        return EmbeddingSlotIdentity(
            slot_id=self.slot_id,
            role=role,
            provider_id=self.provider,
            model=self.model,
            vector_name=self.vector_name,
            dimension=self.dimension,
            max_input_tokens=self.max_input_tokens,
            adapter_revision=self.adapter_revision,
            document_request_policy=freeze_json_object(document_policy),
            query_request_policy=freeze_json_object(query_policy),
            normalization=self.normalization,
        )


class EmbeddingTopologyProfile(_ProfileModel):
    """Profile 中 single 或 hot-standby 拓扑。"""

    mode: str = Field(pattern=r"^(single|hot_standby)$")
    activation_policy: str = Field(
        default="all_required_slots_complete",
        pattern=r"^all_required_slots_complete$",
    )
    primary: EmbeddingSlotProfile
    standby: EmbeddingSlotProfile | None = None

    @model_validator(mode="after")
    def _validate_mode(self) -> Self:
        if self.mode == "single" and self.standby is not None:
            raise ValueError("single topology 禁止 standby。")
        if self.mode == "hot_standby" and self.standby is None:
            raise ValueError("hot_standby 必须配置 standby。")
        return self

    def to_core(self) -> EmbeddingTopology:
        """转换为强制唯一 slot/vector name 的 Core topology。

        Args:
            无参数；读取当前 Profile。

        Returns:
            已验证的 Core topology。

        """
        slots = [self.primary.to_identity(EmbeddingSlotRole.PRIMARY)]
        standby_slot_id: str | None = None
        if self.standby is not None:
            slots.append(self.standby.to_identity(EmbeddingSlotRole.STANDBY))
            standby_slot_id = self.standby.slot_id
        return EmbeddingTopology(
            mode=self.mode,
            primary_slot_id=self.primary.slot_id,
            standby_slot_id=standby_slot_id,
            activation_policy=self.activation_policy,
            slots=tuple(slots),
        )


class RerankerProfile(_ProfileModel):
    """Reranker Provider 与显式降级策略。"""

    provider: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    model: str = Field(min_length=1)
    on_unavailable: str = Field(pattern=r"^bypass_keep_rrf$")
    api_key_env: str = Field(
        default="JINA_API_KEY",
        pattern=r"^[A-Z][A-Z0-9_]{2,127}$",
    )
    max_total_tokens: StrictInt = Field(default=32768, gt=0)
    max_candidates: StrictInt = Field(default=100, gt=0)
    request_policy_revision: str = Field(
        default="1",
        min_length=1,
        max_length=80,
    )

    @model_validator(mode="after")
    def _validate_supported_provider_contract(self) -> Self:
        if (
            self.provider == "jina-reranker"
            and self.model != "jina-reranker-v3.5"
        ):
            raise ValueError("Jina Reranker 只支持 jina-reranker-v3.5。")
        return self


class ComponentsProfile(_ProfileModel):
    """全部可替换组件的显式注册名。"""

    parser: str = "legacy-docx-ir"
    chunker: str = "legacy-section-pack"
    embedding_topology: str | EmbeddingTopologyProfile = "deterministic-single"
    embedding_primary: str | None = "deterministic"
    embedding_standby: str | None = None
    embedding_router: str = "embedding-router-single"
    reranker: str | RerankerProfile = "lexical-overlap"
    vector_store: str = "memory"
    lexical_store: str = "memory"
    metadata_store: str = "sqlite"
    blob_store: str = "local"
    generator: str = "extractive"
    trace_sink: str = "sqlite"

    @model_validator(mode="before")
    @classmethod
    def _default_router_for_topology(cls, value: object) -> object:
        if not isinstance(value, dict) or "embedding_router" in value:
            return value
        topology = value.get("embedding_topology")
        if isinstance(topology, dict) and topology.get("mode") == "hot_standby":
            return {**value, "embedding_router": "embedding-router-hot-standby"}
        return value


class LocalDataProfile(_ProfileModel):
    """P06 本地持久化位置与 SQLite/Qdrant 模式。"""

    data_root: str = Field(default=".data", min_length=1)
    sqlite_filename: str = Field(
        default="universal-rag.sqlite3",
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    journal_mode: str = Field(default="WAL", pattern=r"^(WAL|DELETE|MEMORY)$")
    busy_timeout_ms: StrictInt = Field(default=5000, ge=1, le=60000)
    qdrant_mode: str = Field(default="memory", pattern=r"^(memory|path)$")


class RagProfile(_ProfileModel):
    """可嵌入宿主的严格 V1 Profile。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    profile_id: str = Field(
        default="dev-offline",
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    components: ComponentsProfile = ComponentsProfile()
    parsing: ParsingPolicy = ParsingPolicy()
    chunking: ChunkingPolicy = ChunkingPolicy()
    security: EgressPolicy = EgressPolicy()
    local_data: LocalDataProfile = LocalDataProfile()

    def redacted_dict(self) -> dict[str, JsonValue]:
        """导出不读取环境变量值的安全 Profile。

        Args:
            无参数；序列化当前 Profile。

        Returns:
            只含配置和环境变量名的 JSON object。

        """
        payload = self.model_dump(mode="json")
        return {str(key): value for key, value in payload.items()}


def load_profile(path: str | Path) -> RagProfile:
    """从严格 JSON 文件加载 Profile。

    Args:
        path: JSON Profile 路径。

    Returns:
        已完整验证的冻结 Profile。

    Raises:
        ConfigurationError: 文件、JSON 或 schema 无效。

    """
    profile_path = Path(path)
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            "Profile 文件无法作为 UTF-8 JSON 读取。",
            stage="composition.profile",
            details={"error_type": type(error).__name__},
        ) from None
    return profile_from_mapping(payload)


def profile_from_mapping(payload: object) -> RagProfile:
    """一次列出全部路径地验证 Profile mapping。

    Args:
        payload: JSON 解码后的对象。

    Returns:
        已完整验证的冻结 Profile。

    Raises:
        ConfigurationError: 任一字段无效。

    """
    try:
        return RagProfile.model_validate(payload)
    except ValidationError as error:
        paths = tuple(
            ".".join(str(part) for part in item["loc"])
            for item in error.errors(include_url=False)
        )
        raise ConfigurationError(
            "Profile schema 验证失败。",
            stage="composition.profile",
            details={"paths": list(paths)},
        ) from None


def default_offline_profile() -> RagProfile:
    """返回不访问公网的权威开发 Profile。

    Args:
        无参数；使用内置冻结值。

    Returns:
        使用 deterministic/memory/SQLite/extractive 的离线 Profile。

    """
    return RagProfile(
        components=ComponentsProfile(
            parser="docx-ooxml-v4",
            chunker="docx-structural-v3",
        )
    )


def default_hot_standby_profile() -> RagProfile:
    """返回固定 Jina/Qwen3.7/Jina-reranker 候选 Profile。

    Args:
        无参数；使用用户已接受的模型拓扑。

    Returns:
        只含 Provider 身份和 Secret 环境变量名的 Profile。

    """
    primary = EmbeddingSlotProfile(
        slot_id="primary",
        provider="jina-embedding",
        model="jina-embeddings-v5-text-small",
        dimension=1024,
        max_input_tokens=32768,
        vector_name="dense_primary",
        normalization="l2-v1",
        document_task="retrieval.passage",
        query_task="retrieval.query",
        embedding_type="float",
        api_key_env="JINA_API_KEY",
    )
    standby = EmbeddingSlotProfile(
        slot_id="standby",
        provider="aliyun-qwen37-embedding",
        model="qwen3.7-text-embedding",
        dimension=1024,
        max_input_tokens=128000,
        vector_name="dense_standby",
        normalization="l2-v1",
        transport="dashscope-native",
        document_text_type="document",
        query_text_type="query",
        query_instruct=_QUERY_INSTRUCTION,
        output_type="dense",
        api_key_env="DASHSCOPE_API_KEY",
        workspace_id_env="ALIYUN_MODEL_STUDIO_WORKSPACE_ID",
        region="cn-beijing",
        region_env="ALIYUN_MODEL_STUDIO_REGION",
    )
    return RagProfile(
        profile_id="dev-jina-qwen37-hot-standby",
        components=ComponentsProfile(
            parser="docx-ooxml-v4",
            chunker="docx-structural-v3",
            embedding_topology=EmbeddingTopologyProfile(
                mode="hot_standby",
                primary=primary,
                standby=standby,
            ),
            embedding_primary=None,
            embedding_standby=None,
            embedding_router="embedding-router-hot-standby",
            reranker=RerankerProfile(
                provider="jina-reranker",
                model="jina-reranker-v3.5",
                on_unavailable="bypass_keep_rrf",
                api_key_env="JINA_API_KEY",
            ),
        ),
        security=EgressPolicy(
            remote_document_embedding=True,
            remote_query_embedding=True,
            remote_reranking=True,
            remote_document_embedding_jina=True,
            remote_query_embedding_jina=True,
            remote_reranking_jina=True,
            remote_document_embedding_aliyun=True,
            remote_query_embedding_aliyun=True,
            allow_aliyun_embedding_failover=True,
            aliyun_daily_request_budget=100,
            aliyun_daily_token_budget=100000,
        ),
    )
