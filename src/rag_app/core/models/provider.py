"""Provider、Embedding 主备和重排公共模型。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import (
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)

_HOT_STANDBY_SLOT_COUNT = 2


class ProviderHealthStatus(StrEnum):
    """Provider 对外健康状态。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DEGRADED_CONTRACT = "degraded_contract"
    DEGRADED_CONFIG = "degraded_config"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProviderHealth(FrozenModel):
    """不触发隐式网络的 Provider 健康快照。"""

    status: ProviderHealthStatus
    checked_network: bool = False
    reason_code: str = Field(default="NOT_CHECKED", min_length=1)


class ProviderCall(FrozenModel):
    """不含正文和凭据的 Provider 调用审计。"""

    provider_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    call_count: StrictInt = Field(ge=0)
    retry_count: StrictInt = Field(ge=0)
    elapsed_ms: StrictInt = Field(ge=0)
    reason_code: str | None = None
    model: str | None = None
    endpoint: str | None = None
    attempt_count: StrictInt | None = Field(default=None, ge=1)
    status_category: str | None = None
    retry_after_ms: StrictInt | None = Field(default=None, ge=0)
    input_count: StrictInt | None = Field(default=None, ge=0)
    estimated_tokens: StrictInt | None = Field(default=None, ge=0)


class ProviderFailureCategory(StrEnum):
    """应用层允许识别的 Provider 失败类别。"""

    TRANSIENT = "transient"
    RESPONSE_CONTRACT = "response_contract"
    AUTH_OR_MODEL = "auth_or_model"
    INPUT_INVALID = "input_invalid"
    POLICY_DENIED = "policy_denied"
    STORE_INCOMPATIBLE = "store_incompatible"


class EmbeddingSlotRole(StrEnum):
    """Embedding slot 在拓扑中的角色。"""

    PRIMARY = "primary"
    STANDBY = "standby"


class EmbeddingRequestRole(StrEnum):
    """Core 传给 adapter 的文本用途。"""

    DOCUMENT = "document"
    QUERY = "query"


class EmbeddingSlotId(StrEnum):
    """V1 固定且可审计的 slot ID。"""

    PRIMARY = "primary"
    STANDBY = "standby"


class EmbeddingSlotIdentity(FrozenModel):
    """一个不可与其它 slot 混用的向量空间身份。"""

    slot_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    role: EmbeddingSlotRole
    provider_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    vector_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    dimension: StrictInt = Field(gt=0)
    max_input_tokens: StrictInt = Field(default=32768, gt=0)
    adapter_revision: str = Field(default="1", min_length=1, max_length=80)
    document_request_policy: JsonObject = ()
    query_request_policy: JsonObject = ()
    normalization: str = Field(min_length=1)

    @field_validator(
        "document_request_policy",
        "query_request_policy",
        mode="before",
    )
    @classmethod
    def _freeze_policies(cls, value: object) -> JsonObject:
        return freeze_json_object(value)

    @property
    def vector_space_identity(self) -> str:
        """返回明确包含 slot 的向量空间身份。

        Args:
            无参数；读取当前 slot。

        Returns:
            不会因维度相同而相等的向量空间身份。

        """
        return ":".join(
            (
                self.slot_id,
                self.provider_id,
                self.model,
                str(self.dimension),
                self.normalization,
                self.adapter_revision,
            )
        )


class EmbeddingTopology(FrozenModel):
    """单 slot 或真正双索引热备拓扑。"""

    mode: str = Field(pattern=r"^(single|hot_standby)$")
    primary_slot_id: str
    standby_slot_id: str | None = None
    activation_policy: str = Field(
        default="all_required_slots_complete",
        pattern=r"^all_required_slots_complete$",
    )
    slots: tuple[EmbeddingSlotIdentity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_topology(self) -> Self:
        slot_ids = tuple(slot.slot_id for slot in self.slots)
        vector_names = tuple(slot.vector_name for slot in self.slots)
        if len(set(slot_ids)) != len(slot_ids):
            raise ValueError("embedding topology 的 slot_id 必须唯一。")
        if len(set(vector_names)) != len(vector_names):
            raise ValueError("embedding topology 的 vector_name 必须唯一。")
        primary = tuple(
            slot
            for slot in self.slots
            if slot.role is EmbeddingSlotRole.PRIMARY
        )
        standby = tuple(
            slot
            for slot in self.slots
            if slot.role is EmbeddingSlotRole.STANDBY
        )
        if len(primary) != 1 or primary[0].slot_id != self.primary_slot_id:
            raise ValueError("topology 必须恰有一个匹配的 primary slot。")
        if self.mode == "single":
            if (
                len(self.slots) != 1
                or standby
                or self.standby_slot_id is not None
            ):
                raise ValueError("single topology 只能包含一个 primary slot。")
        elif (
            len(self.slots) != _HOT_STANDBY_SLOT_COUNT
            or len(standby) != 1
            or standby[0].slot_id != self.standby_slot_id
        ):
            raise ValueError("hot_standby 必须恰有匹配的 primary 和 standby。")
        return self

    def slot(self, slot_id: str) -> EmbeddingSlotIdentity:
        """按显式 slot ID 取向量空间。

        Args:
            slot_id: 目标 slot ID。

        Returns:
            匹配的 slot 身份。

        Raises:
            KeyError: slot 不在拓扑中。

        """
        for candidate in self.slots:
            if candidate.slot_id == slot_id:
                return candidate
        raise KeyError(slot_id)


class CircuitState(StrEnum):
    """Provider circuit 的稳定状态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class CircuitKey:
    """Provider、操作与模型组成的 circuit 唯一键。"""

    provider_id: str
    operation: str
    model: str


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    """不含 secret 的 circuit 状态快照。"""

    key: CircuitKey
    state: CircuitState
    consecutive_failures: int
    recovery_successes: int
    reason_code: str


class FailoverReason(StrEnum):
    """Dense 路由选择或降级原因。"""

    PRIMARY_SELECTED = "primary_selected"
    PRIMARY_TRANSIENT_FAILURE = "primary_transient_failure"
    PRIMARY_RESPONSE_CONTRACT = "primary_response_contract"
    PRIMARY_AUTH_OR_MODEL = "primary_auth_or_model"
    POLICY_DENIED = "policy_denied"
    STORE_INCOMPATIBLE = "store_incompatible"
    DENSE_UNAVAILABLE = "dense_unavailable"


class EmbeddingRouteDecision(FrozenModel):
    """单次请求内保持粘性的 Dense slot 决策。"""

    selected_slot_id: str | None
    attempted_slot_ids: tuple[str, ...]
    reason_code: str = Field(min_length=1)
    dense_available: bool
    circuit_states: tuple[tuple[str, CircuitState], ...] = ()
    revision_coverages: tuple[tuple[str, StrictFloat], ...] = ()

    @field_validator("circuit_states", "revision_coverages", mode="before")
    @classmethod
    def _freeze_maps(cls, value: object) -> object:
        if isinstance(value, dict):
            return tuple(sorted(value.items()))
        return value


class EmbeddingCoverage(FrozenModel):
    """revision 中一个 slot 的向量覆盖证据。"""

    slot_id: str
    vector_name: str
    vector_count: StrictInt = Field(ge=0)
    chunk_count: StrictInt = Field(ge=0)
    observed_dimension: StrictInt = Field(gt=0)

    @property
    def ratio(self) -> float:
        """返回覆盖率。

        Args:
            无参数；读取当前计数。

        Returns:
            chunk 为空时为 0，否则为向量数除以 chunk 数。

        """
        if self.chunk_count == 0:
            return 0.0
        return self.vector_count / self.chunk_count


class EmbeddingRequest(FrozenModel):
    """携带 slot 和领域角色的批量 Embedding 请求。"""

    slot_id: str
    role: EmbeddingRequestRole
    texts: tuple[str, ...] = Field(min_length=1, repr=False)

    @field_validator("texts")
    @classmethod
    def _validate_texts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not text.strip() for text in value):
            raise ValueError("embedding texts 禁止为空白。")
        return value


class EmbeddingResult(FrozenModel):
    """与请求顺序一致且绑定 slot 的向量结果。"""

    slot_id: str
    role: EmbeddingRequestRole
    vectors: tuple[tuple[StrictFloat, ...], ...] = Field(
        min_length=1,
        repr=False,
    )
    observed_dimension: StrictInt = Field(gt=0)
    request_policy_identity: str = Field(min_length=1)
    calls: tuple[ProviderCall, ...] = ()

    @model_validator(mode="after")
    def _validate_vectors(self) -> Self:
        if any(
            len(vector) != self.observed_dimension
            for vector in self.vectors
        ):
            raise ValueError("embedding 向量维度与 observed_dimension 不一致。")
        if any(
            not math.isfinite(value)
            for vector in self.vectors
            for value in vector
        ):
            raise ValueError("embedding 向量禁止包含 NaN 或 Inf。")
        return self


class RerankExecutionMode(StrEnum):
    """重排执行或显式旁路模式。"""

    PROVIDER = "provider"
    LEXICAL_OVERLAP = "lexical_overlap"
    BYPASS_KEEP_RRF = "bypass_keep_rrf"


class RerankItem(FrozenModel):
    """按候选 ID 返回的重排项。"""

    candidate_id: str
    score: StrictFloat


class RerankRequest(FrozenModel):
    """格式中立的重排请求。"""

    query: str = Field(min_length=1, repr=False)
    candidates: tuple[tuple[str, str], ...] = Field(min_length=1, repr=False)
    limit: StrictInt = Field(gt=0)


class RerankResult(FrozenModel):
    """重排结果和安全调用审计。"""

    mode: RerankExecutionMode
    items: tuple[RerankItem, ...]
    calls: tuple[ProviderCall, ...] = ()
    reason_code: str | None = None
