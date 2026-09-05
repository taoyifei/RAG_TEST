"""P07 revision snapshot、通道、融合和最终查询公共模型。"""

from __future__ import annotations

import math
from typing import Self

from pydantic import (
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.chunk import Chunk, SourceSpan
from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)
from rag_app.core.models.confidence import ConfidenceDecision, ConfidenceStatus
from rag_app.core.models.document import KnowledgeBaseScope
from rag_app.core.models.lifecycle import IndexRevisionRef, IndexRevisionState
from rag_app.core.models.provider import EmbeddingCoverage, EmbeddingTopology
from rag_app.core.models.query import QueryAnalysis, QueryKind
from rag_app.core.models.retrieval import EvidenceItem
from rag_app.core.models.revisions import RevisionVectorSpec

_MAX_CONVERSATION_TURN_LENGTH = 2000


class RetrievalPolicy(FrozenModel):
    """P07 尚未经过 P08 校准的有界执行参数。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    max_variants: StrictInt = Field(default=2, ge=1, le=2)
    max_channels: StrictInt = Field(default=6, ge=1, le=8)
    channel_top_k: StrictInt = Field(default=24, gt=0, le=100)
    fusion_candidate_limit: StrictInt = Field(default=48, gt=0, le=200)
    rrf_k: StrictInt = Field(default=60, gt=0)
    rerank_candidate_limit: StrictInt = Field(default=24, gt=0, le=100)
    neighbor_count: StrictInt = Field(default=1, ge=0, le=4)
    section_chunk_limit: StrictInt = Field(default=2, ge=0, le=8)
    evidence_token_budget: StrictInt = Field(default=1024, gt=0)
    max_evidence_items: StrictInt = Field(default=8, gt=0, le=50)
    max_evidence_items_per_chunk: StrictInt = Field(default=1, gt=0, le=8)
    minimum_support_items: StrictInt = Field(default=1, gt=0, le=8)
    minimum_span_overlap: float = Field(default=0.2, ge=0.0, le=1.0)
    per_document_cap: StrictInt = Field(default=4, gt=0)
    per_section_cap: StrictInt = Field(default=3, gt=0)
    must_keep_limit: StrictInt = Field(default=3, ge=0, le=10)
    rerank_text_char_limit: StrictInt = Field(default=2400, gt=0, le=10000)
    cache_schema_version: StrictInt = Field(default=1, gt=0)
    dense_semantic_enabled: bool = False
    dense_semantic_calibration_state: str = Field(
        default="UNCALIBRATED",
        pattern=r"^(UNCALIBRATED|CONTROLLED_TEST_ONLY|LIVE_CALIBRATED)$",
    )
    dense_calibrated_vector_spaces: tuple[str, ...] = ()
    bypass_policy_denied: bool = True
    enabled_channels: tuple[str, ...] = ("exact", "lexical", "dense")
    rerank_enabled: bool = True
    neighbor_expansion_enabled: bool = True
    provisional: bool = True

    @field_validator("enabled_channels")
    @classmethod
    def _validate_channels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"exact", "lexical", "dense"}
        if (
            not value
            or len(value) != len(set(value))
            or not set(value) <= allowed
        ):
            raise ValueError("enabled channels 必须非空、唯一且受支持。")
        return value

    @model_validator(mode="after")
    def _validate_dense_requirements(self) -> Self:
        if self.max_channels < len(self.enabled_channels):
            raise ValueError("max_channels 不能小于 enabled channels 数量。")
        if self.minimum_support_items > self.max_evidence_items:
            raise ValueError("minimum support 不能超过 evidence 总上限。")
        if self.dense_semantic_enabled and (
            self.dense_semantic_calibration_state == "UNCALIBRATED"
            or not self.dense_calibrated_vector_spaces
        ):
            raise ValueError(
                "Dense semantic 启用时必须绑定校准状态和向量空间。"
            )
        return self


class SearchRequest(FrozenModel):
    """受 scope、filter 和有限会话约束的 P07 查询。"""

    scope: KnowledgeBaseScope
    text: str = Field(min_length=1, max_length=8000, repr=False)
    limit: StrictInt = Field(default=10, gt=0, le=50)
    conversation_context: tuple[str, ...] = Field(default=(), max_length=8)
    metadata_filters: JsonObject = ()
    access_filters: JsonObject = ()
    dense_required: bool = False

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("search query 禁止仅含空白。")
        return value

    @field_validator("conversation_context")
    @classmethod
    def _bound_context(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item.strip()
            or len(item) > _MAX_CONVERSATION_TURN_LENGTH
            for item in value
        ):
            raise ValueError("conversation turn 必须非空且有界。")
        return value

    @field_validator("metadata_filters", "access_filters", mode="before")
    @classmethod
    def _freeze_filters(cls, value: object) -> JsonObject:
        return freeze_json_object(value)


class ActiveRevisionQuerySnapshot(FrozenModel):
    """一个查询从开始到完成保持不变的持久化索引快照。"""

    revision: IndexRevisionRef
    serving_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    topology: EmbeddingTopology
    coverages: tuple[EmbeddingCoverage, ...]
    vector_spec: RevisionVectorSpec
    lexical_namespace: str = Field(min_length=1)
    exact_namespace: str = Field(min_length=1)
    chunk_payload_schema: str = Field(min_length=1)
    retrieval_policy: RetrievalPolicy
    profile_revision_id: str | None = None

    @model_validator(mode="after")
    def _validate_snapshot(self) -> ActiveRevisionQuerySnapshot:
        if self.revision.state is not IndexRevisionState.ACTIVE:
            raise ValueError("查询 snapshot 只能从 ACTIVE revision 创建。")
        if self.vector_spec.revision != self.revision:
            raise ValueError("查询 snapshot 的 vector spec revision 不一致。")
        if self.topology.slots != self.vector_spec.slots:
            raise ValueError(
                "查询 snapshot 的 topology 与 vector spec 不一致。"
            )
        return self


class ExactSearchRequest(FrozenModel):
    """Exact Identifier 与受控 quoted phrase 查询。"""

    revision: IndexRevisionRef
    identifiers: tuple[str, ...] = ()
    quoted_phrases: tuple[str, ...] = ()
    limit: StrictInt = Field(default=20, gt=0, le=100)


class ChannelHit(FrozenModel):
    """不携带正文的单通道候选。"""

    revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    role: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel: str = Field(min_length=1, max_length=80)
    rank: StrictInt = Field(gt=0)
    raw_score: StrictFloat
    match_type: str | None = Field(default=None, max_length=80)
    must_keep: bool = False

    @field_validator("raw_score")
    @classmethod
    def _finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("channel raw score 必须为有限值。")
        return value


class RrfContribution(FrozenModel):
    """一个通道对候选 RRF 分数的可解释贡献。"""

    channel: str
    rank: StrictInt = Field(gt=0)
    weight: StrictFloat = Field(gt=0.0)
    contribution: StrictFloat = Field(gt=0.0)


class FusedCandidate(FrozenModel):
    """仅由通道 rank 融合的候选。"""

    revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    role: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: StrictFloat = Field(gt=0.0)
    best_channel_rank: StrictInt = Field(gt=0)
    must_keep: bool = False
    contributions: tuple[RrfContribution, ...] = Field(min_length=1)


class HydratedChunk(FrozenModel):
    """来自 SQLite canonical store 的 Chunk 与显示身份。"""

    chunk: Chunk
    display_name: str = Field(min_length=1)


class RankedChunk(FrozenModel):
    """融合、重排及结构扩展共享的 canonical 候选。"""

    hydrated: HydratedChunk
    fusion_rank: StrictInt = Field(gt=0)
    rerank_rank: StrictInt | None = Field(default=None, gt=0)
    rerank_score: StrictFloat | None = None
    must_keep: bool = False
    contributions: tuple[RrfContribution, ...] = ()
    expansion_reason: str | None = None

    @field_validator("rerank_score")
    @classmethod
    def _finite_rerank(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("rerank score 必须为有限值。")
        return value


class EvidenceSelectionContext(FrozenModel):
    """Evidence V2 的查询、路由与重排上下文。"""

    analysis: QueryAnalysis
    query_kind: QueryKind
    rerank_mode: str = Field(min_length=1)
    selected_slot: str | None = None


class DiagnosticRerankItem(FrozenModel):
    """不含正文的重排身份与安全分数。"""

    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    rank: StrictInt = Field(gt=0)
    score: StrictFloat | None = None


class DiagnosticFusionItem(FrozenModel):
    """不含正文的融合排名、分数与逐通道 RRF 贡献。"""

    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    rank: StrictInt = Field(gt=0)
    score: StrictFloat = Field(gt=0.0)
    contributions: tuple[RrfContribution, ...] = Field(min_length=1)


class DiagnosticExpansionItem(FrozenModel):
    """不含正文的扩展候选身份与原因。"""

    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    reason: str | None = None


class DiagnosticEvidenceItem(FrozenModel):
    """Evidence 的安全身份和 canonical source range。"""

    evidence_id: str = Field(min_length=1)
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    source_ranges: tuple[SourceSpan, ...] = ()


class StageTiming(FrozenModel):
    """单个检索阶段的实际耗时。"""

    stage: str = Field(min_length=1)
    elapsed_ms: StrictFloat = Field(ge=0.0)


class ProviderCallCount(FrozenModel):
    """按 Provider 操作汇总的真实调用与重试计数。"""

    operation: str = Field(min_length=1)
    call_count: StrictInt = Field(ge=0)
    retry_count: StrictInt = Field(ge=0)


class RetrievalDiagnostics(FrozenModel):
    """不含正文、向量、Prompt 或 Secret 的完整检索诊断。"""

    channel_chunk_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    fused_chunk_ids: tuple[str, ...] = ()
    fusion: tuple[DiagnosticFusionItem, ...] = ()
    reranked: tuple[DiagnosticRerankItem, ...] = ()
    expanded: tuple[DiagnosticExpansionItem, ...] = ()
    evidence: tuple[DiagnosticEvidenceItem, ...] = ()
    cited_chunk_ids: tuple[str, ...] = ()
    provider_calls: tuple[ProviderCallCount, ...] = ()
    cache_hit: bool = False
    stage_timings: tuple[StageTiming, ...] = ()
    degraded_reason_codes: tuple[str, ...] = ()


class RetrievalDiagnosticsSummary(FrozenModel):
    """可随公共结果返回的有界诊断摘要。"""

    channel_count: StrictInt = Field(ge=0)
    fused_count: StrictInt = Field(ge=0)
    reranked_count: StrictInt = Field(ge=0)
    evidence_count: StrictInt = Field(ge=0)
    provider_call_count: StrictInt = Field(ge=0)
    provider_retry_count: StrictInt = Field(ge=0)
    cache_hit: bool


class BaseResultCacheKey(FrozenModel):
    """可在 Provider 调用前计算的最终结果缓存身份。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    active_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    serving_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_filter_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    access_filter_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    conversation_identity: str = Field(min_length=1)
    rewrite_policy_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cache_schema: StrictInt = Field(gt=0)

    @property
    def persistent_key(self) -> str:
        """返回不含 Query 或过滤值的稳定 SHA-256 key。

        Args:
            无参数；读取当前冻结模型。

        Returns:
            规范 JSON 的 SHA-256 identity。

        """
        return canonical_sha256(self.model_dump(mode="json"))


class SearchAnswerResult(FrozenModel):
    """实际 revision、route、证据、拒答与回答的统一结果。"""

    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    status: ConfidenceStatus
    reason_code: str = Field(min_length=1)
    answer: str | None = Field(default=None, repr=False)
    evidence: tuple[EvidenceItem, ...] = ()
    confidence: ConfidenceDecision
    query_kind: QueryKind
    active_index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    serving_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    selected_embedding_slot: str | None = None
    selected_vector_name: str | None = None
    route_reason_code: str
    rerank_execution_mode: str
    generation_mode: str = Field(pattern=r"^(extractive|none)$")
    degraded_reason_codes: tuple[str, ...] = ()
    cache_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cache_hit: bool = False
    diagnostics_summary: RetrievalDiagnosticsSummary | None = None
    diagnostics: RetrievalDiagnostics | None = Field(
        default=None, exclude=True, repr=False
    )


__all__ = [
    "ActiveRevisionQuerySnapshot",
    "BaseResultCacheKey",
    "ChannelHit",
    "DiagnosticEvidenceItem",
    "DiagnosticExpansionItem",
    "DiagnosticFusionItem",
    "DiagnosticRerankItem",
    "EvidenceSelectionContext",
    "ExactSearchRequest",
    "FusedCandidate",
    "HydratedChunk",
    "ProviderCallCount",
    "RankedChunk",
    "RetrievalDiagnostics",
    "RetrievalDiagnosticsSummary",
    "RetrievalPolicy",
    "RrfContribution",
    "SearchAnswerResult",
    "SearchRequest",
    "StageTiming",
]
