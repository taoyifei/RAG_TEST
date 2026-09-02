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
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
)
from rag_app.core.models.common import JsonObject, freeze_json_object
from rag_app.core.policies import EgressPolicy

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
    vector_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    normalization: str = Field(default="l2", min_length=1)
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

    @field_validator(
        "document_request_policy",
        "query_request_policy",
        mode="before",
    )
    @classmethod
    def _freeze_policies(cls, value: object) -> JsonObject:
        return freeze_json_object(value)

    def to_identity(self, role: EmbeddingSlotRole) -> EmbeddingSlotIdentity:
        """转换为不含 secret 的 Core slot 身份。

        Args:
            role: primary 或 standby 角色。

        Returns:
            可参与指纹和 Store 校验的 slot 身份。

        """
        return EmbeddingSlotIdentity(
            slot_id=self.slot_id,
            role=role,
            provider_id=self.provider,
            model=self.model,
            vector_name=self.vector_name,
            dimension=self.dimension,
            document_request_policy=self.document_request_policy,
            query_request_policy=self.query_request_policy,
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
    api_key_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{2,127}$",
    )


class ComponentsProfile(_ProfileModel):
    """全部可替换组件的显式注册名。"""

    parser: str = "legacy-docx"
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


class RagProfile(_ProfileModel):
    """可嵌入宿主的严格 V1 Profile。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    profile_id: str = Field(
        default="dev-offline",
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    components: ComponentsProfile = ComponentsProfile()
    security: EgressPolicy = EgressPolicy()

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
    return RagProfile()


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
        vector_name="dense_primary",
        normalization="l2",
        document_request_policy=freeze_json_object(
            {"task": "retrieval.passage", "type": "float"}
        ),
        query_request_policy=freeze_json_object(
            {"task": "retrieval.query", "type": "float"}
        ),
        api_key_env="JINA_API_KEY",
    )
    standby = EmbeddingSlotProfile(
        slot_id="standby",
        provider="aliyun-qwen37-embedding",
        model="qwen3.7-text-embedding",
        dimension=1024,
        vector_name="dense_standby",
        normalization="provider",
        document_request_policy=freeze_json_object(
            {
                "text_type": "document",
                "output_type": "dense",
                "transport": "dashscope-native",
            }
        ),
        query_request_policy=freeze_json_object(
            {
                "text_type": "query",
                "output_type": "dense",
                "query_instruct": _QUERY_INSTRUCTION,
                "transport": "dashscope-native",
            }
        ),
        api_key_env="DASHSCOPE_API_KEY",
        workspace_id_env="ALIYUN_MODEL_STUDIO_WORKSPACE_ID",
        region="cn-beijing",
    )
    return RagProfile(
        profile_id="jina-qwen37-hot-standby",
        components=ComponentsProfile(
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
    )
