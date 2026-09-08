"""P07 查询分析、计划与 query embedding 公共模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictInt, field_validator

from rag_app.core.models.common import FrozenModel
from rag_app.core.models.provider import (
    CircuitSnapshot,
    EmbeddingCoverage,
    EmbeddingTopology,
    ProviderCall,
)


class QueryKind(StrEnum):
    """确定性 Query Planner 的稳定分类。"""

    EXACT_IDENTIFIER = "exact_identifier"
    SIMPLE_FACT = "simple_fact"
    TABLE_NUMERIC = "table_numeric"
    AMBIGUOUS = "ambiguous"
    COMPLEX = "complex"


class RequestedAnswerType(StrEnum):
    """查询实际要求返回的答案形状。"""

    UNKNOWN = "UNKNOWN"
    FACT = "FACT"
    ENUMERATION = "ENUMERATION"
    COUNT = "COUNT"
    ORDINAL_ITEM = "ORDINAL_ITEM"
    DUTIES = "DUTIES"
    PROCEDURE = "PROCEDURE"


class ConstraintKind(StrEnum):
    """改写前后必须保持的字面硬约束类别。"""

    IDENTIFIER = "IDENTIFIER"
    NUMBER = "NUMBER"
    UNIT = "UNIT"
    NEGATION = "NEGATION"
    QUALIFIER = "QUALIFIER"
    DATE_VERSION = "DATE_VERSION"
    QUOTED_TEXT = "QUOTED_TEXT"


class QueryConstraint(FrozenModel):
    """保留规范化问题中一个硬约束的位置与原词。"""

    kind: ConstraintKind
    raw_text: str = Field(min_length=1, repr=False)
    normalized_value: str = Field(min_length=1, repr=False)
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)


class QuerySemantics(FrozenModel):
    """检索、证据和回答共同消费的类型化问题语义。"""

    target: str | None = Field(default=None, repr=False)
    relation: str | None = None
    answer_type: RequestedAnswerType = RequestedAnswerType.UNKNOWN
    expected_count: StrictInt | None = Field(default=None, gt=0)
    ordinal: StrictInt | None = Field(default=None, gt=0)
    constraints: tuple[QueryConstraint, ...] = ()
    source: Literal["RULE", "LLM_REWRITE", "ORIGINAL_FALLBACK"] = (
        "ORIGINAL_FALLBACK"
    )
    reason_codes: tuple[str, ...] = ()


class QueryVariant(FrozenModel):
    """受预算约束且只用于召回的 query 变体。"""

    text: str = Field(min_length=1, repr=False)
    kind: str = Field(pattern=r"^(original|normalized|rewrite)$")
    identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class QueryAnalysis(FrozenModel):
    """不丢失数字、否定和标识符的确定性分析结果。"""

    original_query: str = Field(min_length=1, repr=False)
    normalized_query: str = Field(min_length=1, repr=False)
    resolved_query: str | None = Field(default=None, repr=False)
    semantics: QuerySemantics = QuerySemantics()
    quoted_phrases: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    numbers: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    date_version_signals: tuple[str, ...] = ()
    language_hints: tuple[str, ...] = ()
    structural_table_signals: tuple[str, ...] = ()
    negation_signals: tuple[str, ...] = ()
    conversation_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason_codes: tuple[str, ...] = ()


class RetrievalPlan(FrozenModel):
    """一次检索请求的有界执行计划。"""

    query_kind: QueryKind
    variants: tuple[QueryVariant, ...] = Field(min_length=1, max_length=2)
    channels: tuple[str, ...] = Field(min_length=1)
    channel_top_k: tuple[tuple[str, StrictInt], ...]
    must_keep_exact: bool
    use_reranker: bool
    neighbor_mode: str = Field(pattern=r"^(none|same_group|table|section)$")
    evidence_token_budget: StrictInt = Field(gt=0)
    dense_required: bool = False
    reason_codes: tuple[str, ...] = ()
    provisional_confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("channels")
    @classmethod
    def _unique_channels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("检索 channels 禁止重复。")
        return value


@dataclass(frozen=True, slots=True)
class QueryEmbeddingRequest:
    """一次只允许选择一个 Dense slot 的查询。"""

    text: str

    def __post_init__(self) -> None:
        """拒绝空白 query。"""
        if not self.text.strip():
            raise ValueError("Query Embedding 文本不能为空。")


@dataclass(frozen=True, slots=True)
class ActiveRevisionEmbeddingState:
    """Router 所需的 active revision slot 证据。"""

    topology: EmbeddingTopology
    coverages: tuple[EmbeddingCoverage, ...]


@dataclass(frozen=True, slots=True)
class RoutedEmbeddingResult:
    """绑定单一 slot/vector name 的查询向量。"""

    vector: tuple[float, ...]
    selected_slot_id: str
    vector_name: str
    attempted_slot_ids: tuple[str, ...]
    fallback_reason: str
    provider_calls: tuple[ProviderCall, ...]
    circuit_before: tuple[CircuitSnapshot, ...]
    circuit_after: tuple[CircuitSnapshot, ...]


__all__ = [
    "ActiveRevisionEmbeddingState",
    "ConstraintKind",
    "QueryAnalysis",
    "QueryConstraint",
    "QueryEmbeddingRequest",
    "QueryKind",
    "QuerySemantics",
    "QueryVariant",
    "RequestedAnswerType",
    "RetrievalPlan",
    "RoutedEmbeddingResult",
]
