"""P07 Active Snapshot 到模型证据回答的统一同步路由。"""

from __future__ import annotations

import hashlib
import json
import re
import traceback
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from copy import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Literal

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
)
from rag_app.application.retrieval.adaptive import (
    AdaptivePlannerPort,
    AdaptivePlanOutcome,
    ReasoningEffort,
    catalog_matches,
    is_navigation_query,
    reasoning_effort,
)
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.atom_group_alignment import (
    AlignmentQualification,
    AtomEvidenceQualification,
    AtomGroupAlignment,
    align_atom_to_groups,
    qualify_atom_evidence,
)
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.context_resolution import (
    CONTEXT_RESOLUTION_REVISION,
    build_input_spans,
    degraded_query_plan,
    resolve_root_query,
)
from rag_app.application.retrieval.dense import DenseChannel
from rag_app.application.retrieval.evidence import (
    EvidenceAssembler,
    group_source_maps_covered,
    requires_complete_evidence_group,
)
from rag_app.application.retrieval.evidence_groups import (
    GroupCandidate,
    build_catalog_evidence_group,
    build_evidence_groups,
    pack_evidence_groups_with_diagnostics,
    rank_evidence_groups,
)
from rag_app.application.retrieval.exact import ExactChannel
from rag_app.application.retrieval.expansion import RuleBasedNormalizer
from rag_app.application.retrieval.filters import apply_candidate_filters
from rag_app.application.retrieval.fusion import reciprocal_rank_fusion
from rag_app.application.retrieval.generation_evidence import (
    GENERATION_EVIDENCE_PACK_REVISION,
    build_generation_evidence_pack,
)
from rag_app.application.retrieval.hydration import CandidateHydrator
from rag_app.application.retrieval.lexical import LexicalChannel
from rag_app.application.retrieval.neighbors import (
    ExpansionOutcome,
    NeighborExpander,
)
from rag_app.application.retrieval.per_atom_correction import (
    PerAtomCorrectionOutcome,
    correct_per_atom,
)
from rag_app.application.retrieval.planner import QueryPlanner
from rag_app.application.retrieval.query_plan_retrieval import (
    QueryUnit,
    QueryUnitRetrieval,
    fuse_query_units,
)
from rag_app.application.retrieval.related import (
    DISPLAY_POLICY,
    related_display_message,
    rerank_dependency_failed,
    select_related_contents,
    validate_candidate,
)
from rag_app.application.retrieval.reranking import (
    CircuitAwareReranker,
    RerankingOutcome,
)
from rag_app.application.retrieval.structural import StructuralChannel
from rag_app.core.errors import (
    ChannelRateLimited,
    ChannelUnavailable,
    DenseUnavailable,
    IndexCompatibilityError,
    IndexCorrupt,
    PolicyDenied,
    QueryCancelled,
    RagError,
    StreamDeliveryError,
    ValidationFailed,
)
from rag_app.core.events import TraceEvent
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    AnswerClaim,
    BaseResultCacheKey,
    CatalogCitation,
    ChannelHit,
    Chunk,
    CircuitSnapshot,
    ConfidenceDecision,
    ConfidenceStatus,
    DiagnosticEvidenceItem,
    DiagnosticExpansionItem,
    DiagnosticFusionItem,
    DiagnosticRerankItem,
    EvidenceItem,
    EvidenceSelectionContext,
    FusedCandidate,
    OcrVerificationState,
    ProviderCall,
    ProviderCallCount,
    ProviderFailureCategory,
    QueryAnalysis,
    QueryDataPlane,
    QueryVariant,
    RankedChunk,
    RelatedContent,
    RetrievalDiagnostics,
    RetrievalDiagnosticsSummary,
    RetrievalPlan,
    RetrievalPolicy,
    SearchAnswerResult,
    SearchRequest,
    SourceSpan,
    StageTiming,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query import RequestedAnswerType
from rag_app.core.models.query_plan import (
    ATOM_GROUP_ALIGNMENT_REVISION,
    CORRECTIVE_RETRIEVAL_REVISION,
    EVIDENCE_GROUP_SCHEMA_REVISION,
    GROUNDED_CLAIM_SCHEMA_REVISION,
    NATURAL_RENDERER_REVISION,
    QUERY_PLAN_SCHEMA_REVISION,
    QUERY_UNIT_FUSION_REVISION,
    AtomAnswerShape,
    AtomCandidateLink,
    AtomCoverage,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
    fallback_query_plan,
    make_query_plan,
)
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import (
    CancellationPort,
    CriticalOcrVerifierPort,
    EvidenceSourcePort,
    ExactStorePort,
    GeneratorPort,
    LexicalStorePort,
    QueryEmbeddingPort,
    QueryInterpretPort,
    RerankerPort,
    RetrievalCachePort,
    TracePort,
    VectorStorePort,
)
from rag_app.core.ports.evidence_source import CatalogDocument
from rag_app.core.ports.query_rewrite import QueryRewritePort


@dataclass(frozen=True, slots=True)
class _SelectionOutcome:
    """一次候选融合到置信判断的内部结果。"""

    fused: tuple[FusedCandidate, ...]
    reranked: RerankingOutcome
    expansion: ExpansionOutcome
    evidence: tuple[EvidenceItem, ...]
    model_evidence_candidates: tuple[EvidenceItem, ...]
    evidence_decisions: tuple[tuple[str, str], ...]
    ambiguous_support: bool
    confidence: ConfidenceDecision
    groups: tuple[GroupCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class _AtomRetrievalOutcome:
    """Root 与 Atom 初召回经两级融合后的唯一通道包。"""

    channels: dict[str, tuple[ChannelHit, ...]]
    links: tuple[AtomCandidateLink, ...]
    selected_slot: str | None
    selected_vector: str | None
    route_reason: str
    fused: tuple[FusedCandidate, ...]
    seed_chunk_ids: tuple[str, ...]
    rerank_query: str


_CONFLICT_QUANTITY = re.compile(
    r"(?<![\d.])(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>％|%|毫秒|分钟|小时|秒|天|日|周|个月|月|年|"
    r"万元|亿元|元|人|次|个|件|项)"
)
_MAX_SCALAR_DIRECT_CANDIDATES = 12


def _atom_scoped_candidates(  # noqa: PLR0913
    atom: QueryAtom,
    candidates: tuple[RankedChunk, ...],
    groups: tuple[GroupCandidate, ...],
    links: tuple[AtomCandidateLink, ...],
    *,
    policy: RetrievalPolicy,
    multi_atom: bool,
) -> tuple[
    tuple[RankedChunk, ...],
    tuple[GroupCandidate, ...],
    tuple[AtomGroupAlignment, ...],
]:
    """按目标和结构身份锁组；Root 强锚点可补足 Atom 召回失误。"""
    if not groups and not multi_atom:
        return candidates, (), ()
    alignments = align_atom_to_groups(atom, groups, links, policy)
    selected_ids = {
        alignment.group_id
        for alignment in alignments
        if alignment.qualification is not AlignmentQualification.REJECTED
    }
    selected_groups = tuple(
        group for group in groups if group.group_id in selected_ids
    )[: policy.atom_group_max_per_atom]
    scoped = {
        member.hydrated.chunk.chunk_id: member
        for group in selected_groups
        for member in group.members
    }
    if atom.answer_shape in {
        AtomAnswerShape.FACT,
        AtomAnswerShape.DEFINITION,
        AtomAnswerShape.DURATION,
        AtomAnswerShape.COUNT,
        AtomAnswerShape.RESPONSIBLE_PARTY,
    }:
        linked_ids = {
            link.chunk_id
            for link in links
            if link.atom_id in {None, atom.atom_id}
        }
        for candidate in candidates:
            chunk_id = candidate.hydrated.chunk.chunk_id
            if (
                chunk_id in linked_ids
                and len(scoped) < _MAX_SCALAR_DIRECT_CANDIDATES
            ):
                scoped.setdefault(chunk_id, candidate)
    return tuple(scoped.values()), selected_groups, alignments


def _numeric_conflict(
    atom: QueryAtom, evidence: tuple[EvidenceItem, ...]
) -> tuple[EvidenceItem, EvidenceItem] | tuple[()]:
    """仅识别不同文档对同一目标关系的明确单值、同单位冲突。"""
    if atom.answer_shape not in {
        AtomAnswerShape.FACT,
        AtomAnswerShape.DURATION,
        AtomAnswerShape.COUNT,
    }:
        return ()
    comparable: list[tuple[EvidenceItem, Decimal, str]] = []
    target = unicodedata.normalize("NFKC", atom.target).casefold()
    relation = unicodedata.normalize("NFKC", atom.relation).casefold()
    for item in evidence:
        if (
            not item.document_id
            or not item.publishable
            or not item.source_spans
        ):
            continue
        text = unicodedata.normalize("NFKC", item.citation_text).casefold()
        if target not in text or relation not in text:
            continue
        if atom.source_qualifier:
            label = unicodedata.normalize(
                "NFKC",
                " ".join(
                    (
                        item.display_name or "",
                        str(dict(item.metadata).get("document_title", "")),
                    )
                ),
            ).casefold()
            if (
                unicodedata.normalize("NFKC", atom.source_qualifier).casefold()
                not in label
            ):
                continue
        values = tuple(_CONFLICT_QUANTITY.finditer(text))
        if len(values) == 1:
            comparable.append(
                (
                    item,
                    Decimal(values[0]["value"]),
                    values[0]["unit"],
                )
            )
    for index, (left, left_value, left_unit) in enumerate(comparable):
        for right, right_value, right_unit in comparable[index + 1 :]:
            if (
                left.document_id != right.document_id
                and left_unit == right_unit
                and left_value != right_value
            ):
                return left, right
    return ()


def _flatten_evidence_groups(
    groups: tuple[GroupCandidate, ...],
) -> tuple[RankedChunk, ...]:
    """按组排序展开成员，保留原始 Chunk 与来源映射。"""
    flattened: list[RankedChunk] = []
    seen: set[str] = set()
    for rank, group in enumerate(groups, start=1):
        for member in group.members:
            chunk_id = member.hydrated.chunk.chunk_id
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            flattened.append(
                member.model_copy(
                    update={"rerank_rank": member.rerank_rank or rank}
                )
            )
    return tuple(flattened)


@dataclass(frozen=True, slots=True)
class RetrievalExecutionIdentity:
    """可在 Provider 前冻结的等价计算身份。"""

    key_hash: str
    active_revision_id: str
    serving_fingerprint: str


@dataclass(frozen=True, slots=True)
class QueryDataPlaneContext:
    """组合根为单次查询提供的非敏感配置与授权真相。"""

    retrieval_data_plane: Literal[
        "active_remote_profile", "default_local_fallback"
    ] = "default_local_fallback"
    active_retrieval_profile_revision_id: str | None = None
    reranker_provider_id: str | None = None
    reranker_model: str | None = None
    generation_provider_id: str | None = None
    generation_model: str | None = None
    rewrite_provider_id: str | None = None
    rewrite_model: str | None = None
    interpret_provider_id: str | None = None
    interpret_model: str | None = None
    model_configuration_state: str = "NOT_CONFIGURED"
    model_authorization_state: str = "NOT_REQUIRED"
    corpus_authorization_state: str = "NOT_REQUIRED"
    budget_state: str = "NOT_REQUIRED"
    fallback_reason_codes: tuple[str, ...] = ("NO_ACTIVE_RETRIEVAL_PROFILE",)
    report_model_capability_blockers: bool = False


class RetrievalService:
    """不依赖 API、SQLite 或 Qdrant 类型的 P07 application service。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        source: EvidenceSourcePort,
        exact_store: ExactStorePort,
        lexical_store: LexicalStorePort,
        vector_store: VectorStorePort,
        query_embedding: QueryEmbeddingPort,
        reranker: RerankerPort,
        generator: GeneratorPort,
        trace: TracePort,
        cache: RetrievalCachePort,
        serving_fingerprint: str,
        egress_policy: EgressPolicy,
        policy: RetrievalPolicy | None = None,
        expected_index_fingerprint: str | None = None,
        expected_profile_revision_id: str | None = None,
    ) -> None:
        self._source = source
        self._expected_index_fingerprint = expected_index_fingerprint
        self._expected_profile_revision_id = expected_profile_revision_id
        self._exact = ExactChannel(exact_store)
        self._lexical = LexicalChannel(lexical_store)
        self._structural = StructuralChannel(lexical_store)
        self._dense = DenseChannel(query_embedding, vector_store)
        self._reranker = CircuitAwareReranker(reranker)
        reranker_descriptor = reranker.descriptor
        self._default_generator_descriptor = generator.descriptor
        self._grounded: GroundedAnsweringService | None = None
        self._interpreter: QueryInterpretPort | None = None
        self._adaptive_planner: AdaptivePlannerPort | None = None
        self._rewriter: QueryRewritePort | None = None
        self._generation_behavior = "model_required"
        self._trace = trace
        self._cache = cache
        self._policy = policy or RetrievalPolicy()
        # 检索实现演进仅改变 serving/query cache；文档索引与向量语义不变。
        self._serving_fingerprint = canonical_sha256(
            {
                "configured_serving": serving_fingerprint,
                "retrieval_implementation": (
                    "wb08r-01-adaptive-catalog-v1"
                    if self._policy.evidence_group_mode == "off"
                    and self._policy.contextual_rerank_mode == "off"
                    else "wb08r-02-post-rerank-groups-v1"
                ),
                "query_plan_schema_revision": QUERY_PLAN_SCHEMA_REVISION,
                "query_unit_fusion_revision": QUERY_UNIT_FUSION_REVISION,
                "atom_group_alignment_revision": ATOM_GROUP_ALIGNMENT_REVISION,
                "evidence_group_schema_revision": (
                    EVIDENCE_GROUP_SCHEMA_REVISION
                ),
                "grounded_claim_schema_revision": (
                    GROUNDED_CLAIM_SCHEMA_REVISION
                ),
                "generation_evidence_pack_revision": (
                    GENERATION_EVIDENCE_PACK_REVISION
                ),
                "answer_pipeline_revision": "wb08r-evidence-first-v3",
                "natural_renderer_revision": NATURAL_RENDERER_REVISION,
                "corrective_retrieval_revision": CORRECTIVE_RETRIEVAL_REVISION,
            }
        )
        self._egress = egress_policy
        self._analyzer = QueryAnalyzer()
        self._expander = RuleBasedNormalizer()
        self._planner = QueryPlanner()
        self._hydrator = CandidateHydrator(source)
        self._neighbors = NeighborExpander(source)
        self._evidence = EvidenceAssembler()
        self._confidence = ConfidenceEvaluator()
        self._data_plane_context = QueryDataPlaneContext(
            reranker_provider_id=reranker_descriptor.name,
            reranker_model=reranker_descriptor.version,
        )

    def with_data_plane(
        self, context: QueryDataPlaneContext
    ) -> RetrievalService:
        """为单次请求附加真实配置、授权与降级状态。

        Args:
            context: Product 组合根从当前持久状态解析的非敏感上下文。

        Returns:
            共用检索资源、但不会污染其它请求状态的轻量副本。

        """
        configured = copy(self)
        configured._data_plane_context = context
        return configured

    @property
    def data_plane_context(self) -> QueryDataPlaneContext:
        """返回当前服务的非敏感数据面配置快照。

        Args:
            无参数；读取当前服务已经绑定的数据面上下文。

        Returns:
            可由组合根按单次请求派生的冻结上下文。

        """
        return self._data_plane_context

    def with_generation(  # noqa: PLR0913
        self,
        generator: GeneratorPort,
        *,
        serving_identity: str,
        interpreter: QueryInterpretPort | None = None,
        adaptive_planner: AdaptivePlannerPort | None = None,
        rewriter: QueryRewritePort | None = None,
        critical_ocr_verifier: CriticalOcrVerifierPort | None = None,
    ) -> RetrievalService:
        """为单次知识库解析创建轻量配置副本，共用原索引与检索通道。

        Args:
            generator: 已绑定知识库与出站授权的生成器。
            serving_identity: 模型及查询策略缓存身份。
            interpreter: 可选的一次结构化问题解释端口。
            adaptive_planner: 可选的一次轻量语义规划端口。
            rewriter: 可选的一次问题改写端口。
            critical_ocr_verifier: PDF 高风险事实的有界 PP-OCRv6 复核端口。

        Returns:
            与原文档向量配置共存的查询服务。

        """
        configured = self.with_generation_cache_identity(
            serving_identity=serving_identity
        )
        configured._grounded = GroundedAnsweringService(
            generator,
            critical_ocr_verifier=critical_ocr_verifier,
        )
        configured._interpreter = interpreter
        configured._adaptive_planner = adaptive_planner
        configured._rewriter = rewriter
        descriptor = getattr(
            generator, "descriptor", self._default_generator_descriptor
        )
        configured._data_plane_context = replace(
            self._data_plane_context,
            generation_provider_id=descriptor.name,
            generation_model=descriptor.version,
            interpret_provider_id=(
                descriptor.name
                if interpreter is not None or adaptive_planner is not None
                else None
            ),
            interpret_model=(
                descriptor.version
                if interpreter is not None or adaptive_planner is not None
                else None
            ),
            rewrite_provider_id=(
                descriptor.name if rewriter is not None else None
            ),
            rewrite_model=descriptor.version if rewriter is not None else None,
            model_configuration_state="CONFIGURED",
        )
        return configured

    def with_generation_cache_identity(
        self,
        *,
        serving_identity: str,
    ) -> RetrievalService:
        """仅附加已批准生成配置的缓存身份，不挂载可出网模型。

        预算耗尽不会改变已经发布答案的模型、Prompt 或授权身份。此副本
        因而可以在任何 Provider 调用前读取同身份缓存；缓存未命中时仍由
        数据面阻断原因拒答，且不存在可调用的生成器。

        Args:
            serving_identity: 模型、Prompt、连接、凭据与活动授权的稳定身份。

        Returns:
            使用 grounded 缓存键、但不能发起生成请求的轻量副本。

        """
        configured = copy(self)
        configured._grounded = None
        configured._interpreter = None
        configured._adaptive_planner = None
        configured._rewriter = None
        configured._generation_behavior = "grounded"
        configured._serving_fingerprint = canonical_sha256(
            {
                "retrieval": self._serving_fingerprint,
                "generation": serving_identity,
            }
        )
        return configured

    def execution_identity(
        self, request: SearchRequest
    ) -> RetrievalExecutionIdentity:
        """在 Provider 调用前冻结 singleflight/cache 的完整身份。

        Args:
            request: 已绑定 owner、scope、过滤、会话与回答行为的请求。

        Returns:
            当前活动 Revision 与规范计算键；不包含问题或上下文正文。

        """
        snapshot = self._query_snapshot(request)
        analysis = self._analyzer.analyze(request)
        variants = self._expander.expand(analysis)
        plan = self._planner.plan(
            analysis,
            variants,
            self._policy,
            dense_required=request.dense_required,
        )
        identity = self._cache_identity(request, snapshot, analysis, plan)
        return RetrievalExecutionIdentity(
            key_hash=identity.persistent_key,
            active_revision_id=snapshot.revision.index_revision_id,
            serving_fingerprint=snapshot.serving_fingerprint,
        )

    def search_and_answer(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        request: SearchRequest,
        *,
        on_stage: Callable[[str, dict[str, object]], None] | None = None,
        on_claim: Callable[[AnswerClaim, str], None] | None = None,
        on_final: Callable[[SearchAnswerResult], None] | None = None,
        cancellation: CancellationPort | None = None,
        cache_result: bool = True,
    ) -> SearchAnswerResult:
        """执行一次 revision-sticky、bounded、fail-closed 查询。

        Args:
            request: scope、query、过滤和有限 conversation context。
            on_stage: 可选的无正文阶段事件回调。
            on_claim: 可选的已校验 claim 与快照版本回调。
            on_final: 可选的唯一权威终态发布回调。
            cancellation: 可选协作取消端口。
            cache_result: 是否立即写入最终结果缓存；流式路径延后到 final
                已交付后。

        Returns:
            实际 route/rerank、证据、置信和模型回答或明确拒答。

        Raises:
            IndexNotReady: 没有 Active Revision。
            IndexCorrupt: 通道身份无法 canonical hydrate。
            PolicyDenied: 未支持 filter 或 profile 要求 fail closed。

        """
        trace_id = request.trace_id or f"trace_{uuid.uuid4().hex}"
        stage_started = perf_counter()
        stage_timings: list[StageTiming] = []
        provider_calls: list[ProviderCall] = []
        _raise_if_cancelled(cancellation, provider_calls)
        snapshot = self._query_snapshot(request)
        self._record(
            trace_id,
            "snapshot",
            {
                "revision_id": snapshot.revision.index_revision_id,
                "index_fingerprint": snapshot.revision.index_fingerprint,
                "serving_fingerprint": snapshot.serving_fingerprint,
            },
        )
        _emit_stage(
            on_stage,
            "snapshot",
            {
                "active_index_revision_id": (
                    snapshot.revision.index_revision_id
                ),
                "index_fingerprint": snapshot.revision.index_fingerprint,
                "serving_fingerprint": snapshot.serving_fingerprint,
            },
            provider_calls,
        )
        stage_started = _finish_timing(stage_timings, "snapshot", stage_started)
        _raise_if_cancelled(cancellation, provider_calls)
        analysis = self._analyzer.analyze(request)
        input_spans = build_input_spans(request)
        resolved_root = resolve_root_query(request, input_spans)
        effort = reasoning_effort(
            analysis, has_context=bool(request.conversation_context)
        )
        effective_analysis = (
            self._analyzer.resolve(
                analysis, request, resolved_root.resolved_query
            )
            if resolved_root.mode == "RULE_CONTEXT"
            else analysis
        )
        self._record(
            trace_id,
            "context_resolution",
            {
                "context_resolution_mode": resolved_root.mode,
                "context_resolution_confidence": resolved_root.confidence,
                "context_resolution_revision": (
                    resolved_root.resolution_revision
                ),
                "original_query_sha256": hashlib.sha256(
                    request.text.encode()
                ).hexdigest(),
                "resolved_root_query_sha256": hashlib.sha256(
                    resolved_root.resolved_query.encode()
                ).hexdigest(),
                "context_digest": resolved_root.context_digest,
                "referenced_turn_count": len(
                    {
                        span.turn
                        for span in input_spans
                        if span.span_id in resolved_root.referenced_span_ids
                        and span.turn != "CURRENT"
                    }
                ),
            },
        )
        self._record(
            trace_id,
            "analyze",
            {
                "query_sha256": hashlib.sha256(
                    request.text.encode("utf-8")
                ).hexdigest(),
                "query_length": len(request.text),
                "identifier_count": len(analysis.identifiers),
                "answer_type": analysis.semantics.answer_type.value,
                "semantic_source": analysis.semantics.source,
                "expected_count": analysis.semantics.expected_count,
                "ordinal": analysis.semantics.ordinal,
                "constraint_kinds": tuple(
                    item.kind.value for item in analysis.semantics.constraints
                ),
                "reason_codes": analysis.reason_codes,
                "reasoning_effort": effort.value,
            },
        )
        stage_started = _finish_timing(stage_timings, "analyze", stage_started)
        variants = self._expander.expand(effective_analysis)
        self._record(
            trace_id,
            "expand",
            {
                "variant_count": len(variants),
                "variant_kinds": [variant.kind for variant in variants],
            },
        )
        plan = self._planner.plan(
            effective_analysis,
            variants,
            self._policy,
            dense_required=request.dense_required,
        )
        self._record(
            trace_id,
            "plan",
            {
                "query_kind": plan.query_kind.value,
                "channels": plan.channels,
                "reason_codes": plan.reason_codes,
            },
        )
        stage_started = _finish_timing(stage_timings, "plan", stage_started)
        cache_identity = self._cache_identity(request, snapshot, analysis, plan)
        cache_key = cache_identity.persistent_key
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._validate_cached_sources(cached, request, snapshot)
            self._record(trace_id, "cache", {"result": "hit"})
            self._record(trace_id, "complete", {"status": cached.status.value})
            _finish_timing(stage_timings, "cache", stage_started)
            diagnostics = RetrievalDiagnostics(
                cache_hit=True,
                stage_timings=tuple(stage_timings),
            )
            _emit_stage(
                on_stage,
                "retrieval",
                {"cache_hit": True},
                provider_calls,
            )
            cached_result = cached.model_copy(
                update={
                    "trace_id": trace_id,
                    "cache_hit": True,
                    "result_origin": "cache",
                    "generation_called_this_request": False,
                    "interpret_called_this_request": False,
                    "rewrite_called_this_request": False,
                    "diagnostics": diagnostics,
                    "diagnostics_summary": _diagnostics_summary(diagnostics),
                    "data_plane": self._query_data_plane(
                        snapshot,
                        selected_slot=cached.selected_embedding_slot,
                        rerank_mode=cached.rerank_execution_mode,
                        degraded=cached.degraded_reason_codes,
                    ),
                }
            )
            self._record_shortcut_trace(trace_id, cached_result, "CACHE_REPLAY")
            if on_final is not None:
                _raise_if_cancelled(cancellation, provider_calls)
                _validate_stream_final_sources(
                    self,
                    cached_result.evidence,
                    request,
                    snapshot,
                    provider_calls,
                )
                _raise_if_cancelled(cancellation, provider_calls)
                _emit_final(on_final, cached_result, provider_calls)
            return cached_result
        self._record(trace_id, "cache", {"result": "miss"})
        stage_started = _finish_timing(stage_timings, "cache", stage_started)
        if is_navigation_query(analysis.normalized_query):
            catalog_result = self._catalog_fast_path(
                request=request,
                snapshot=snapshot,
                analysis=analysis,
                plan=plan,
                cache_key=cache_key,
                trace_id=trace_id,
                stage_timings=stage_timings,
                on_stage=on_stage,
                on_final=on_final,
                cancellation=cancellation,
            )
            if catalog_result is not None:
                return catalog_result
        adaptive = AdaptivePlanOutcome()
        adaptive_attempted = False
        adaptive_reason = (
            "ADAPTIVE_PLAN_NOT_NEEDED"
            if self._adaptive_planner is not None
            else "ADAPTIVE_PLAN_NOT_CONFIGURED"
        )
        if (
            self._adaptive_planner is not None
            and effort is not ReasoningEffort.DIRECT
        ):
            _raise_if_cancelled(cancellation, provider_calls)
            adaptive = self._adaptive_planner.plan_adaptive(
                request, analysis, effort
            )
            adaptive_attempted = adaptive.attempted
            adaptive_reason = adaptive.reason_code
            provider_calls.extend(adaptive.calls)
            if adaptive.standalone_query and not adaptive.needs_clarification:
                effective_analysis = self._analyzer.resolve(
                    analysis, request, adaptive.standalone_query
                )
                adaptive_variant = QueryVariant(
                    text=adaptive.standalone_query,
                    kind="rewrite",
                    identity=canonical_sha256(
                        {
                            "query": adaptive.standalone_query,
                            "policy": "adaptive-plan-v1",
                        }
                    ),
                )
                plan = self._planner.plan(
                    effective_analysis,
                    (
                        variants
                        if adaptive.standalone_query
                        in {variant.text for variant in variants}
                        else (variants[0], adaptive_variant)
                    ),
                    self._policy,
                    dense_required=request.dense_required,
                )
            self._record(
                trace_id,
                "interpret",
                {
                    "reason_code": adaptive_reason,
                    "schema_fallback_detail": (adaptive.schema_fallback_detail),
                    "structured_output_mode": (adaptive.structured_output_mode),
                    "schema_revision": adaptive.schema_revision,
                    "schema_sha256": adaptive.schema_sha256,
                    "attempted": adaptive_attempted,
                    "accepted": adaptive.standalone_query is not None,
                    "reasoning_effort": effort.value,
                    "intent": adaptive.intent,
                    "atom_count": len(adaptive.atoms),
                    "needs_clarification": adaptive.needs_clarification,
                    "planner_called": adaptive.attempted,
                    "planner_protocol": adaptive.structured_output_mode,
                    "planner_schema_revision": adaptive.schema_revision,
                    "planner_transport_timeout_ms": (
                        adaptive.planner_transport_timeout_ms
                    ),
                    "planner_latency_ms": adaptive.planner_latency_ms,
                    "planner_input_tokens": adaptive.planner_input_tokens,
                    "planner_output_tokens": adaptive.planner_output_tokens,
                    "planner_finish_reason": adaptive.planner_finish_reason,
                    "planner_failure_category": adaptive.failure_category,
                },
            )
        interpret_attempted = False
        interpret_reason = "INTERPRET_NOT_CONFIGURED"
        if self._interpreter is not None and self._adaptive_planner is None:
            _raise_if_cancelled(cancellation, provider_calls)
            interpreted = self._interpreter.interpret(request, analysis)
            interpret_attempted = interpreted.attempted
            interpret_reason = interpreted.reason_code
            provider_calls.extend(interpreted.calls)
            if (
                interpreted.standalone_query is not None
                and interpreted.semantics is not None
            ):
                effective_analysis = self._analyzer.apply_interpretation(
                    analysis,
                    request,
                    interpreted.standalone_query,
                    interpreted.semantics,
                )
                interpreted_variant = QueryVariant(
                    text=interpreted.standalone_query,
                    kind="rewrite",
                    identity=canonical_sha256(
                        {
                            "query": interpreted.standalone_query,
                            "policy": "bounded-interpret-v2",
                        }
                    ),
                )
                interpreted_variants = (
                    variants
                    if interpreted.standalone_query
                    in {variant.text for variant in variants}
                    else (variants[0], interpreted_variant)
                )
                plan = self._planner.plan(
                    effective_analysis,
                    interpreted_variants,
                    self._policy,
                    dense_required=request.dense_required,
                )
            self._record(
                trace_id,
                "interpret",
                {
                    "reason_code": interpreted.reason_code,
                    "attempted": interpreted.attempted,
                    "accepted": interpreted.semantics is not None,
                    "resolved_query_sha256": hashlib.sha256(
                        (effective_analysis.resolved_query or "").encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    "resolved_answer_type": (
                        effective_analysis.semantics.answer_type.value
                    ),
                    "semantic_source": effective_analysis.semantics.source,
                },
            )
        rewrite_attempted = False
        rewrite_reason = "REWRITE_NOT_CONFIGURED"
        if (
            self._rewriter is not None
            and self._adaptive_planner is None
            and not interpret_attempted
        ):
            _raise_if_cancelled(cancellation, provider_calls)
            rewritten = self._rewriter.rewrite(request)
            rewrite_reason = rewritten.reason_code
            rewrite_attempted = rewritten.attempted
            provider_calls.extend(rewritten.calls)
            if rewritten.variant is not None:
                effective_analysis = self._analyzer.resolve(
                    analysis, request, rewritten.variant.text
                )
                plan = self._planner.plan(
                    effective_analysis,
                    (plan.variants[0], rewritten.variant),
                    self._policy,
                    dense_required=request.dense_required,
                )
            self._record(
                trace_id,
                "rewrite",
                {
                    "reason_code": rewritten.reason_code,
                    "attempted": rewritten.attempted,
                    "accepted": rewritten.variant is not None,
                    "resolved_query_sha256": hashlib.sha256(
                        (effective_analysis.resolved_query or "").encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    "resolved_answer_type": (
                        effective_analysis.semantics.answer_type.value
                    ),
                },
            )
        if adaptive.atoms and adaptive.standalone_query:
            try:
                query_plan = make_query_plan(
                    standalone_query=resolved_root.resolved_query,
                    original_query=request.text,
                    context_resolution_mode=resolved_root.mode,
                    context_digest=resolved_root.context_digest,
                    referenced_span_ids=resolved_root.referenced_span_ids,
                    intent=adaptive.intent or "FACT",
                    effort=effort.value,
                    atoms=adaptive.atoms,
                    reason_code=adaptive.reason_code,
                    planner_called=adaptive.attempted,
                    needs_clarification=adaptive.needs_clarification,
                    clarification_question=adaptive.clarification_question,
                    route_hints=adaptive.route_hints,
                )
            except ValueError:
                query_plan = degraded_query_plan(
                    request,
                    effective_analysis,
                    input_spans,
                    resolved_root,
                    effort=effort.value,
                    reason_code="PLANNER_INTERNAL_VALIDATION",
                    planner_called=adaptive.attempted,
                )
        else:
            query_plan = (
                degraded_query_plan(
                    request,
                    effective_analysis,
                    input_spans,
                    resolved_root,
                    effort=effort.value,
                    reason_code=adaptive_reason,
                    planner_called=adaptive.attempted,
                )
                if adaptive.attempted or resolved_root.mode == "CLARIFY"
                else fallback_query_plan(
                    effective_analysis,
                    effort=effort.value,
                    reason_code=adaptive_reason,
                    planner_called=False,
                    original_query=request.text,
                    resolved_root_query=resolved_root.resolved_query,
                    context_resolution_mode=resolved_root.mode,
                    context_digest=resolved_root.context_digest,
                    referenced_span_ids=resolved_root.referenced_span_ids,
                )
            )
        atom_mode = len(query_plan.atoms) > 1
        self._record(
            trace_id,
            "query_plan",
            {
                "plan_id": query_plan.plan_id,
                "atom_count": len(query_plan.atoms),
                "planner_called": query_plan.planner_called,
                "planner_protocol": adaptive.structured_output_mode
                if adaptive.attempted
                else "NONE",
                "planner_schema_revision": QUERY_PLAN_SCHEMA_REVISION,
                "planner_transport_timeout_ms": (
                    adaptive.planner_transport_timeout_ms
                    if adaptive.attempted
                    else 0
                ),
                "planner_latency_ms": adaptive.planner_latency_ms,
                "planner_input_tokens": adaptive.planner_input_tokens
                if adaptive.attempted
                else 0,
                "planner_output_tokens": adaptive.planner_output_tokens
                if adaptive.attempted
                else 0,
                "planner_finish_reason": adaptive.planner_finish_reason,
                "planner_failure_category": adaptive.failure_category,
                "planner_fallback_mode": query_plan.fallback_mode,
                "reason_code": query_plan.planner_reason_code,
                "fallback_mode": query_plan.fallback_mode,
                "coverage_confidence": query_plan.coverage_confidence,
                "resolved_root_query_sha256": hashlib.sha256(
                    query_plan.resolved_root_query.encode()
                ).hexdigest(),
            },
        )
        top_k = dict(plan.channel_top_k)
        channel_hits: dict[str, tuple[ChannelHit, ...]] = {}
        degraded: list[str] = []
        if "exact" in plan.channels and not atom_mode:
            _raise_if_cancelled(cancellation, provider_calls)
            channel_started = perf_counter()
            try:
                hits = apply_candidate_filters(
                    self._exact.search(
                        snapshot, effective_analysis, limit=top_k["exact"]
                    ),
                    request,
                )
            except (ChannelRateLimited, ChannelUnavailable) as error:
                degraded.append(error.code)
                hits = ()
            channel_hits["exact"] = hits
            self._record(trace_id, "exact", {"hit_count": len(hits)})
            _finish_timing(stage_timings, "exact_channel", channel_started)
        if "structural" in plan.channels and not atom_mode:
            _raise_if_cancelled(cancellation, provider_calls)
            channel_started = perf_counter()
            try:
                hits = apply_candidate_filters(
                    self._structural.search(
                        snapshot,
                        effective_analysis,
                        limit=top_k["structural"],
                    ),
                    request,
                )
            except (ChannelRateLimited, ChannelUnavailable) as error:
                degraded.append(error.code)
                hits = ()
            channel_hits["structural"] = hits
            self._record(
                trace_id,
                "structural",
                {
                    "hit_count": len(hits),
                    "match_types": tuple(
                        hit.match_type for hit in hits if hit.match_type
                    ),
                },
            )
            _finish_timing(stage_timings, "structural_channel", channel_started)
        if "lexical" in plan.channels and not atom_mode:
            _raise_if_cancelled(cancellation, provider_calls)
            channel_started = perf_counter()
            for variant in plan.variants:
                try:
                    hits = apply_candidate_filters(
                        self._lexical.search(
                            snapshot,
                            variant,
                            limit=top_k["lexical"],
                            analysis=effective_analysis,
                        ),
                        request,
                    )
                except (ChannelRateLimited, ChannelUnavailable) as error:
                    degraded.append(error.code)
                    hits = ()
                name = (
                    "lexical"
                    if variant.kind == "original"
                    else f"lexical:{variant.kind}"
                )
                channel_hits[name] = hits
            self._record(
                trace_id,
                "lexical",
                {
                    "hit_count": sum(
                        len(items)
                        for name, items in channel_hits.items()
                        if name.startswith("lexical")
                    )
                },
            )
            _finish_timing(stage_timings, "lexical_channel", channel_started)
        selected_slot: str | None = None
        selected_vector: str | None = None
        route_reason = "DENSE_DISABLED_BY_PLAN"
        if "dense" in plan.channels and not atom_mode:
            _raise_if_cancelled(cancellation, provider_calls)
            channel_started = perf_counter()
            route_attributes: dict[str, object] = {
                "selected_slot": None,
                "vector_name": None,
                "reason_code": route_reason,
                "attempted_slots": (),
                "circuit_before": (),
                "circuit_after": (),
            }
            try:
                dense = self._dense.search(
                    snapshot,
                    effective_analysis.resolved_query
                    or effective_analysis.normalized_query,
                    self._egress,
                    limit=top_k["dense"],
                )
            except (DenseUnavailable, PolicyDenied) as error:
                if plan.dense_required:
                    raise
                degraded.append(error.code)
                route_reason = error.code
            except IndexCompatibilityError as error:
                raise IndexCorrupt(
                    "Dense route 与 Active Revision 不兼容。",
                    stage="retrieval.dense",
                ) from error
            else:
                provider_calls.extend(dense.routed.provider_calls)
                selected_slot = dense.routed.selected_slot_id
                selected_vector = dense.routed.vector_name
                route_reason = dense.routed.fallback_reason
                filtered = apply_candidate_filters(dense.hits, request)
                channel_hits[f"dense:{selected_slot}"] = filtered
                route_attributes.update(
                    {
                        "attempted_slots": dense.routed.attempted_slot_ids,
                        "circuit_before": _circuit_trace(
                            dense.routed.circuit_before
                        ),
                        "circuit_after": _circuit_trace(
                            dense.routed.circuit_after
                        ),
                    }
                )
            route_attributes.update(
                {
                    "selected_slot": selected_slot,
                    "vector_name": selected_vector,
                    "reason_code": route_reason,
                }
            )
            self._record(
                trace_id,
                "query_embedding_route",
                route_attributes,
            )
            self._record(
                trace_id,
                "dense",
                {
                    "hit_count": sum(
                        len(items)
                        for name, items in channel_hits.items()
                        if name.startswith("dense:")
                    )
                },
            )
            _finish_timing(stage_timings, "vector_channel", channel_started)
        atom_links: tuple[AtomCandidateLink, ...] = ()
        unit_fused: tuple[FusedCandidate, ...] | None = None
        unit_seed_ids: tuple[str, ...] = ()
        rerank_query: str | None = None
        if atom_mode:
            atom_retrieval = self._retrieve_atoms(
                request=request,
                snapshot=snapshot,
                root_analysis=effective_analysis,
                plan=plan,
                query_plan=query_plan,
                provider_calls=provider_calls,
                degraded=degraded,
                trace_id=trace_id,
                stage_timings=stage_timings,
            )
            channel_hits = atom_retrieval.channels
            atom_links = atom_retrieval.links
            selected_slot = atom_retrieval.selected_slot
            selected_vector = atom_retrieval.selected_vector
            route_reason = atom_retrieval.route_reason
            unit_fused = atom_retrieval.fused
            unit_seed_ids = atom_retrieval.seed_chunk_ids
            rerank_query = atom_retrieval.rerank_query
        embedding_calls = tuple(
            call
            for call in provider_calls
            if call.operation == "embedding.query"
        )
        self._record(
            trace_id,
            "embedding_accounting",
            {
                "query_embedding_provider_call_count": sum(
                    call.call_count for call in embedding_calls
                ),
                "query_embedding_batch_size": (
                    len(
                        {
                            query_plan.resolved_root_query,
                            *(atom.search_text for atom in query_plan.atoms),
                        }
                    )
                    if atom_mode and "dense" in plan.channels
                    else 1
                    if "dense" in plan.channels
                    else 0
                ),
                "query_embedding_slot_id": selected_slot,
                "query_embedding_latency_ms": sum(
                    call.elapsed_ms for call in embedding_calls
                ),
            },
        )
        selection = self._rank_and_select(
            request=request,
            snapshot=snapshot,
            analysis=effective_analysis,
            plan=plan,
            channel_hits=channel_hits,
            selected_slot=selected_slot,
            trace_id=trace_id,
            provider_calls=provider_calls,
            degraded=degraded,
            stage_timings=stage_timings,
            retrieval_phase="original",
            atom_required_ids=unit_seed_ids,
            pre_fused=unit_fused,
            rerank_query=rerank_query,
        )
        fused = selection.fused
        reranked = selection.reranked
        if atom_links:
            rerank_ranks = {
                item.hydrated.chunk.chunk_id: index
                for index, item in enumerate(reranked.candidates, 1)
            }
            atom_links = tuple(
                link.model_copy(
                    update={"rerank_rank": rerank_ranks.get(link.chunk_id)}
                )
                for link in atom_links
            )
        expansion = selection.expansion
        evidence = selection.evidence
        model_evidence_candidates = selection.model_evidence_candidates
        evidence_decisions = selection.evidence_decisions
        confidence = selection.confidence
        _raise_if_cancelled(cancellation, provider_calls)
        if (
            self._rewriter is not None
            and self._adaptive_planner is None
            and not rewrite_attempted
            and confidence.status
            in {
                ConfidenceStatus.INSUFFICIENT_EVIDENCE,
                ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION,
            }
            and not any("POLICY_DENIED" in reason for reason in degraded)
        ):
            _raise_if_cancelled(cancellation, provider_calls)
            rewritten = self._rewriter.rewrite(
                request, recall_insufficient=True
            )
            rewrite_reason = rewritten.reason_code
            rewrite_attempted = rewritten.attempted
            provider_calls.extend(rewritten.calls)
            if rewritten.variant is not None:
                effective_analysis = self._analyzer.resolve(
                    analysis, request, rewritten.variant.text
                )
                plan = self._planner.plan(
                    effective_analysis,
                    (plan.variants[0], rewritten.variant),
                    self._policy,
                    dense_required=request.dense_required,
                )
                top_k = dict(plan.channel_top_k)
                if "structural" in plan.channels:
                    _raise_if_cancelled(cancellation, provider_calls)
                    try:
                        structural_hits = apply_candidate_filters(
                            self._structural.search(
                                snapshot,
                                effective_analysis,
                                limit=top_k["structural"],
                            ),
                            request,
                        )
                    except (
                        ChannelRateLimited,
                        ChannelUnavailable,
                    ) as error:
                        degraded.append(error.code)
                        structural_hits = ()
                    channel_hits["structural:rewrite"] = structural_hits
                if "lexical" in plan.channels:
                    _raise_if_cancelled(cancellation, provider_calls)
                    try:
                        rewrite_hits = apply_candidate_filters(
                            self._lexical.search(
                                snapshot,
                                rewritten.variant,
                                limit=top_k["lexical"],
                                analysis=effective_analysis,
                            ),
                            request,
                        )
                    except (
                        ChannelRateLimited,
                        ChannelUnavailable,
                    ) as error:
                        degraded.append(error.code)
                        rewrite_hits = ()
                    channel_hits["lexical:rewrite"] = rewrite_hits
                if "dense" in plan.channels:
                    _raise_if_cancelled(cancellation, provider_calls)
                    try:
                        rewrite_dense = self._dense.search(
                            snapshot,
                            effective_analysis.resolved_query
                            or effective_analysis.normalized_query,
                            self._egress,
                            limit=top_k["dense"],
                        )
                    except (DenseUnavailable, PolicyDenied) as error:
                        if plan.dense_required:
                            raise
                        degraded.append(error.code)
                    except IndexCompatibilityError as error:
                        raise IndexCorrupt(
                            "Dense route 与 Active Revision 不兼容。",
                            stage="retrieval.dense",
                        ) from error
                    else:
                        provider_calls.extend(
                            rewrite_dense.routed.provider_calls
                        )
                        rewrite_slot = rewrite_dense.routed.selected_slot_id
                        if (
                            selected_slot is not None
                            and rewrite_slot != selected_slot
                        ):
                            raise IndexCorrupt(
                                "单请求补召回禁止切换 Dense slot。",
                                stage="retrieval.dense",
                            )
                        selected_slot = rewrite_slot
                        selected_vector = rewrite_dense.routed.vector_name
                        channel_hits[f"dense:{rewrite_slot}:rewrite"] = (
                            apply_candidate_filters(rewrite_dense.hits, request)
                        )
                selection = self._rank_and_select(
                    request=request,
                    snapshot=snapshot,
                    analysis=effective_analysis,
                    plan=plan,
                    channel_hits=channel_hits,
                    selected_slot=selected_slot,
                    trace_id=trace_id,
                    provider_calls=provider_calls,
                    degraded=degraded,
                    stage_timings=stage_timings,
                    retrieval_phase="rewrite",
                )
                fused = selection.fused
                reranked = selection.reranked
                expansion = selection.expansion
                evidence = selection.evidence
                model_evidence_candidates = selection.model_evidence_candidates
                evidence_decisions = selection.evidence_decisions
                confidence = selection.confidence
            self._record(
                trace_id,
                "rewrite",
                {
                    "reason_code": rewritten.reason_code,
                    "attempted": rewritten.attempted,
                    "accepted": rewritten.variant is not None,
                    "trigger": "EVIDENCE_INSUFFICIENT",
                    "resolved_query_sha256": hashlib.sha256(
                        (effective_analysis.resolved_query or "").encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    "resolved_answer_type": (
                        effective_analysis.semantics.answer_type.value
                    ),
                },
            )
        (
            atom_matrix,
            atom_evidence,
            atom_coverage,
            atom_candidate_membership,
        ) = self._ground_atoms(
            request=request,
            query_plan=query_plan,
            candidates=selection.expansion.candidates,
            groups=selection.groups,
            links=atom_links,
            selected_slot=selected_slot,
            snapshot=snapshot,
            rerank_mode=reranked.mode,
            trace_id=trace_id,
        )
        correction_started = perf_counter()
        correction_triggered = any(
            item.status in {AtomStatus.PARTIAL, AtomStatus.MISSING}
            for item in atom_matrix.atoms
        )
        correction_outcome: PerAtomCorrectionOutcome | None = None
        generation_ranked_candidates = selection.expansion.candidates
        generation_groups = selection.groups
        if correction_triggered:
            correction_outcome = self._corrective_retrieval(
                snapshot=snapshot,
                candidates=selection.expansion.candidates,
                groups=selection.groups,
                links=atom_links,
                matrix=atom_matrix,
                query_plan=query_plan,
            )
            if correction_outcome.added_chunk_count:
                generation_ranked_candidates = correction_outcome.candidates
                generation_groups = correction_outcome.groups
                (
                    atom_matrix,
                    atom_evidence,
                    atom_coverage,
                    atom_candidate_membership,
                ) = self._ground_atoms(
                    request=request,
                    query_plan=query_plan,
                    candidates=correction_outcome.candidates,
                    groups=correction_outcome.groups,
                    links=atom_links,
                    selected_slot=selected_slot,
                    snapshot=snapshot,
                    rerank_mode=reranked.mode,
                    trace_id=trace_id,
                )
        correction_elapsed_ms = (perf_counter() - correction_started) * 1000
        source_closure = self._neighbors.close_source_nodes(
            snapshot, generation_ranked_candidates, self._policy
        )
        generation_ranked_candidates = source_closure.candidates
        degraded.extend(source_closure.degraded_reason_codes)
        self._record(
            trace_id,
            "source_node_closure",
            {
                "added_chunk_ids": tuple(
                    item.hydrated.chunk.chunk_id
                    for item in generation_ranked_candidates
                    if item.expansion_reason == "SOURCE_NODE_CONTINUATION"
                ),
                "reason_codes": source_closure.degraded_reason_codes,
            },
        )
        _finish_timing(
            stage_timings, "corrective_retrieval", correction_started
        )
        correction_by_atom = (
            {item.atom_id: item for item in correction_outcome.atom_traces}
            if correction_outcome is not None
            else {}
        )
        for coverage in atom_coverage:
            self._record(
                trace_id,
                "atom_coverage",
                {
                    "atom_id": coverage.atom_id,
                    "source_hit": coverage.source_hit,
                    "reranker_hit": coverage.reranker_hit,
                    "candidate_group_ids": coverage.candidate_group_ids,
                    "status": coverage.status.value,
                    "reason_codes": coverage.reason_codes,
                },
            )
            self._record(
                trace_id,
                "corrective_retrieval",
                {
                    "atom_id": coverage.atom_id,
                    "correction_triggered": (
                        coverage.atom_id in correction_by_atom
                    ),
                    "correction_reason": (
                        correction_by_atom[coverage.atom_id].reason_code
                        if coverage.atom_id in correction_by_atom
                        else "NOT_NEEDED"
                    ),
                    "anchor_document_id": (
                        correction_by_atom[coverage.atom_id].anchor_document_id
                        if coverage.atom_id in correction_by_atom
                        else None
                    ),
                    "anchor_section_id": (
                        correction_by_atom[coverage.atom_id].anchor_section_id
                        if coverage.atom_id in correction_by_atom
                        else None
                    ),
                    "added_chunk_count": (
                        correction_by_atom[coverage.atom_id].added_chunk_count
                        if coverage.atom_id in correction_by_atom
                        else 0
                    ),
                    "added_group_count": (
                        correction_outcome.added_group_count
                        if correction_outcome is not None
                        else 0
                    ),
                    "post_status": coverage.status.value,
                    "elapsed_ms": round(correction_elapsed_ms, 3),
                },
            )
        generation_evidence_pack = build_generation_evidence_pack(
            query_plan=query_plan,
            root_evidence=selection.model_evidence_candidates,
            atom_evidence=atom_evidence,
            atom_candidates_by_atom=atom_candidate_membership,
            ranked_candidates=generation_ranked_candidates,
            groups=generation_groups,
            links=atom_links,
            request=request,
            active_revision_id=snapshot.revision.index_revision_id,
            excluded_document_ids=snapshot.excluded_document_ids,
            policy=self._policy,
        )
        self._record(
            trace_id,
            "generation_evidence",
            {
                "pack_revision": generation_evidence_pack.pack_revision,
                "rerank_candidate_count": len(reranked.candidates),
                "root_candidate_count": len(
                    selection.model_evidence_candidates
                ),
                "atom_candidate_count": len(atom_evidence),
                "generation_evidence_count": len(
                    generation_evidence_pack.entries
                ),
                **generation_evidence_pack.structural_sibling_observation(
                    generation_groups
                ),
                "admitted_sources": tuple(
                    {
                        "support_id": item.support_id,
                        "document_version_id": (
                            item.evidence_item.document_version_id
                        ),
                        "chunk_id": item.evidence_item.chunk_id,
                        "source_group_id": item.source_group_id,
                        "table_node_id": item.table_node_id,
                        "table_group_id": item.table_group_id,
                        "table_row_index": item.table_row_index,
                        "linked_atom_ids": tuple(
                            atom_id
                            for atom_id, support_ids in (
                                generation_evidence_pack.per_atom_candidate_support_ids
                            )
                            if item.support_id in support_ids
                        ),
                        "node_ids": tuple(
                            span.node_id
                            for span in item.evidence_item.source_spans
                            if span.node_id is not None
                        ),
                    }
                    for item in generation_evidence_pack.entries
                ),
                "hard_rejected_sources": (
                    generation_evidence_pack.hard_rejected_sources
                ),
                "per_atom_candidate_count": {
                    atom_id: len(support_ids)
                    for atom_id, support_ids in (
                        generation_evidence_pack.per_atom_candidate_support_ids
                    )
                },
                "pre_generation_availability_by_atom": (
                    generation_evidence_pack.pre_generation_availability(
                        atom_matrix
                    )
                ),
                "complete_group_ids": (
                    generation_evidence_pack.complete_group_ids
                ),
                "partial_group_ids": (
                    generation_evidence_pack.partial_group_ids
                ),
                "missing_atom_ids": generation_evidence_pack.missing_atom_ids,
                "hard_reject_reason_distribution": dict(
                    Counter(
                        reason.value
                        for item in generation_evidence_pack.rejected_entries
                        for reason in item.hard_reject_reasons
                    )
                ),
                "soft_signal_distribution": dict(
                    Counter(
                        signal.value
                        for item in generation_evidence_pack.entries
                        for signal in item.soft_signals
                    )
                ),
            },
        )
        if self._grounded is not None:
            model_evidence_candidates = generation_evidence_pack.evidence
            supported_ids = {
                support_id
                for item in atom_matrix.atoms
                if item.status is AtomStatus.SUPPORTED
                for support_id in item.supporting_support_ids
            }
            supported_sources = {
                (
                    item.document_version_id,
                    item.chunk_id,
                    item.citation_text,
                    tuple(span.node_id for span in item.source_spans),
                )
                for item in atom_evidence
                if item.support_id in supported_ids
            }
            evidence = tuple(
                item
                for item in generation_evidence_pack.evidence
                if (
                    item.document_version_id,
                    item.chunk_id,
                    item.citation_text,
                    tuple(span.node_id for span in item.source_spans),
                )
                in supported_sources
            )
        if query_plan.needs_clarification:
            confidence = confidence.model_copy(
                update={
                    "status": ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION,
                    "score": 0.0,
                    "reason_codes": (
                        *confidence.reason_codes,
                        "CONTEXT_UNRESOLVED",
                    ),
                }
            )
        _emit_stage(
            on_stage,
            "retrieval",
            {
                "cache_hit": False,
                "evidence_count": len(evidence),
                "status": confidence.status.value,
            },
            provider_calls,
        )
        _raise_if_cancelled(cancellation, provider_calls)
        stage_started = perf_counter()
        generation_mode = "none"
        generated: GroundedOutcome | None = None
        generation_reason: str | None = _generation_unavailable_reason(
            self._data_plane_context
        )
        generation_evidence = (
            model_evidence_candidates
            if self._grounded is not None
            else evidence
        )
        try:
            if self._grounded is not None:
                _emit_stage(
                    on_stage,
                    "generation",
                    {"mode": "configured"},
                    provider_calls,
                )

                def publish_claim(claim: AnswerClaim) -> None:
                    """在每个公开 claim 前重新核对当前来源可见性。

                    Args:
                        claim: 已通过生成层证据规则的完整事实。

                    Returns:
                        无返回值；来源有效时交给流式发布回调。

                    """
                    self._validate_stream_sources(
                        generation_evidence,
                        request,
                        snapshot,
                    )
                    if on_claim is not None:
                        on_claim(
                            claim,
                            snapshot.revision.index_revision_id,
                        )

                generated = self._grounded.answer(
                    effective_analysis.resolved_query
                    or effective_analysis.normalized_query,
                    generation_evidence,
                    confidence,
                    answer_support_set=evidence,
                    analysis=effective_analysis,
                    query_plan=query_plan,
                    atom_support_matrix=atom_matrix,
                    generation_evidence_pack=generation_evidence_pack,
                    on_claim=None if on_claim is None else publish_claim,
                    cancellation=cancellation,
                )
                answer = generated.answer
                generation_mode = generated.mode
                generation_reason = generated.reason_code
                provider_calls.extend(generated.calls)
                self._record(
                    trace_id,
                    "atom_grounding",
                    {
                        "atom_count": len(query_plan.atoms),
                        "atom_coverage": generated.atom_coverage,
                        "repair_calls": generated.repair_calls,
                        "claim_rejection_codes": (
                            generated.claim_rejection_codes
                        ),
                        "generation_calls": sum(
                            call.call_count
                            for call in generated.calls
                            if call.operation == "generation"
                        ),
                    },
                )
                if answer is not None:
                    published = _published_evidence(
                        generation_evidence,
                        generated.published_support_ids,
                        generated.ocr_verification_states,
                    )
                    if published:
                        evidence = published
                    confidence = confidence.model_copy(
                        update={"status": ConfidenceStatus.ANSWERABLE}
                    )
                elif generation_reason == "GENERATION_ABSTAINED":
                    confidence = confidence.model_copy(
                        update={
                            "status": ConfidenceStatus.INSUFFICIENT_EVIDENCE
                        }
                    )
                if (
                    generation_reason
                    and generation_reason != "CLAIMS_VALIDATED"
                ):
                    degraded.append(generation_reason)
            else:
                _emit_stage(
                    on_stage,
                    "generation",
                    {"mode": "model_required"},
                    provider_calls,
                )
                # 正常查询必须由模型实际读取本次候选。未挂载模型时明确
                # 拒答，不能调用确定性表格或摘录 renderer 代替生成。
                answer = None
                generation_reason = _generation_unavailable_reason(
                    self._data_plane_context
                )
                degraded.append(generation_reason)
        except QueryCancelled as error:
            error.provider_calls = (*provider_calls, *error.provider_calls)
            raise
        except StreamDeliveryError:
            raise
        except ValidationFailed as error:
            # 模型输出校验已在 GroundedAnsweringService 内完成修复；
            # 这里逸出的 ValidationFailed 代表最终发布合同损坏，不能
            # 伪装成远程 Provider 不可用。
            error.provider_calls = (*provider_calls, *error.provider_calls)
            raise
        except (RagError, ValueError) as error:
            degraded.append(f"GENERATOR_FAILURE:{type(error).__name__}")
            confidence = confidence.model_copy(
                update={
                    "status": ConfidenceStatus.PROVIDER_UNAVAILABLE,
                    "score": 0.0,
                    "reason_codes": (
                        *confidence.reason_codes,
                        "GENERATOR_FAILURE",
                    ),
                }
            )
            answer = None
        except Exception as error:
            # 仅记录代码位置和异常类型，避免在诊断轨迹中写入问题或原文。
            self._record(
                trace_id,
                "answer_unexpected_failure",
                {
                    "error_type": type(error).__name__,
                    "frames": tuple(
                        (frame.name, frame.lineno)
                        for frame in traceback.extract_tb(error.__traceback__)
                    ),
                },
            )
            raise
        if answer is None:
            blocked_status = _model_capability_status(
                self._data_plane_context,
                generation_reason,
            )
            if blocked_status is not None:
                status, blocked_reason = blocked_status
                confidence = confidence.model_copy(
                    update={
                        "status": status,
                        "score": 0.0,
                        "reason_codes": tuple(
                            dict.fromkeys(
                                (*confidence.reason_codes, blocked_reason)
                            )
                        ),
                    }
                )
                degraded.append(blocked_reason)
                generation_reason = blocked_reason
            elif (
                self._grounded is not None
                and confidence.status is ConfidenceStatus.ANSWERABLE
            ):
                # 直接证据只能作为模型输入；模型弃答或连续两次输出未通过
                # 逐字引用校验时，不能让规则把检索状态冒充成最终答案状态。
                failure_reason = generation_reason or "GENERATION_FAILED"
                confidence = confidence.model_copy(
                    update={
                        "status": ConfidenceStatus.INSUFFICIENT_EVIDENCE,
                        "score": 0.0,
                        "reason_codes": tuple(
                            dict.fromkeys(
                                (*confidence.reason_codes, failure_reason)
                            )
                        ),
                    }
                )
        published_ids = (
            generated.published_support_ids if generated is not None else ()
        )
        published_quotes = tuple(
            hashlib.sha256(item.citation_text.encode("utf-8")).hexdigest()
            for item in evidence
            if item.support_id in published_ids
        )
        generation_called = any(
            call.operation == "generation" and call.call_count > 0
            for call in provider_calls
        )
        answer_path = {
            "extractive": "DIRECT_EXTRACT",
            "extractive_fallback": "EXTRACTIVE_FALLBACK",
            "llm": "LLM_CLAIM_VALIDATED" if answer else "LLM_ABSTAINED",
        }.get(generation_mode, "NO_ANSWER")
        observed_coverage = dict(generated.atom_coverage) if generated else {}
        coverage_labels = {
            "SUPPORTED": "ANSWERED",
            "PARTIAL": "PARTIALLY_ANSWERED",
            "MISSING": "NOT_ANSWERED",
            "CONTRADICTORY": "CONFLICTING",
        }
        final_coverage_by_atom = {
            atom.atom_id: coverage_labels.get(
                observed_coverage.get(atom.atom_id), "NOT_OBSERVED"
            )
            for atom in query_plan.atoms
        }
        self._record(
            trace_id,
            "claim_publication",
            {
                "answer_path": answer_path,
                "generation_called": generation_called,
                "extractive_fallback_used": (
                    generation_mode == "extractive_fallback"
                ),
                "final_coverage_by_atom": final_coverage_by_atom,
                "generated_claim_count": generated.generated_claim_count
                if generated
                else 0,
                "accepted_claim_count": generated.accepted_claim_count
                if generated
                else 0,
                "published_claim_count": generated.published_claim_count
                if generated
                else 0,
                "accepted_support_ids": generated.accepted_support_ids
                if generated
                else (),
                "published_support_ids": published_ids,
                "published_quote_sha256s": published_quotes,
                "claim_rejection_code_distribution": dict(
                    generated.claim_rejection_codes
                )
                if generated
                else {},
                "generation_gap_count": generated.generation_gap_count
                if generated
                else 0,
                "final_atom_coverage": generated.atom_coverage
                if generated
                else tuple(
                    (item.atom_id, item.status.value)
                    for item in atom_matrix.atoms
                ),
                "false_limited_detected": generated.false_limited_detected
                if generated
                else False,
            },
        )
        if on_claim is not None:
            # 没有增量 claim 的拒答或 final-only 路径也必须在 final
            # 前重查删除/撤权，且仍坚持请求开始时冻结的 revision。
            try:
                self._validate_stream_sources(evidence, request, snapshot)
            except RagError as error:
                error.provider_calls = (*provider_calls, *error.provider_calls)
                raise
        _raise_if_cancelled(cancellation, provider_calls)
        _emit_stage(
            on_stage,
            "validation",
            {
                "published": answer is not None,
                "generation_mode": generation_mode,
            },
            provider_calls,
        )
        self._record(
            trace_id,
            "generate",
            {
                "mode": generation_mode,
                "answer_path": answer_path,
                "generation_called": generation_called,
                "extractive_fallback_used": (
                    generation_mode == "extractive_fallback"
                ),
                "final_coverage_by_atom": final_coverage_by_atom,
                "reason_code": generation_reason,
                "provider_call_count_by_operation": (
                    _provider_call_count_by_operation(provider_calls)
                ),
                "provider_call_count_observation_status": (
                    "CAPTURED_PROVIDER_CALLS"
                ),
                "provider_call_operation_aliases": {
                    "embedding.query": ("embedding", "embedding.query")
                },
                "provider_calls": [
                    call.model_dump(mode="json")
                    for call in provider_calls
                    if call.operation
                    in {
                        "generation",
                        "image.ocr.verify",
                        "query.interpret",
                        "query.rewrite",
                    }
                ],
            },
        )
        self._record(
            trace_id,
            "validate",
            {
                "published": answer is not None,
                "support_count": len(evidence),
                "model_evidence_candidate_count": len(
                    model_evidence_candidates
                ),
                "evidence_decisions": evidence_decisions,
            },
        )
        _finish_timing(stage_timings, "answer", stage_started)
        diagnostics = _diagnostics(
            channel_hits=channel_hits,
            fused=fused,
            reranked=reranked.candidates,
            expanded=expansion.candidates,
            evidence=evidence,
            model_evidence_candidates=model_evidence_candidates,
            evidence_decisions=evidence_decisions,
            answer_published=answer is not None,
            provider_calls=tuple(provider_calls),
            stage_timings=tuple(stage_timings),
            degraded=tuple(dict.fromkeys(degraded)),
        )
        related_contents: tuple[RelatedContent, ...] = ()
        display_message = None
        if query_plan.needs_clarification:
            display_message = query_plan.clarification_question
        if (
            request.include_related_content
            and answer is None
            and confidence.status
            in (
                ConfidenceStatus.INSUFFICIENT_EVIDENCE,
                ConfidenceStatus.CONFIGURATION_REQUIRED,
                ConfidenceStatus.BUDGET_BLOCKED,
                ConfidenceStatus.PROVIDER_UNAVAILABLE,
            )
            and not any("POLICY_DENIED" in reason for reason in degraded)
            and "policy_denied" not in reranked.mode
            and reranked.failure_category
            is not ProviderFailureCategory.POLICY_DENIED
        ):
            related_contents = select_related_contents(
                reranked.candidates,
                request,
                effective_analysis,
                revision_id=snapshot.revision.index_revision_id,
                rerank_mode=reranked.mode,
            )
            display_message = related_display_message(
                related_contents,
                reranked.mode,
                failure_category=reranked.failure_category,
            )
        result = SearchAnswerResult(
            trace_id=trace_id,
            status=confidence.status,
            reason_code=confidence.status.value,
            answer=answer,
            evidence=evidence,
            related_contents=related_contents,
            display_message=display_message,
            confidence=confidence,
            query_kind=plan.query_kind,
            requested_answer_type=effective_analysis.semantics.answer_type,
            query_semantic_source=effective_analysis.semantics.source,
            active_index_revision_id=snapshot.revision.index_revision_id,
            index_fingerprint=snapshot.revision.index_fingerprint,
            serving_fingerprint=snapshot.serving_fingerprint,
            selected_embedding_slot=selected_slot,
            selected_vector_name=selected_vector,
            route_reason_code=route_reason,
            rerank_execution_mode=reranked.mode,
            generation_mode=generation_mode,
            generation_reason_code=generation_reason,
            interpret_reason_code=(
                adaptive_reason
                if self._adaptive_planner is not None
                else interpret_reason
            ),
            reasoning_effort=effort.value,
            rewrite_reason_code=rewrite_reason,
            degraded_reason_codes=tuple(dict.fromkeys(degraded)),
            cache_key=cache_key,
            result_origin="fresh",
            generation_called_this_request=any(
                call.operation == "generation" and call.call_count > 0
                for call in provider_calls
            ),
            interpret_called_this_request=any(
                call.operation == "query.interpret" and call.call_count > 0
                for call in provider_calls
            ),
            rewrite_called_this_request=any(
                call.operation == "query.rewrite" and call.call_count > 0
                for call in provider_calls
            ),
            diagnostics_summary=_diagnostics_summary(diagnostics),
            data_plane=self._query_data_plane(
                snapshot,
                selected_slot=selected_slot,
                rerank_mode=reranked.mode,
                degraded=tuple(dict.fromkeys(degraded)),
            ),
            diagnostics=diagnostics,
        )
        if on_final is not None:
            # validation 阶段之后仍可能发生删除或撤权；在真正发送 final 的
            # 最后边界使用本请求冻结的快照再核验一次，禁止改读新激活版本。
            _validate_stream_final_sources(
                self,
                evidence,
                request,
                snapshot,
                provider_calls,
            )
            _raise_if_cancelled(cancellation, provider_calls)
            _emit_final(on_final, result, provider_calls)
        if cache_result:
            if on_final is None:
                self.commit_result_cache(result, cancellation=cancellation)
            else:
                with suppress(Exception):
                    # final 已确认交付后，缓存故障或瞬时断连都不能把已经
                    # 公开的权威结果改写成失败/取消终态。
                    self.commit_result_cache(result)
        self._record(trace_id, "complete", {"status": result.status.value})
        return result

    def _catalog_fast_path(  # noqa: PLR0913, PLR0915
        self,
        *,
        request: SearchRequest,
        snapshot: ActiveRevisionQuerySnapshot,
        analysis: QueryAnalysis,
        plan: RetrievalPlan,
        cache_key: str,
        trace_id: str,
        stage_timings: list[StageTiming],
        on_stage: Callable[[str, dict[str, object]], None] | None,
        on_final: Callable[[SearchAnswerResult], None] | None,
        cancellation: CancellationPort | None,
    ) -> SearchAnswerResult | None:
        """仅用活动版本的目录元数据回答文档导航问题。"""
        catalog_reader = getattr(self._source, "catalog_documents", None)
        if catalog_reader is None:
            return None
        # 角色或章节过滤必须经过普通 Chunk 检索，不能由文档目录代答。
        if any(
            key in {"role", "section_id"}
            for key, _value in request.metadata_filters
        ):
            return None
        apply_candidate_filters((), request)
        started = perf_counter()
        documents = catalog_reader(snapshot, limit=2000)
        if documents is None:
            self._record(trace_id, "catalog", {"reason_code": "CATALOG_LIMIT"})
            return None
        candidates = tuple(
            ChannelHit(
                revision_id=snapshot.revision.index_revision_id,
                chunk_id=document.chunk_id,
                document_id=document.document_id,
                document_version_id=document.document_version_id,
                role="catalog",
                section_id="catalog",
                content_sha256="0" * 64,
                channel="catalog",
                rank=index,
                raw_score=0.0,
            )
            for index, document in enumerate(documents, start=1)
            if document.document_id not in snapshot.excluded_document_ids
        )
        visible_ids = {
            hit.document_id
            for hit in apply_candidate_filters(candidates, request)
        }
        visible = tuple(
            document
            for document in documents
            if document.document_id in visible_ids
        )
        matched = catalog_matches(analysis.normalized_query, visible)
        if matched and self._policy.evidence_group_mode != "off":
            group_started = perf_counter()
            catalog_groups = tuple(
                build_catalog_evidence_group(
                    document,
                    index_revision_id=snapshot.revision.index_revision_id,
                )
                for document in matched
            )
            active = self._policy.evidence_group_mode == "active"
            self._record(
                trace_id,
                "evidence_group_build",
                {
                    "pass": "catalog",
                    "mode": self._policy.evidence_group_mode,
                    "group_count": len(catalog_groups),
                    "group_type_counts": (
                        ("CATALOG_ENTRY", len(catalog_groups)),
                    ),
                    "complete_group_count": len(catalog_groups),
                    "incomplete_group_count": 0,
                    "members_per_group": tuple(0 for _ in catalog_groups),
                    "build_elapsed_ms": round(
                        (perf_counter() - group_started) * 1000, 3
                    ),
                    "selected_group_count": len(catalog_groups)
                    if active
                    else 0,
                    "group_diagnostics": tuple(
                        {
                            "group_id": group.group_id,
                            "group_type": "CATALOG_ENTRY",
                            "member_count": 0,
                            "complete": True,
                            "completeness_reason": "COMPLETE",
                            "best_rank": 0,
                            "selected": active,
                            "drop_reason": None if active else "SHADOW_ONLY",
                            "token_cost": group.token_cost,
                        }
                        for group in catalog_groups
                    ),
                },
            )
            _finish_timing(
                stage_timings, "catalog_evidence_group", group_started
            )
        citations = tuple(_catalog_citation(document) for document in matched)
        if len(matched) == 1:
            status = ConfidenceStatus.ANSWERABLE
            answer = (
                f"可参考《{matched[0].title}》。"
                "此目录只证明文档存在；具体内容请查阅原件。"
            )
            reason = "CATALOG_UNIQUE"
        elif matched:
            status = ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION
            options = "、".join(f"《{item.title}》" for item in matched)
            answer = f"目录中找到可能相关的资料：{options}。请说明具体用途。"
            reason = "CATALOG_MULTIPLE"
        else:
            status = ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION
            answer = "尚未在已入库目录中确认对应资料。请补充主题或文档名称。"
            reason = "CATALOG_NO_MATCH"
        if matched:
            self._validate_catalog_citations(matched, request, snapshot)
        _finish_timing(stage_timings, "catalog", started)
        self._record(
            trace_id,
            "catalog",
            {"reason_code": reason, "candidate_count": len(matched)},
        )
        _emit_stage(
            on_stage,
            "retrieval",
            {
                "cache_hit": False,
                "evidence_count": len(citations),
                "status": status.value,
            },
            [],
        )
        _emit_stage(on_stage, "generation", {"mode": "catalog_fast_path"}, [])
        _emit_stage(
            on_stage,
            "validation",
            {"published": bool(matched), "generation_mode": "none"},
            [],
        )
        self._record(
            trace_id,
            "generate",
            {
                "mode": "none",
                "answer_path": "CATALOG_FAST_PATH",
                "generation_called": False,
                "extractive_fallback_used": False,
                "final_coverage_by_atom": {"A1": "NOT_OBSERVED"},
                "reason_code": reason,
                "provider_call_count_by_operation": (
                    _provider_call_count_by_operation(())
                ),
                "provider_call_count_observation_status": (
                    "CAPTURED_PROVIDER_CALLS"
                ),
                "provider_calls": [],
            },
        )
        self._record(
            trace_id,
            "validate",
            {"published": bool(matched), "support_count": len(citations)},
        )
        diagnostics = RetrievalDiagnostics(stage_timings=tuple(stage_timings))
        result = SearchAnswerResult(
            trace_id=trace_id,
            status=status,
            reason_code=status.value,
            answer=answer,
            catalog_citations=citations,
            confidence=ConfidenceDecision(
                status=status,
                score=1.0 if len(matched) == 1 else 0.0,
                reason_codes=(reason,),
            ),
            query_kind=plan.query_kind,
            reasoning_effort=(
                ReasoningEffort.DIRECT.value
                if len(matched) == 1
                else ReasoningEffort.ASSISTED.value
            ),
            requested_answer_type=analysis.semantics.answer_type,
            query_semantic_source=analysis.semantics.source,
            active_index_revision_id=snapshot.revision.index_revision_id,
            index_fingerprint=snapshot.revision.index_fingerprint,
            serving_fingerprint=snapshot.serving_fingerprint,
            route_reason_code="CATALOG_METADATA",
            rerank_execution_mode="catalog_fast_path",
            generation_mode="none",
            generation_reason_code=reason,
            interpret_reason_code="CATALOG_FAST_PATH",
            rewrite_reason_code="CATALOG_FAST_PATH",
            cache_key=cache_key,
            diagnostics=diagnostics,
            diagnostics_summary=_diagnostics_summary(diagnostics),
            data_plane=self._query_data_plane(
                snapshot,
                selected_slot=None,
                rerank_mode="catalog_fast_path",
                degraded=(),
            ),
        )
        _raise_if_cancelled(cancellation)
        self._record_shortcut_trace(trace_id, result, "CATALOG_FAST_PATH")
        if on_final is not None:
            if matched:
                self._validate_catalog_citations(matched, request, snapshot)
            _emit_final(on_final, result, [])
        self._record(trace_id, "complete", {"status": status.value})
        return result

    def _validate_catalog_citations(
        self,
        documents: tuple[CatalogDocument, ...],
        request: SearchRequest,
        snapshot: ActiveRevisionQuerySnapshot,
    ) -> None:
        """发布前复核目录版本、可见性和 canonical Chunk 身份。"""
        current = self._source.catalog_documents(snapshot, limit=2000)
        if current is None:
            raise IndexCorrupt(
                "Catalog 已超过可验证上限。", stage="retrieval.catalog"
            )
        by_id = {item.document_id: item for item in current}
        if any(by_id.get(item.document_id) != item for item in documents):
            raise IndexCorrupt(
                "Catalog 来源已变更。", stage="retrieval.catalog"
            )
        rows = self._source.hydrate_chunks(
            snapshot, tuple(item.chunk_id for item in documents)
        )
        by_chunk = {row.chunk.chunk_id: row for row in rows}
        for item in documents:
            row = by_chunk.get(item.chunk_id)
            if row is None:
                raise IndexCorrupt(
                    "Catalog 来源不可用。", stage="retrieval.catalog"
                )
            validate_candidate(
                RankedChunk(hydrated=row, fusion_rank=1),
                request,
                snapshot.revision.index_revision_id,
            )

    def _query_data_plane(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        *,
        selected_slot: str | None,
        rerank_mode: str,
        degraded: tuple[str, ...],
    ) -> QueryDataPlane:
        """由本次冻结快照和实际路由生成统一数据面真相。

        Args:
            snapshot: 请求开始时冻结的活动 Revision。
            selected_slot: Dense 实际选择的 slot；未执行时为空。
            rerank_mode: 本次实际重排或旁路模式。
            degraded: 本次执行收集的稳定降级原因。

        Returns:
            不含正文和 Secret 的公开数据面合同。

        """
        context = self._data_plane_context
        slot_id = selected_slot or snapshot.topology.primary_slot_id
        slot = snapshot.topology.slot(slot_id)
        fallbacks = list(context.fallback_reason_codes)
        if selected_slot is None:
            fallbacks.append("DENSE_NOT_SELECTED_BY_PLAN")
        if slot.provider_id.casefold().startswith("deterministic"):
            fallbacks.append("DETERMINISTIC_EMBEDDING")
        fallbacks.extend(degraded)
        return QueryDataPlane(
            retrieval_data_plane=context.retrieval_data_plane,
            active_retrieval_profile_revision_id=(
                snapshot.profile_revision_id
                or context.active_retrieval_profile_revision_id
            ),
            active_index_revision_id=snapshot.revision.index_revision_id,
            index_fingerprint=snapshot.revision.index_fingerprint,
            serving_fingerprint=snapshot.serving_fingerprint,
            embedding_provider_id=slot.provider_id,
            embedding_model=slot.model,
            selected_vector_space=(
                slot.vector_space_identity
                if selected_slot is not None
                else None
            ),
            reranker_provider_id=context.reranker_provider_id,
            reranker_model=context.reranker_model,
            rerank_mode=rerank_mode,
            generation_provider_id=context.generation_provider_id,
            generation_model=context.generation_model,
            interpret_provider_id=context.interpret_provider_id,
            interpret_model=context.interpret_model,
            rewrite_provider_id=context.rewrite_provider_id,
            rewrite_model=context.rewrite_model,
            dense_calibration_state=(
                snapshot.retrieval_policy.dense_semantic_calibration_state
            ),
            model_configuration_state=context.model_configuration_state,
            model_authorization_state=context.model_authorization_state,
            corpus_authorization_state=context.corpus_authorization_state,
            budget_state=context.budget_state,
            fallback_reason_codes=tuple(dict.fromkeys(fallbacks)),
        )

    def validate_shared_result(
        self,
        result: SearchAnswerResult,
        request: SearchRequest,
        identity: RetrievalExecutionIdentity,
    ) -> None:
        """Follower 返回前重新核验 Revision、scope 与来源可读性。

        Args:
            result: Leader 已完成全部门禁的结果。
            request: 当前 Follower 的独立请求。
            identity: 加入 flight 前冻结的计算身份。

        Returns:
            校验通过时无返回值。

        """
        snapshot = self._query_snapshot(
            request.model_copy(
                update={
                    "expected_active_revision_id": identity.active_revision_id,
                    "expected_serving_fingerprint": (
                        identity.serving_fingerprint
                    ),
                }
            )
        )
        self._validate_cached_sources(result, request, snapshot)

    def record_singleflight_observation(
        self,
        result: SearchAnswerResult,
        identity: RetrievalExecutionIdentity,
    ) -> None:
        """为当前请求记录不含正文的 singleflight 身份与等待时间。

        Follower 没有重复执行检索阶段，因此先补充冻结的 Revision 身份；
        History 与 Operational Trace 仍使用当前请求自己的 ``trace_id``。

        Args:
            result: 已投影为当前请求 Trace ID 的最终结果。
            identity: 加入共享计算前冻结的执行身份。

        Returns:
            无返回值。

        """
        if result.singleflight_role == "follower":
            self._record(
                result.trace_id,
                "snapshot",
                {
                    "revision_id": identity.active_revision_id,
                    "index_fingerprint": result.index_fingerprint,
                    "serving_fingerprint": identity.serving_fingerprint,
                },
            )
        self._record(
            result.trace_id,
            "singleflight",
            {
                "role": result.singleflight_role,
                "key_hash": identity.key_hash,
                "wait_ms": result.singleflight_wait_ms,
            },
        )
        if result.singleflight_role == "follower":
            self._record(
                result.trace_id,
                "complete",
                {"status": result.status.value},
            )

    def _query_snapshot(
        self, request: SearchRequest
    ) -> ActiveRevisionQuerySnapshot:
        """读取并验证 Product Profile 与可选 singleflight 快照。"""
        snapshot = self._source.active_query_snapshot(
            request.scope,
            serving_fingerprint=self._serving_fingerprint,
            retrieval_policy=self._policy,
        )
        if (
            self._expected_index_fingerprint is not None
            and snapshot.revision.index_fingerprint
            != self._expected_index_fingerprint
        ):
            raise IndexCorrupt(
                "Query Profile 与 Active Revision 语义不一致。",
                stage="retrieval.snapshot",
            )
        if (
            self._expected_profile_revision_id is not None
            and snapshot.profile_revision_id
            != self._expected_profile_revision_id
        ):
            raise IndexCorrupt(
                "Query Profile 已切换，请重试查询。", stage="retrieval.snapshot"
            )
        if (
            request.expected_active_revision_id is not None
            and snapshot.revision.index_revision_id
            != request.expected_active_revision_id
        ):
            raise IndexCorrupt(
                "活动 Revision 已在请求合并期间切换，请重试。",
                stage="retrieval.snapshot",
                code="SINGLEFLIGHT_REVISION_CHANGED",
            )
        if (
            request.expected_serving_fingerprint is not None
            and snapshot.serving_fingerprint
            != request.expected_serving_fingerprint
        ):
            raise IndexCorrupt(
                "Serving Profile 已在请求合并期间切换，请重试。",
                stage="retrieval.snapshot",
                code="SINGLEFLIGHT_SERVING_CHANGED",
            )
        return snapshot

    def _cache_identity(
        self,
        request: SearchRequest,
        snapshot: ActiveRevisionQuerySnapshot,
        analysis: QueryAnalysis,
        plan: RetrievalPlan,
    ) -> BaseResultCacheKey:
        """构造不含正文但覆盖全部答案行为的规范缓存键。"""
        resolved_root = resolve_root_query(request, build_input_spans(request))
        normalized_variants = tuple(
            dict.fromkeys(
                " ".join(
                    unicodedata.normalize("NFKC", variant.text).strip().split()
                )
                for variant in plan.variants
            )
        )
        rewrite_identity = canonical_sha256(
            {
                "variants": normalized_variants,
                "semantic_policy": "shared-query-semantics-v3-09",
                "rewrite_policy": "bounded-rewrite-v3",
                "answer_support_policy": "minimum-supported-set-v3-08",
                "answer_generation_policy": "model-grounded-claims-v2",
                "query_plan_schema_revision": QUERY_PLAN_SCHEMA_REVISION,
                "context_resolution_revision": CONTEXT_RESOLUTION_REVISION,
                "resolved_root_query_sha256": hashlib.sha256(
                    resolved_root.resolved_query.encode("utf-8")
                ).hexdigest(),
                "context_resolution_mode": resolved_root.mode,
                "context_digest": resolved_root.context_digest,
                "query_unit_fusion_revision": QUERY_UNIT_FUSION_REVISION,
                "atom_group_alignment_revision": ATOM_GROUP_ALIGNMENT_REVISION,
                "evidence_group_schema_revision": (
                    EVIDENCE_GROUP_SCHEMA_REVISION
                ),
                "grounded_claim_schema_revision": (
                    GROUNDED_CLAIM_SCHEMA_REVISION
                ),
                "generation_evidence_pack_revision": (
                    GENERATION_EVIDENCE_PACK_REVISION
                ),
                "answer_pipeline_revision": "wb08r-evidence-first-v3",
                "natural_renderer_revision": NATURAL_RENDERER_REVISION,
                "corrective_retrieval_revision": CORRECTIVE_RETRIEVAL_REVISION,
            }
        )
        return BaseResultCacheKey(
            project_id=request.scope.project_id,
            knowledge_base_id=request.scope.knowledge_base_id,
            active_revision_id=snapshot.revision.index_revision_id,
            index_fingerprint=snapshot.revision.index_fingerprint,
            serving_fingerprint=snapshot.serving_fingerprint,
            retrieval_profile_identity=(
                snapshot.profile_revision_id or "default-offline-profile"
            ),
            query_sha256=hashlib.sha256(
                analysis.normalized_query.encode("utf-8")
            ).hexdigest(),
            owner_identity_hash=hashlib.sha256(
                request.owner_identity.encode("utf-8")
            ).hexdigest(),
            metadata_filter_hash=canonical_sha256(request.metadata_filters),
            access_filter_hash=canonical_sha256(
                (request.access_filters, snapshot.excluded_document_ids)
            ),
            conversation_identity=analysis.conversation_fingerprint,
            rewrite_policy_identity=rewrite_identity,
            query_semantics_identity=canonical_sha256(
                {
                    "semantics": analysis.semantics.model_dump(mode="json"),
                    "plan_variants": normalized_variants,
                }
            ),
            cache_schema=self._policy.cache_schema_version,
            limit=request.limit,
            dense_required=request.dense_required,
            generation_behavior=self._generation_behavior,
            include_related_content=request.include_related_content,
            related_policy_version=DISPLAY_POLICY.version,
        )

    def commit_result_cache(
        self,
        result: SearchAnswerResult,
        *,
        cancellation: CancellationPort | None = None,
    ) -> None:
        """仅在最终发布边界之后写入可复用结果。

        Args:
            result: 已通过完整回答与引用校验的最终结果。
            cancellation: 非流式路径在写入前使用的取消令牌；流式 final
                已交付后不再传入。

        Returns:
            无返回值；不满足稳定缓存条件时保持未写入。

        """
        if result.result_origin != "fresh":
            return
        if result.rerank_execution_mode == "catalog_fast_path":
            return
        if (
            result.status is ConfidenceStatus.ANSWERABLE
            and not rerank_dependency_failed(result.rerank_execution_mode)
        ):
            _raise_if_cancelled(cancellation)
            self._cache.put(result.cache_key, result, ttl_seconds=300)
        elif (
            result.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
            and not result.degraded_reason_codes
            and not rerank_dependency_failed(result.rerank_execution_mode)
        ):
            _raise_if_cancelled(cancellation)
            self._cache.put(result.cache_key, result, ttl_seconds=30)

    def _close_structural_context(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
    ) -> ExpansionOutcome:
        """沿 canonical 邻接链批量闭合有界结构候选。"""
        return self._neighbors.close_structure(
            snapshot, candidates, self._policy
        )

    def _analysis_for_atom(
        self, request: SearchRequest, atom: QueryAtom
    ) -> QueryAnalysis:
        """继承请求边界，只替换当前原子的可检索语义。"""
        analysis = self._analyzer.analyze(
            request.model_copy(update={"text": atom.search_text})
        )
        answer_type = {
            AtomAnswerShape.DEFINITION: RequestedAnswerType.DEFINITION,
            AtomAnswerShape.ENUMERATION: RequestedAnswerType.ENUMERATION,
            AtomAnswerShape.PROCEDURE: RequestedAnswerType.PROCEDURE,
            AtomAnswerShape.DUTIES: RequestedAnswerType.DUTIES,
            AtomAnswerShape.RESPONSIBLE_PARTY: (
                RequestedAnswerType.RESPONSIBLE_PARTY
            ),
            AtomAnswerShape.COUNT: RequestedAnswerType.COUNT,
            AtomAnswerShape.DURATION: RequestedAnswerType.DURATION,
        }.get(atom.answer_shape, RequestedAnswerType.FACT)
        semantics = analysis.semantics.model_copy(
            update={
                "target": atom.target,
                "relation": atom.relation,
                "answer_type": answer_type,
                "source_qualifier": atom.source_qualifier,
                "source": "SPAN_REFERENCED",
            }
        )
        return analysis.model_copy(update={"semantics": semantics})

    def _retrieve_atoms(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        *,
        request: SearchRequest,
        snapshot: ActiveRevisionQuerySnapshot,
        root_analysis: QueryAnalysis,
        plan: RetrievalPlan,
        query_plan: QueryPlan,
        provider_calls: list[ProviderCall],
        degraded: list[str],
        trace_id: str,
        stage_timings: list[StageTiming],
    ) -> _AtomRetrievalOutcome:
        """Root 和原子分别召回，批量嵌入后执行两级有界融合。"""
        started = perf_counter()
        merged: dict[str, dict[str, ChannelHit]] = {}
        root_text = query_plan.resolved_root_query
        units = (
            QueryUnit(
                unit_id="ROOT",
                atom_id=None,
                text=root_text,
                analysis=root_analysis,
                weight=self._policy.unit_root_weight,
            ),
            *(
                QueryUnit(
                    unit_id=atom.atom_id,
                    atom_id=atom.atom_id,
                    text=atom.search_text,
                    analysis=self._analysis_for_atom(request, atom),
                    weight=(
                        self._policy.unit_atom_total_weight
                        / len(query_plan.atoms)
                    ),
                )
                for atom in query_plan.atoms
            ),
        )
        unit_channels: dict[str, dict[str, tuple[ChannelHit, ...]]] = {
            unit.unit_id: {} for unit in units
        }
        top_k = dict(plan.channel_top_k)
        enabled = frozenset(self._policy.enabled_channels)

        def limit_for(channel: str) -> int:
            return top_k.get(channel, self._policy.channel_top_k)

        def add(
            unit_id: str, channel: str, hits: tuple[ChannelHit, ...]
        ) -> None:
            normalized_hits = tuple(
                hit.model_copy(update={"channel": channel}) for hit in hits
            )
            unit_channels[unit_id][channel] = normalized_hits
            channel_items = merged.setdefault(channel, {})
            for hit in normalized_hits:
                prior = channel_items.get(hit.chunk_id)
                if prior is None or hit.rank < prior.rank:
                    channel_items[hit.chunk_id] = hit

        for unit in units:
            if "exact" in enabled:
                try:
                    add(
                        unit.unit_id,
                        "exact",
                        apply_candidate_filters(
                            self._exact.search(
                                snapshot,
                                unit.analysis,
                                limit=limit_for("exact"),
                            ),
                            request,
                        ),
                    )
                except (ChannelRateLimited, ChannelUnavailable) as error:
                    degraded.append(error.code)
            if "structural" in enabled:
                try:
                    add(
                        unit.unit_id,
                        "structural",
                        apply_candidate_filters(
                            self._structural.search(
                                snapshot,
                                unit.analysis,
                                limit=limit_for("structural"),
                            ),
                            request,
                        ),
                    )
                except (ChannelRateLimited, ChannelUnavailable) as error:
                    degraded.append(error.code)
            if "lexical" in enabled:
                variants = (
                    plan.variants
                    if unit.unit_id == "ROOT"
                    else (
                        QueryVariant(
                            text=unit.text,
                            kind="original",
                            identity=canonical_sha256(
                                {
                                    "atom_query": unit.text,
                                    "schema": QUERY_PLAN_SCHEMA_REVISION,
                                }
                            ),
                        ),
                    )
                )
                for variant in variants:
                    channel = (
                        "lexical"
                        if variant.kind == "original"
                        else f"lexical:{variant.kind}"
                    )
                    try:
                        add(
                            unit.unit_id,
                            channel,
                            apply_candidate_filters(
                                self._lexical.search(
                                    snapshot,
                                    variant,
                                    limit=limit_for("lexical"),
                                    analysis=unit.analysis,
                                ),
                                request,
                            ),
                        )
                    except (ChannelRateLimited, ChannelUnavailable) as error:
                        degraded.append(error.code)
        selected_slot: str | None = None
        selected_vector: str | None = None
        route_reason = "DENSE_DISABLED_BY_PLAN"
        if "dense" in enabled:
            try:
                dense_results = self._dense.search_many(
                    snapshot,
                    tuple(unit.text for unit in units),
                    self._egress,
                    limit=limit_for("dense"),
                )
            except (DenseUnavailable, PolicyDenied) as error:
                if plan.dense_required:
                    raise
                degraded.append(error.code)
                route_reason = error.code
            except IndexCompatibilityError as error:
                raise IndexCorrupt(
                    "逐 Atom Dense route 与 Active Revision 不兼容。",
                    stage="retrieval.dense",
                ) from error
            else:
                for unit, dense in zip(units, dense_results, strict=True):
                    provider_calls.extend(dense.routed.provider_calls)
                    if selected_slot is not None and (
                        selected_slot != dense.routed.selected_slot_id
                        or selected_vector != dense.routed.vector_name
                    ):
                        raise IndexCorrupt(
                            "同一 QueryPlan 禁止切换 Dense slot。",
                            stage="retrieval.dense",
                        )
                    selected_slot = dense.routed.selected_slot_id
                    selected_vector = dense.routed.vector_name
                    route_reason = dense.routed.fallback_reason
                    add(
                        unit.unit_id,
                        f"dense:{selected_slot}",
                        apply_candidate_filters(dense.hits, request),
                    )
                self._record(
                    trace_id,
                    "query_embedding_route",
                    {
                        "selected_slot": selected_slot,
                        "vector_name": selected_vector,
                        "reason_code": route_reason,
                        "batch_size": len({unit.text for unit in units}),
                        "call_count": sum(
                            call.call_count
                            for result in dense_results
                            for call in result.routed.provider_calls
                        ),
                    },
                )
        fusion = fuse_query_units(
            tuple(
                QueryUnitRetrieval(unit, unit_channels[unit.unit_id])
                for unit in units
            ),
            revision_id=snapshot.revision.index_revision_id,
            policy=self._policy,
        )
        channels = {
            channel: tuple(
                sorted(items.values(), key=lambda hit: (hit.rank, hit.chunk_id))
            )
            for channel, items in merged.items()
        }
        self._record(
            trace_id,
            "atom_retrieval",
            {
                "root_query_present": True,
                "atom_count": len(query_plan.atoms),
                "channel_counts": tuple(
                    (channel, len(hits)) for channel, hits in channels.items()
                ),
                "candidate_link_count": len(fusion.links),
                "seed_chunk_count": len(fusion.seed_chunk_ids),
                "seed_chunk_ids": fusion.seed_chunk_ids,
                "root_lexical_top_chunk_ids": tuple(
                    hit.chunk_id
                    for hit in unit_channels["ROOT"].get("lexical", ())[:6]
                ),
                "fused_chunk_count": len(fusion.candidates),
            },
        )
        _finish_timing(stage_timings, "atom_retrieval", started)
        rerank_query = (
            "原始问题："
            + root_text
            + "\n子问题：\n"
            + "\n".join(
                f"- {atom.atom_id} {atom.target}｜{atom.relation}"
                for atom in query_plan.atoms
            )
        )
        return _AtomRetrievalOutcome(
            channels,
            fusion.links,
            selected_slot,
            selected_vector,
            route_reason,
            fusion.candidates,
            fusion.seed_chunk_ids,
            rerank_query,
        )

    def _ground_atoms(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        *,
        request: SearchRequest,
        query_plan: QueryPlan,
        candidates: tuple[RankedChunk, ...],
        groups: tuple[GroupCandidate, ...],
        links: tuple[AtomCandidateLink, ...],
        selected_slot: str | None,
        snapshot: ActiveRevisionQuerySnapshot,
        rerank_mode: str,
        trace_id: str | None = None,
    ) -> tuple[
        AtomSupportMatrix,
        tuple[EvidenceItem, ...],
        tuple[AtomCoverage, ...],
        tuple[tuple[str, tuple[EvidenceItem, ...]], ...],
    ]:
        """按每个原子的语义复用 EvidenceAssembler，不以相似命中冒充支持。"""

        def identity(item: EvidenceItem) -> tuple[object, ...]:
            return (
                item.document_version_id,
                item.chunk_id,
                item.citation_text,
                tuple(span.node_id for span in item.source_spans),
            )

        vector_space = (
            snapshot.topology.slot(selected_slot).vector_space_identity
            if selected_slot is not None
            else None
        )
        selected_by_key: dict[tuple[object, ...], EvidenceItem] = {}
        per_atom: list[
            tuple[
                QueryAtom,
                tuple[tuple[object, ...], ...],
                tuple[tuple[object, ...], ...],
            ]
        ] = []
        alignments_by_atom: dict[str, tuple[AtomGroupAlignment, ...]] = {}
        groups_by_atom: dict[str, tuple[GroupCandidate, ...]] = {}
        qualifications_by_atom: dict[
            str, dict[tuple[object, ...], AtomEvidenceQualification]
        ] = {}
        atom_evidence_audit: list[dict[str, object]] = []
        for atom in query_plan.atoms:
            atom_candidates, atom_groups, alignments = _atom_scoped_candidates(
                atom,
                candidates,
                groups,
                links,
                policy=self._policy,
                multi_atom=(
                    len(query_plan.atoms) > 1
                    or self._policy.evidence_group_mode == "active"
                ),
            )
            alignments_by_atom[atom.atom_id] = alignments
            groups_by_atom[atom.atom_id] = atom_groups
            atom_analysis = self._analysis_for_atom(request, atom)
            atom_plan = self._planner.plan(
                atom_analysis,
                self._expander.expand(atom_analysis),
                self._policy,
                dense_required=request.dense_required,
            )
            selection = self._evidence.assemble_sets(
                atom_candidates,
                self._policy,
                include_model_candidates=True,
                groups=atom_groups
                if self._policy.evidence_group_mode == "active"
                else None,
                context=EvidenceSelectionContext(
                    analysis=atom_analysis,
                    query_kind=atom_plan.query_kind,
                    rerank_mode=rerank_mode,
                    selected_slot=selected_slot,
                    selected_vector_space=vector_space,
                    include_table_context=True,
                ),
            )
            scoped_items = (
                *selection.answer_support_set,
                *selection.model_evidence_candidates,
            )
            atom_evidence_audit.append(
                {
                    "atom_id": atom.atom_id,
                    "answer_shape": atom.answer_shape.value,
                    "scoped_candidate_count": len(atom_candidates),
                    "selected_group_count": len(atom_groups),
                    "direct_support_count": len(selection.answer_support_set),
                    "model_candidate_count": len(
                        selection.model_evidence_candidates
                    ),
                    "support_status_distribution": dict(
                        Counter(
                            str(support.get("status", "NOT_EVALUATED"))
                            if isinstance(
                                support := dict(item.metadata).get(
                                    "answer_support"
                                ),
                                dict,
                            )
                            else "NOT_EVALUATED"
                            for item in scoped_items
                        )
                    ),
                }
            )
            alignment_by_group = {
                alignment.group_id: alignment for alignment in alignments
            }
            qualifications = {
                identity(item): qualify_atom_evidence(
                    atom,
                    item,
                    links,
                    alignment=alignment_by_group.get(
                        dict(item.metadata).get("evidence_group_id")
                    ),
                    resolved_root_query=query_plan.resolved_root_query,
                    context_resolution_confidence=(
                        "LOW"
                        if query_plan.needs_clarification
                        else "LOW"
                        if query_plan.coverage_confidence == "LOW"
                        and len(query_plan.atoms) > 1
                        else "HIGH"
                    ),
                    single_atom_direct=len(query_plan.atoms) == 1
                    and not query_plan.needs_clarification,
                    supporting_items=scoped_items,
                )
                for item in scoped_items
            }
            qualifications_by_atom[atom.atom_id] = qualifications
            allowed_keys = {
                key
                for key, qualification in qualifications.items()
                if qualification.retrieval_relevant or qualification.publishable
            }
            direct_keys = tuple(
                identity(item)
                for item in scoped_items
                if identity(item) in allowed_keys
                and qualifications[identity(item)].publishable
            )
            if selection.ambiguous:
                direct_keys = ()
            elif (
                atom.answer_shape
                in {
                    AtomAnswerShape.ENUMERATION,
                    AtomAnswerShape.DUTIES,
                    AtomAnswerShape.PROCEDURE,
                }
                and not atom.source_qualifier
            ):
                direct_documents = {
                    item.document_id
                    for item in scoped_items
                    if identity(item) in direct_keys
                }
                if len(direct_documents) > 1:
                    direct_keys = ()
            candidate_keys = tuple(
                identity(item)
                for item in scoped_items
                if identity(item) in allowed_keys
                and identity(item) not in direct_keys
            )
            if atom.answer_shape not in {
                AtomAnswerShape.ENUMERATION,
                AtomAnswerShape.PROCEDURE,
                AtomAnswerShape.DUTIES,
            }:
                # 单事实不把整批仅相关候选送给模型；表格闭合最多保留
                # 三个来源单元，引用仍由各自 citation_text 校验。
                direct_keys = direct_keys[:3]
                candidate_keys = candidate_keys[:3]
            elif not direct_keys:
                # 不完整结构组只能支撑有限回答，避免用十余条宽候选
                # 制造接近整份文档的 Generation 输入。
                candidate_keys = candidate_keys[:6]
            per_atom.append((atom, direct_keys, candidate_keys))
            for item in scoped_items:
                selected_by_key.setdefault(identity(item), item)
        # 先轮流保障各原子的直接支持，再公平补入有限相关上下文。
        ordered_keys: list[tuple[object, ...]] = []
        for index in range(self._policy.max_evidence_items):
            for _atom, direct_keys, _candidate_keys in per_atom:
                if index < len(direct_keys):
                    key = direct_keys[index]
                    if key not in ordered_keys:
                        ordered_keys.append(key)
        for index in range(self._policy.max_evidence_items):
            for _atom, _direct_keys, candidate_keys in per_atom:
                if index < len(candidate_keys):
                    key = candidate_keys[index]
                    if key not in ordered_keys:
                        ordered_keys.append(key)
        structural = any(
            atom.answer_shape
            in {
                AtomAnswerShape.ENUMERATION,
                AtomAnswerShape.PROCEDURE,
                AtomAnswerShape.DUTIES,
            }
            for atom, _direct_keys, _candidate_keys in per_atom
        )
        max_items = min(
            12 if structural else 8, self._policy.max_evidence_items * 2
        )
        ordered_keys = ordered_keys[:max_items]
        evidence = tuple(
            selected_by_key[key].model_copy(update={"evidence_id": f"S{index}"})
            for index, key in enumerate(ordered_keys, 1)
        )
        by_key = dict(zip(ordered_keys, evidence, strict=True))
        reranked_ids = {
            item.hydrated.chunk.chunk_id
            for item in candidates
            if item.rerank_rank
        }
        supports: list[AtomSupport] = []
        coverages: list[AtomCoverage] = []
        atom_candidate_membership: list[
            tuple[str, tuple[EvidenceItem, ...]]
        ] = []
        for atom, direct_keys, candidate_keys in per_atom:
            direct = tuple(by_key[key] for key in direct_keys if key in by_key)
            relevant = tuple(
                by_key[key] for key in candidate_keys if key in by_key
            )
            atom_candidate_membership.append(
                (atom.atom_id, (*direct, *relevant))
            )
            atom_qualifications = qualifications_by_atom[atom.atom_id]
            direct_text = "\n".join(
                item.citation_text for item in direct
            ).casefold()
            checks: list[tuple[str, bool]] = []
            for constraint in atom.constraints:
                value = unicodedata.normalize(
                    "NFKC", constraint.value
                ).casefold()
                source_text = "\n".join(
                    "{} {}".format(
                        item.display_name or "",
                        dict(item.metadata).get("document_title", ""),
                    )
                    for item in direct
                ).casefold()
                text = (
                    source_text
                    if constraint.kind.value == "SOURCE"
                    else direct_text
                )
                checks.append((constraint.kind.value, value in text))
            if atom.source_qualifier:
                source_text = "\n".join(
                    "{} {}".format(
                        item.display_name or "",
                        dict(item.metadata).get("document_title", ""),
                    )
                    for item in direct
                ).casefold()
                checks.append(
                    ("SOURCE", atom.source_qualifier.casefold() in source_text)
                )
            structural = atom.answer_shape in {
                AtomAnswerShape.ENUMERATION,
                AtomAnswerShape.PROCEDURE,
                AtomAnswerShape.DUTIES,
            }
            complete_group_ids = {
                group.group_id
                for group in groups_by_atom[atom.atom_id]
                if group_source_maps_covered(
                    group,
                    tuple(
                        item
                        for item in direct
                        if dict(item.metadata).get("evidence_group_id")
                        == group.group_id
                    ),
                )
            }
            if structural and self._policy.evidence_group_mode == "active":
                checks.append(
                    (
                        "GROUP_COMPLETE",
                        bool(complete_group_ids),
                    )
                )
            if (
                structural
                and self._policy.evidence_group_mode == "active"
                and groups
            ):
                strong_group_ids = {
                    alignment.group_id
                    for alignment in alignments_by_atom[atom.atom_id]
                    if alignment.qualification is AlignmentQualification.STRONG
                }
                checks.append(
                    (
                        "GROUP_ANCHORED",
                        bool(complete_group_ids & strong_group_ids),
                    )
                )
            conflicting = _numeric_conflict(atom, direct)
            status = (
                AtomStatus.CONTRADICTORY
                if conflicting
                else AtomStatus.SUPPORTED
                if direct and all(passed for _name, passed in checks)
                else AtomStatus.PARTIAL
                if direct or relevant
                else AtomStatus.MISSING
            )
            support_items = (
                conflicting
                if status is AtomStatus.CONTRADICTORY
                else direct
                if direct
                else relevant
            )
            groups_for_atom = tuple(
                dict.fromkeys(
                    group_id
                    for item in support_items
                    if isinstance(
                        group_id := dict(item.metadata).get(
                            "evidence_group_id"
                        ),
                        str,
                    )
                )
            )
            supports.append(
                AtomSupport(
                    atom_id=atom.atom_id,
                    status=status,
                    supporting_group_ids=groups_for_atom,
                    supporting_support_ids=tuple(
                        item.support_id
                        for item in (
                            conflicting
                            if status is AtomStatus.CONTRADICTORY
                            else direct
                        )
                    ),
                    relation_certified_group_ids=tuple(
                        alignment.group_id
                        for alignment in alignments_by_atom[atom.atom_id]
                        if alignment.structural_relation_proven
                        and alignment.publishable
                        and alignment.group_id in complete_group_ids
                    ),
                    missing_aspects=tuple(
                        (name for name, passed in checks if not passed)
                    )
                    + (
                        ("STRUCTURE_GROUP_INCOMPLETE",)
                        if structural and relevant and not direct
                        else ("ROOT_SOURCE_HIT_ATOM_MISS",)
                        if relevant
                        and any(
                            atom_qualifications[identity(item)].provenance.value
                            == "ROOT"
                            for item in relevant
                        )
                        and not direct
                        else ("EVIDENCE_PRESENT_BUT_NOT_OWNED",)
                        if relevant and not direct
                        else ()
                    ),
                    contradictions=(
                        ("NUMERIC_VALUE_CONFLICT",) if conflicting else ()
                    ),
                    deterministic_checks=tuple(checks),
                )
            )
            linked = tuple(
                link for link in links if link.atom_id == atom.atom_id
            )
            coverages.append(
                AtomCoverage(
                    atom_id=atom.atom_id,
                    source_hit=bool(linked or relevant),
                    reranker_hit=any(
                        link.chunk_id in reranked_ids for link in linked
                    )
                    or any(item.chunk_id in reranked_ids for item in relevant),
                    candidate_group_ids=tuple(
                        dict.fromkeys(
                            group_id
                            for item in relevant
                            if isinstance(
                                group_id := dict(item.metadata).get(
                                    "evidence_group_id"
                                ),
                                str,
                            )
                        )
                    ),
                    status=status,
                    reason_codes=(
                        ("CONTRADICTORY_SOURCES",)
                        if status is AtomStatus.CONTRADICTORY
                        else ("DIRECT_SUPPORT",)
                        if status is AtomStatus.SUPPORTED
                        else ("RELATED_EVIDENCE_ONLY",)
                        if status is AtomStatus.PARTIAL
                        else ("NO_ATOM_EVIDENCE",)
                    ),
                )
            )
        all_qualifications = tuple(
            qualification
            for per_atom_qualifications in qualifications_by_atom.values()
            for qualification in per_atom_qualifications.values()
        )
        alignment_reasons = Counter(
            code
            for alignments in alignments_by_atom.values()
            for alignment in alignments
            for code in alignment.reason_codes
        )
        ownership_summary = {
            "root_source_hit": any(link.atom_id is None for link in links),
            "per_atom_source_hit": {
                coverage.atom_id: coverage.source_hit for coverage in coverages
            },
            "retrieval_relevant_count": sum(
                item.retrieval_relevant for item in all_qualifications
            ),
            "ownership_qualified_count": sum(
                item.target_owned for item in all_qualifications
            ),
            "publishable_support_count": sum(
                len(support.supporting_support_ids) for support in supports
            ),
            "evidence_present_but_rejected": sum(
                support.status is not AtomStatus.SUPPORTED
                and any(
                    item.retrieval_relevant
                    for item in qualifications_by_atom[support.atom_id].values()
                )
                for support in supports
            ),
            "alignment_reason_distribution": dict(alignment_reasons),
            "direct_root_rescue_count": sum(
                item.publishable
                and item.support_mode is not None
                and item.support_mode.value == "DIRECT_ROOT_SPAN"
                for item in all_qualifications
            ),
            "direct_atom_rescue_count": sum(
                item.publishable
                and item.support_mode is not None
                and item.support_mode.value == "DIRECT_ATOM_SPAN"
                for item in all_qualifications
            ),
            "constraint_failure_count": sum(
                not item.constraints_supported for item in all_qualifications
            ),
            "relation_failure_count": sum(
                not item.relation_supported for item in all_qualifications
            ),
        }
        if trace_id is not None:
            self._record(
                trace_id,
                "atom_evidence_audit",
                {"atoms": atom_evidence_audit},
            )
            self._record(trace_id, "ownership_summary", ownership_summary)
        return (
            AtomSupportMatrix(atoms=tuple(supports)),
            evidence,
            tuple(coverages),
            tuple(atom_candidate_membership),
        )

    def _corrective_retrieval(  # noqa: PLR0913
        self,
        *,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        groups: tuple[GroupCandidate, ...],
        links: tuple[AtomCandidateLink, ...],
        matrix: AtomSupportMatrix,
        query_plan: QueryPlan,
    ) -> PerAtomCorrectionOutcome:
        """委托一次按 Atom 的结构纠错，不执行全章节回读。"""
        return correct_per_atom(
            source=self._source,
            snapshot=snapshot,
            candidates=candidates,
            groups=groups,
            links=links,
            matrix=matrix,
            query_plan=query_plan,
            policy=self._policy,
        )

    def _rank_and_select(  # noqa: PLR0913, PLR0915
        self,
        *,
        request: SearchRequest,
        snapshot: ActiveRevisionQuerySnapshot,
        analysis: QueryAnalysis,
        plan: RetrievalPlan,
        channel_hits: dict[str, tuple[ChannelHit, ...]],
        selected_slot: str | None,
        trace_id: str,
        provider_calls: list[ProviderCall],
        degraded: list[str],
        stage_timings: list[StageTiming],
        retrieval_phase: str,
        atom_required_ids: tuple[str, ...] = (),
        pre_fused: tuple[FusedCandidate, ...] | None = None,
        rerank_query: str | None = None,
    ) -> _SelectionOutcome:
        """融合、重排并按同一个语义对象选择证据。

        Args:
            request: 原始用户查询与访问范围。
            snapshot: 本次请求固定的 Active Revision。
            analysis: 原问或已接受改写对应的共享分析。
            plan: 与 analysis 同步生成的检索计划。
            channel_hits: 当前有界通道候选。
            selected_slot: 本请求唯一 Dense slot。
            trace_id: 脱敏追踪标识。
            provider_calls: 汇总真实调用的可变列表。
            degraded: 汇总稳定降级码的可变列表。
            stage_timings: 汇总实际耗时的可变列表。
            retrieval_phase: original 或 rewrite，用于区分有界补召回。
            atom_required_ids: 每个原子初召回中需要保留的候选身份。
            pre_fused: 已完成 Root/Atom 两级融合的候选，可跳过全局 RRF。
            rerank_query: 一次统一重排使用的原问与原子关系。

        Returns:
            最终融合、重排、扩展、证据和置信结果。

        """
        started = perf_counter()
        if len(channel_hits) > self._policy.max_channels:
            raise ValueError("检索实际通道数超过 P07 policy。")
        excluded_documents = frozenset(snapshot.excluded_document_ids)
        filtered_channels = {
            name: tuple(
                hit for hit in hits if hit.document_id not in excluded_documents
            )
            for name, hits in channel_hits.items()
        }
        channel_hits.clear()
        channel_hits.update(filtered_channels)
        if pre_fused is None:
            structural_closure_ids = tuple(
                dict.fromkeys(
                    (
                        *_required_structural_candidate_ids(channel_hits),
                        *(item for item in atom_required_ids if item),
                    )
                )
            )
            fused = reciprocal_rank_fusion(
                channel_hits,
                expected_revision_id=snapshot.revision.index_revision_id,
                k=self._policy.rrf_k,
                limit=self._policy.fusion_candidate_limit,
                required_candidate_ids=frozenset(structural_closure_ids),
            )
        else:
            fused = tuple(
                candidate
                for candidate in pre_fused
                if candidate.document_id not in excluded_documents
            )
            fused_ids = {candidate.chunk_id for candidate in fused}
            structural_closure_ids = tuple(
                candidate_id
                for candidate_id in atom_required_ids
                if candidate_id in fused_ids
            )
        self._record(
            trace_id,
            "fuse",
            {
                "pass": retrieval_phase,
                "candidate_count": len(fused),
                "rrf_k": self._policy.rrf_k,
                "rank_contributions": [
                    [
                        [
                            contribution.channel,
                            contribution.rank,
                            contribution.contribution,
                        ]
                        for contribution in candidate.contributions
                    ]
                    for candidate in fused
                ],
            },
        )
        _finish_timing(stage_timings, f"{retrieval_phase}_retrieve", started)
        hydration_started = perf_counter()
        hydrated = self._hydrator.hydrate(snapshot, fused)
        _finish_timing(
            stage_timings,
            f"{retrieval_phase}_sqlite_hydration",
            hydration_started,
        )
        self._record(
            trace_id,
            "hydrate",
            {"pass": retrieval_phase, "candidate_count": len(hydrated)},
        )
        rank_started = perf_counter()
        resolved_query = (
            rerank_query or analysis.resolved_query or analysis.normalized_query
        )
        reranked = self._reranker.rerank(
            resolved_query,
            hydrated,
            self._egress,
            self._policy,
            enabled=plan.use_reranker,
            # 生成证据需要保留重排池中的低位候选；用户请求的返回条数
            # 仍由后续证据装配预算控制，不能在此提前截断来源跨度。
            result_limit=max(
                request.limit,
                len(structural_closure_ids),
                self._policy.rerank_candidate_limit,
            ),
            required_candidate_ids=frozenset(structural_closure_ids),
        )
        expansion = self._neighbors.expand(
            snapshot,
            reranked.candidates,
            plan.neighbor_mode,
            self._policy,
            source_qualifier=analysis.semantics.source_qualifier,
        )
        selected_groups: tuple[GroupCandidate, ...] = ()
        correction_groups: tuple[GroupCandidate, ...] = ()
        if self._policy.evidence_group_mode != "off":
            group_started = perf_counter()
            structural = self._close_structural_context(
                snapshot, expansion.candidates
            )
            group_inputs = tuple(
                {
                    item.hydrated.chunk.chunk_id: item
                    for item in (
                        *expansion.candidates,
                        *structural.candidates,
                    )
                }.values()
            )
            groups = rank_evidence_groups(
                build_evidence_groups(
                    group_inputs,
                    max_groups=self._policy.rerank_candidate_limit,
                    max_member_chunks=self._policy.group_member_chunk_limit,
                    rerank_text_char_limit=self._policy.rerank_text_char_limit,
                )
            )
            rejected: tuple[tuple[str, str], ...] = ()
            if self._policy.evidence_group_mode == "active":
                packing = pack_evidence_groups_with_diagnostics(
                    groups,
                    token_budget=self._policy.group_retrieval_token_budget,
                    max_groups=self._policy.rerank_candidate_limit,
                    max_chunks=self._policy.group_retrieval_chunk_limit,
                    per_document_cap=self._policy.per_document_cap,
                    per_section_cap=self._policy.per_section_cap,
                )
                selected_groups = packing.selected
                correction_groups = (
                    *packing.selected,
                    *packing.incomplete,
                )
                rejected = packing.rejected
                selected_members = (
                    _flatten_evidence_groups(selected_groups)
                    if requires_complete_evidence_group(
                        analysis.semantics.answer_type
                    )
                    else ()
                )
                expanded_by_id = {
                    item.hydrated.chunk.chunk_id: item
                    for item in expansion.candidates
                }
                for member in selected_members:
                    # 原召回及章节扩展身份优先；闭合组只补尚未出现的成员。
                    expanded_by_id.setdefault(
                        member.hydrated.chunk.chunk_id, member
                    )
                expansion = ExpansionOutcome(
                    tuple(expanded_by_id.values()),
                    tuple(
                        dict.fromkeys(
                            (
                                *expansion.degraded_reason_codes,
                                *structural.degraded_reason_codes,
                            )
                        )
                    ),
                )
            elapsed_ms = (perf_counter() - group_started) * 1000
            selected_ids = {group.group_id for group in selected_groups}
            rejected_by_id = dict(rejected)
            drop_reasons = {
                "INCOMPLETE_STRUCTURE": "GROUP_INCOMPLETE",
                "GROUP_EXCEEDS_BUDGET": "GROUP_TOKEN_BUDGET",
                "CHUNK_LIMIT": "GROUP_TOKEN_BUDGET",
                "GROUP_LIMIT": "GROUP_COUNT_CAP",
                "GROUP_DUPLICATE": "GROUP_DUPLICATE",
                "GROUP_DOCUMENT_CAP": "GROUP_DOCUMENT_CAP",
                "GROUP_SECTION_CAP": "GROUP_SECTION_CAP",
            }
            self._record(
                trace_id,
                "evidence_group_build",
                {
                    "pass": retrieval_phase,
                    "mode": self._policy.evidence_group_mode,
                    "group_count": len(groups),
                    "group_type_counts": tuple(
                        (
                            kind,
                            sum(
                                group.group.kind.value == kind
                                for group in groups
                            ),
                        )
                        for kind in sorted(
                            {group.group.kind.value for group in groups}
                        )
                    ),
                    "complete_group_count": sum(
                        group.complete for group in groups
                    ),
                    "incomplete_group_count": sum(
                        not group.complete for group in groups
                    ),
                    "members_per_group": tuple(
                        len(group.members) for group in groups
                    ),
                    "build_elapsed_ms": round(elapsed_ms, 3),
                    "selected_group_count": len(selected_groups),
                    "dropped_group_reasons": rejected,
                    "group_diagnostics": tuple(
                        {
                            "group_id": group.group_id,
                            "group_type": group.group.kind.value,
                            "member_count": len(group.members),
                            "complete": group.complete,
                            "completeness_reason": (
                                ";".join(group.group.incomplete_reasons)
                                if group.group.incomplete_reasons
                                else "COMPLETE"
                            ),
                            "best_rank": min(
                                group.group.member_ranks, default=0
                            ),
                            "selected": group.group_id in selected_ids,
                            "drop_reason": (
                                drop_reasons.get(
                                    rejected_by_id.get(group.group_id, "")
                                )
                                if self._policy.evidence_group_mode == "active"
                                else "SHADOW_ONLY"
                            ),
                            "token_cost": group.token_cost,
                        }
                        for group in groups
                    ),
                },
            )
            _finish_timing(
                stage_timings,
                f"{retrieval_phase}_evidence_group_build",
                group_started,
            )
        provider_calls.extend(reranked.provider_calls)
        self._record(
            trace_id,
            "rerank",
            {
                "pass": retrieval_phase,
                "mode": reranked.mode,
                "reason_code": reranked.reason_code,
                "candidate_count": len(reranked.candidates),
                "candidate_chunk_ids": tuple(
                    item.hydrated.chunk.chunk_id
                    for item in reranked.candidates
                ),
            },
        )
        degraded.extend(expansion.degraded_reason_codes)
        self._record(
            trace_id,
            "expand_neighbors",
            {
                "pass": retrieval_phase,
                "neighbor_mode": plan.neighbor_mode,
                "candidate_count": len(expansion.candidates),
                "candidate_chunk_ids": tuple(
                    item.hydrated.chunk.chunk_id
                    for item in expansion.candidates
                ),
                "predecessor_chunk_ids": tuple(
                    item.hydrated.chunk.chunk_id
                    for item in expansion.candidates
                    if item.expansion_reason == "SECTION_PREDECESSOR"
                ),
                "reason_codes": expansion.degraded_reason_codes,
            },
        )
        vector_space = (
            snapshot.topology.slot(selected_slot).vector_space_identity
            if selected_slot is not None
            else None
        )
        evidence_selection = self._evidence.assemble_sets(
            expansion.candidates,
            self._policy,
            include_model_candidates=True,
            groups=(
                selected_groups
                if self._policy.evidence_group_mode == "active"
                else None
            ),
            context=EvidenceSelectionContext(
                analysis=analysis,
                query_kind=plan.query_kind,
                rerank_mode=reranked.mode,
                selected_slot=selected_slot,
                selected_vector_space=vector_space,
            ),
        )
        evidence = evidence_selection.answer_support_set
        self._record(
            trace_id,
            "assemble_evidence",
            {
                "pass": retrieval_phase,
                "retrieval_candidate_count": len(
                    evidence_selection.retrieval_candidates
                ),
                "model_evidence_candidate_count": len(
                    evidence_selection.model_evidence_candidates
                ),
                "answer_support_count": len(evidence),
                "ambiguous_support": evidence_selection.ambiguous,
                "rejected_candidate_reasons": (
                    evidence_selection.rejected_candidate_reasons
                ),
            },
        )
        confidence = self._confidence.evaluate(
            analysis,
            plan.query_kind,
            expansion.candidates,
            evidence,
            tuple(degraded),
            policy=self._policy,
            rerank_mode=reranked.mode,
            selected_vector_space=vector_space,
        )
        if evidence_selection.ambiguous:
            confidence = confidence.model_copy(
                update={
                    "status": ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION,
                    "score": 0.0,
                    "reason_codes": tuple(
                        dict.fromkeys(
                            (
                                *confidence.reason_codes,
                                "AMBIGUOUS_SAME_TARGET_ACROSS_DOCUMENTS",
                            )
                        )
                    ),
                }
            )
        _finish_timing(
            stage_timings,
            f"{retrieval_phase}_rank_and_evidence",
            rank_started,
        )
        self._record(
            trace_id,
            "confidence",
            {
                "pass": retrieval_phase,
                "status": confidence.status.value,
                "score": confidence.score,
            },
        )
        return _SelectionOutcome(
            fused=fused,
            reranked=reranked,
            expansion=expansion,
            evidence=evidence,
            model_evidence_candidates=(
                evidence_selection.model_evidence_candidates
            ),
            evidence_decisions=evidence_selection.rejected_candidate_reasons,
            ambiguous_support=evidence_selection.ambiguous,
            confidence=confidence,
            groups=correction_groups,
        )

    def _validate_cached_sources(
        self,
        result: SearchAnswerResult,
        request: SearchRequest,
        snapshot: ActiveRevisionQuerySnapshot,
    ) -> None:
        """缓存正文返回前回读当前 canonical 身份和删除状态。"""
        items: tuple[EvidenceItem | RelatedContent, ...] = (
            *result.evidence,
            *result.related_contents,
        )
        rows = self._source.hydrate_chunks(
            snapshot, tuple(dict.fromkeys(item.chunk_id for item in items))
        )
        by_id = {row.chunk.chunk_id: row for row in rows}
        for item in items:
            row = by_id.get(item.chunk_id)
            if row is None:
                raise IndexCorrupt(
                    "缓存原文已不可用。", stage="retrieval.cache"
                )
            validate_candidate(
                RankedChunk(hydrated=row, fusion_rank=1),
                request,
                snapshot.revision.index_revision_id,
            )
            if (item.document_id, item.document_version_id) != (
                row.chunk.version.document_id,
                row.chunk.version.document_version_id,
            ):
                raise IndexCorrupt(
                    "缓存文档版本失配。", stage="retrieval.cache"
                )
            for span in item.source_spans:
                if not any(
                    (
                        _formal_span_is_current(row.chunk, original, span, item)
                        if isinstance(item, EvidenceItem)
                        else _span_is_current(original, span)
                    )
                    for original in row.chunk.source_spans
                ):
                    raise IndexCorrupt(
                        "缓存原文范围失配。", stage="retrieval.cache"
                    )
            if isinstance(item, RelatedContent) and item.excerpt != "".join(
                row.chunk.citation_text[
                    span.chunk_start_char : span.chunk_end_char
                ]
                for span in item.source_spans
            ):
                raise IndexCorrupt(
                    "缓存原文内容失配。", stage="retrieval.cache"
                )

    def _validate_stream_sources(
        self,
        evidence: tuple[EvidenceItem, ...],
        request: SearchRequest,
        snapshot: ActiveRevisionQuerySnapshot,
    ) -> None:
        """每次流式发布前回读当前文档状态和不可变来源范围。"""
        rows = self._source.hydrate_chunks(
            snapshot,
            tuple(dict.fromkeys(item.chunk_id for item in evidence)),
        )
        by_id = {row.chunk.chunk_id: row for row in rows}
        for item in evidence:
            row = by_id.get(item.chunk_id)
            if row is None:
                raise IndexCorrupt(
                    "回答来源已不可用。",
                    stage="answer.source_recheck",
                )
            validate_candidate(
                RankedChunk(hydrated=row, fusion_rank=1),
                request,
                snapshot.revision.index_revision_id,
            )
            if (item.document_id, item.document_version_id) != (
                row.chunk.version.document_id,
                row.chunk.version.document_version_id,
            ):
                raise IndexCorrupt(
                    "回答来源版本已失配。",
                    stage="answer.source_recheck",
                )
            if any(
                not any(
                    _formal_span_is_current(row.chunk, original, span, item)
                    for original in row.chunk.source_spans
                )
                for span in item.source_spans
            ):
                raise IndexCorrupt(
                    "回答来源范围已失配。",
                    stage="answer.source_recheck",
                )

    def _record(
        self, trace_id: str, stage: str, attributes: dict[str, object]
    ) -> None:
        normalized = json.loads(json.dumps(attributes, ensure_ascii=False))
        self._trace.record(
            TraceEvent(
                trace_id=trace_id,
                event_name=f"retrieval.{stage}",
                occurred_at=datetime.now(UTC),
                attributes=freeze_json_object(normalized),
            )
        )

    def _record_shortcut_trace(
        self, trace_id: str, result: SearchAnswerResult, origin: str
    ) -> None:
        """缓存和目录早退也记录真实的本请求 0 次模型调用。"""
        self._record(
            trace_id,
            "query_plan",
            {
                "planner_called": False,
                "planner_protocol": "NONE",
                "planner_schema_revision": QUERY_PLAN_SCHEMA_REVISION,
                "planner_transport_timeout_ms": 0,
                "planner_latency_ms": 0,
                "planner_input_tokens": 0,
                "planner_output_tokens": 0,
                "planner_finish_reason": None,
                "planner_failure_category": None,
                "planner_fallback_mode": None,
                "atom_count": 1,
                "shortcut_origin": origin,
            },
        )
        self._record(
            trace_id,
            "embedding_accounting",
            {
                "query_embedding_provider_call_count": 0,
                "query_embedding_batch_size": 0,
                "query_embedding_slot_id": None,
                "query_embedding_latency_ms": 0,
                "shortcut_origin": origin,
            },
        )
        self._record(
            trace_id,
            "ownership_summary",
            {
                "root_source_hit": bool(
                    result.evidence or result.catalog_citations
                ),
                "per_atom_source_hit": {
                    "A1": bool(result.evidence or result.catalog_citations)
                },
                "retrieval_relevant_count": len(result.evidence),
                "ownership_qualified_count": len(result.evidence),
                "publishable_support_count": len(result.evidence),
                "evidence_present_but_rejected": 0,
                "alignment_reason_distribution": {},
                "direct_root_rescue_count": 0,
                "direct_atom_rescue_count": 0,
                "constraint_failure_count": 0,
                "relation_failure_count": 0,
                "shortcut_origin": origin,
            },
        )
        support_ids = tuple(item.support_id for item in result.evidence)
        if origin == "CACHE_REPLAY":
            self._record(
                trace_id,
                "generate",
                {
                    "mode": result.generation_mode,
                    "answer_path": origin,
                    "generation_called": False,
                    "extractive_fallback_used": False,
                    "final_coverage_by_atom": {"A1": "NOT_OBSERVED"},
                    "reason_code": result.generation_reason_code,
                    "provider_call_count_by_operation": (
                        _provider_call_count_by_operation(())
                    ),
                    "provider_call_count_observation_status": (
                        "CAPTURED_PROVIDER_CALLS"
                    ),
                    "provider_calls": [],
                },
            )
        self._record(
            trace_id,
            "claim_publication",
            {
                "answer_path": origin,
                "generation_called": False,
                "extractive_fallback_used": False,
                "final_coverage_by_atom": {"A1": "NOT_OBSERVED"},
                "generated_claim_count": 0,
                "accepted_claim_count": 0,
                "published_claim_count": 0,
                "accepted_support_ids": (),
                "published_support_ids": support_ids,
                "published_quote_sha256s": tuple(
                    hashlib.sha256(
                        item.citation_text.encode("utf-8")
                    ).hexdigest()
                    for item in result.evidence
                ),
                "claim_rejection_code_distribution": {},
                "generation_gap_count": 0,
                "final_atom_coverage": (),
                "false_limited_detected": False,
                "shortcut_origin": origin,
            },
        )


def _provider_call_count_by_operation(
    calls: tuple[ProviderCall, ...] | list[ProviderCall],
) -> dict[str, int]:
    """按实际记录汇总；旧 embedding 操作在检索请求中即查询向量。"""
    counts: Counter[str] = Counter(
        dict.fromkeys(
            (
                "embedding.query",
                "reranking",
                "generation",
                "query.interpret",
                "query.rewrite",
                "image.ocr.verify",
            ),
            0,
        )
    )
    for call in calls:
        operation = (
            "embedding.query"
            if call.operation == "embedding"
            else call.operation
        )
        counts[operation] += call.call_count
    return dict(counts)


def _required_structural_candidate_ids(
    channel_hits: dict[str, tuple[ChannelHit, ...]],
) -> tuple[str, ...]:
    """保留完整阶段组、表格行及每类首个强结构候选到证据验证。"""
    # 一个 canonical 表格行可能被 Chunker 拆成角色、表头和多个职责片段。
    # EvidenceAssembler 才能根据真实 table 坐标判断哪些片段属于同一行；
    # 因此重排前必须保留全部强表格候选，不能只保留首个命中。
    closure_types = {
        "STRUCTURAL_STAGE_HEADING",
        "STRUCTURAL_GLOSSARY_ROW",
        "STRUCTURAL_TABLE_ROW",
    }
    singleton_types = {
        "STRUCTURAL_SECTION_HEADING_BODY",
        "STRUCTURAL_CONTIGUOUS_LIST",
    }
    selected: list[str] = []
    seen_singletons: set[str] = set()
    for name, hits in channel_hits.items():
        if not name.startswith("structural"):
            continue
        for hit in hits:
            match_type = hit.match_type or ""
            if match_type in closure_types:
                selected.append(hit.chunk_id)
            elif (
                match_type in singleton_types
                and match_type not in seen_singletons
            ):
                selected.append(hit.chunk_id)
                seen_singletons.add(match_type)
    return tuple(dict.fromkeys(selected))


def _formal_span_is_current(
    chunk: Chunk,
    original: SourceSpan,
    span: SourceSpan,
    item: EvidenceItem,
) -> bool:
    """把正式引用的相对坐标投影回 canonical 原文并逐字复核。

    Args:
        chunk: 当前索引中的 canonical chunk。
        original: 当前 chunk 持有的原始来源范围。
        span: Evidence 中相对于引用文本的来源范围。
        item: 待复核的 Evidence。

    Returns:
        来源身份、连续范围和引用原文是否仍与当前索引一致。

    """
    quote = item.citation_text
    relative_updates = {
        "chunk_start_char": 0,
        "chunk_end_char": len(quote),
    }
    if original.source_start_char is None:
        raw_quote = chunk.citation_text[
            original.chunk_start_char : original.chunk_end_char
        ]
        return (
            original.source_end_char is None
            and span == original.model_copy(update=relative_updates)
            and quote in {raw_quote, raw_quote.strip()}
        )
    if (
        original.source_end_char is None
        or span.source_start_char is None
        or span.source_end_char is None
    ):
        return False
    source_start = span.source_start_char
    source_end = source_start + len(quote)
    canonical_start = (
        original.chunk_start_char + source_start - original.source_start_char
    )
    canonical_end = canonical_start + len(quote)
    expected = original.model_copy(
        update={
            **relative_updates,
            "source_start_char": source_start,
            "source_end_char": source_end,
        }
    )
    return (
        span == expected
        and original.source_start_char
        <= source_start
        < source_end
        <= original.source_end_char
        and original.chunk_start_char
        <= canonical_start
        < canonical_end
        <= original.chunk_end_char
        and chunk.citation_text[canonical_start:canonical_end] == quote
    )


def _span_is_current(original: SourceSpan, span: SourceSpan) -> bool:
    if original == span:
        return True
    return (
        original.span_type == span.span_type
        and original.node_id == span.node_id
        and original.source_anchor == span.source_anchor
        and original.is_citable == span.is_citable
        and original.is_repeated == span.is_repeated
        and original.chunk_start_char
        <= span.chunk_start_char
        < span.chunk_end_char
        <= original.chunk_end_char
        and original.source_start_char is not None
        and span.source_start_char
        == original.source_start_char
        + span.chunk_start_char
        - original.chunk_start_char
        and span.source_end_char
        == original.source_start_char
        + span.chunk_end_char
        - original.chunk_start_char
    )


def _circuit_trace(
    snapshots: tuple[CircuitSnapshot, ...],
) -> list[dict[str, object]]:
    return [
        {
            "provider": snapshot.key.provider_id,
            "operation": snapshot.key.operation,
            "model": snapshot.key.model,
            "state": snapshot.state.value,
            "failures": snapshot.consecutive_failures,
            "recoveries": snapshot.recovery_successes,
            "reason_code": snapshot.reason_code,
        }
        for snapshot in snapshots
    ]


def _finish_timing(
    timings: list[StageTiming], stage: str, started: float
) -> float:
    finished = perf_counter()
    timings.append(
        StageTiming(stage=stage, elapsed_ms=(finished - started) * 1000.0)
    )
    return finished


def _raise_if_cancelled(
    cancellation: CancellationPort | None,
    provider_calls: list[ProviderCall] | tuple[ProviderCall, ...] = (),
) -> None:
    """在下一阶段或持久副作用前停止已经取消的查询。"""
    if cancellation is not None and cancellation.is_cancelled():
        raise QueryCancelled(
            "QUERY_CANCELLED",
            provider_calls=tuple(provider_calls),
        )


def _emit_stage(
    callback: Callable[[str, dict[str, object]], None] | None,
    name: str,
    attributes: dict[str, object],
    provider_calls: list[ProviderCall] | tuple[ProviderCall, ...],
) -> None:
    """发布阶段时保留回调竞态前已经发生的 Provider 调用。"""
    if callback is None:
        return
    try:
        callback(name, attributes)
    except QueryCancelled as error:
        error.provider_calls = (*provider_calls, *error.provider_calls)
        raise
    except RagError as error:
        error.provider_calls = (*provider_calls, *error.provider_calls)
        raise


def _validate_stream_final_sources(
    service: RetrievalService,
    evidence: tuple[EvidenceItem, ...],
    request: SearchRequest,
    snapshot: ActiveRevisionQuerySnapshot,
    provider_calls: list[ProviderCall] | tuple[ProviderCall, ...],
) -> None:
    """在 final 边界复核冻结来源，并保留已经发生的调用账。"""
    try:
        service._validate_stream_sources(evidence, request, snapshot)
    except RagError as error:
        error.provider_calls = (*provider_calls, *error.provider_calls)
        raise


def _emit_final(
    callback: Callable[[SearchAnswerResult], None],
    result: SearchAnswerResult,
    provider_calls: list[ProviderCall] | tuple[ProviderCall, ...],
) -> None:
    """发布唯一 final，并把发布边界故障绑定到实际调用账。"""
    try:
        callback(result)
    except QueryCancelled as error:
        error.provider_calls = (*provider_calls, *error.provider_calls)
        raise
    except RagError as error:
        error.provider_calls = (*provider_calls, *error.provider_calls)
        raise


def _published_evidence(
    candidates: tuple[EvidenceItem, ...],
    support_ids: tuple[str, ...],
    ocr_verification_states: tuple[tuple[str, OcrVerificationState], ...] = (),
) -> tuple[EvidenceItem, ...]:
    """按已验证 claim 的 Support ID 投影实际发布引用。"""
    if not support_ids:
        return ()
    by_id = {item.evidence_id: item for item in candidates}
    if any(support_id not in by_id for support_id in support_ids):
        raise IndexCorrupt(
            "已验证回答引用了不存在的模型证据。",
            stage="answer.publish",
        )
    states = dict(ocr_verification_states)
    return tuple(
        by_id[support_id].model_copy(
            update={"ocr_verification_state": states.get(support_id)}
        )
        for support_id in support_ids
    )


def _configured_generation_blocker(
    context: QueryDataPlaneContext,
) -> str | None:
    """返回已配置远程生成未能挂载时的稳定阻断原因。

    Args:
        context: 组合根在请求开始时冻结的模型、授权和预算状态。

    Returns:
        已配置但被阻断时的具体原因；未配置或可用时为空。

    """
    # OCR 等非回答用途会共享统一模型状态与语料授权字段；只有回答
    # Provider 或模型身份实际存在时，才允许这些状态阻断 generation。
    if (
        context.generation_provider_id is None
        and context.generation_model is None
    ):
        return None
    if context.model_configuration_state == "NOT_CONFIGURED":
        return None
    if context.model_configuration_state == "INVALID":
        return "CONFIGURATION_REQUIRED"
    if context.corpus_authorization_state not in {
        "APPROVED",
        "NOT_REQUIRED",
    } or context.model_authorization_state not in {
        "APPROVED",
        "NOT_REQUIRED",
    }:
        return next(
            (
                reason
                for reason in context.fallback_reason_codes
                if reason
                not in {
                    "NO_ACTIVE_RETRIEVAL_PROFILE",
                    "DETERMINISTIC_EMBEDDING",
                }
            ),
            "DATA_EGRESS_NOT_AUTHORIZED",
        )
    if context.budget_state in {"EXHAUSTED", "BLOCKED"}:
        return "BLOCKED_BUDGET"
    return None


def _generation_unavailable_reason(
    context: QueryDataPlaneContext,
) -> str:
    """为未挂载远程生成器的本地路径给出真实原因。

    Args:
        context: 当前请求的数据面上下文。

    Returns:
        明确阻断原因；没有配置模型时返回稳定的未配置原因。

    """
    return _configured_generation_blocker(context) or "GENERATOR_NOT_CONFIGURED"


def _model_capability_status(  # noqa: PLR0911
    context: QueryDataPlaneContext,
    reason: str | None,
) -> tuple[ConfidenceStatus, str] | None:
    """在有模型候选但无本地支持时投影能力阻断终态。

    Args:
        context: 请求开始时冻结的配置、授权和预算状态。
        reason: 实际生成尝试或未挂载生成器的稳定原因。

    Returns:
        能力确实被阻断时的公开状态与原因；模型正常拒答或输出校验失败
        时为空，由 Evidence 语义继续保持证据不足。

    """
    blocker = _configured_generation_blocker(context)
    for candidate in dict.fromkeys((reason, blocker)):
        normalized = (candidate or "").upper()
        if "BUDGET" in normalized:
            return (
                ConfidenceStatus.BUDGET_BLOCKED,
                candidate or "BLOCKED_BUDGET",
            )
        if any(
            marker in normalized
            for marker in (
                "CONFIGURATION",
                "CREDENTIAL",
                "AUTHENTICATION",
                "GENERATOR_NOT_CONFIGURED",
            )
        ):
            return (
                ConfidenceStatus.CONFIGURATION_REQUIRED,
                candidate or "CONFIGURATION_REQUIRED",
            )
        if any(
            marker in normalized
            for marker in (
                "POLICY_DENIED",
                "NOT_AUTHORIZED",
                "AUTHORIZATION_DENIED",
                "AUTHORIZATION_REQUIRED",
                "SOURCE_UNAVAILABLE",
                "CORPUS_AUTHORIZATION",
                "CORPUS_MODEL_BINDING_CHANGED",
                "BUSINESS_AUTHORIZATION",
                "BUSINESS_SOURCE",
                "BUSINESS_MODEL_OPERATION",
            )
        ):
            return (
                ConfidenceStatus.POLICY_DENIED,
                candidate or "POLICY_DENIED",
            )
        if any(
            marker in normalized
            for marker in (
                "PROVIDER_UNAVAILABLE",
                "PROVIDER_INVALID_RESPONSE",
                "PROVIDER_RATE_LIMITED",
                "PROVIDER_TIMEOUT",
                "GENERATION_JSON_INVALID",
                "GENERATION_OUTPUT_INVALID",
                "HTTP_429",
                "CONNECT_TIMEOUT",
                "READ_TIMEOUT",
                "UPSTREAM",
            )
        ):
            return (
                ConfidenceStatus.PROVIDER_UNAVAILABLE,
                candidate or "PROVIDER_UNAVAILABLE",
            )
    if blocker is not None:
        # 配置层已经确认远程模型不可用；即使未来出现新的稳定原因码，
        # 也必须按状态投影并终止，不能递归处理同一个未知原因。
        if context.model_configuration_state == "INVALID":
            return ConfidenceStatus.CONFIGURATION_REQUIRED, blocker
        if context.budget_state in {"EXHAUSTED", "BLOCKED"}:
            return ConfidenceStatus.BUDGET_BLOCKED, blocker
        return ConfidenceStatus.POLICY_DENIED, blocker
    if (
        context.model_configuration_state == "NOT_CONFIGURED"
        and context.report_model_capability_blockers
    ):
        return (
            ConfidenceStatus.CONFIGURATION_REQUIRED,
            "CONFIGURATION_REQUIRED",
        )
    return None


def _catalog_citation(document: CatalogDocument) -> CatalogCitation:
    """把已冻结的 canonical 文档元数据投影为目录来源。"""
    metadata = dict(document.metadata)
    department = metadata.get("department_name")
    category = metadata.get("category_path")
    path = metadata.get("source_relative_path")
    return CatalogCitation(
        document_id=document.document_id,
        document_version_id=document.document_version_id,
        chunk_id=document.chunk_id,
        document_title=document.title,
        source_relative_path=path if isinstance(path, str) else None,
        department_name=(department if isinstance(department, str) else None),
        category_path=(
            tuple(item for item in category if isinstance(item, str))
            if isinstance(category, (tuple, list))
            else ()
        ),
    )


def _diagnostics(  # noqa: PLR0913
    *,
    channel_hits: dict[str, tuple[ChannelHit, ...]],
    fused: tuple[FusedCandidate, ...],
    reranked: tuple[RankedChunk, ...],
    expanded: tuple[RankedChunk, ...],
    evidence: tuple[EvidenceItem, ...],
    model_evidence_candidates: tuple[EvidenceItem, ...],
    evidence_decisions: tuple[tuple[str, str], ...],
    answer_published: bool,
    provider_calls: tuple[ProviderCall, ...],
    stage_timings: tuple[StageTiming, ...],
    degraded: tuple[str, ...],
) -> RetrievalDiagnostics:
    call_totals: dict[str, list[int]] = {}
    for call in provider_calls:
        totals = call_totals.setdefault(call.operation, [0, 0])
        totals[0] += call.call_count
        totals[1] += call.retry_count
    selected_evidence_ids = {item.evidence_id for item in evidence}
    rejected_by_chunk = dict(evidence_decisions)
    return RetrievalDiagnostics(
        channel_chunk_ids=tuple(
            (name, tuple(item.chunk_id for item in hits))
            for name, hits in channel_hits.items()
        ),
        fused_chunk_ids=tuple(item.chunk_id for item in fused),
        fusion=tuple(
            DiagnosticFusionItem(
                chunk_id=item.chunk_id,
                rank=rank,
                score=item.score,
                contributions=item.contributions,
            )
            for rank, item in enumerate(fused, start=1)
        ),
        reranked=tuple(
            DiagnosticRerankItem(
                chunk_id=item.hydrated.chunk.chunk_id,
                rank=item.rerank_rank or rank,
                score=item.rerank_score,
            )
            for rank, item in enumerate(reranked, start=1)
        ),
        expanded=tuple(
            DiagnosticExpansionItem(
                chunk_id=item.hydrated.chunk.chunk_id,
                reason=item.expansion_reason,
            )
            for item in expanded
        ),
        model_evidence_candidates=tuple(
            DiagnosticEvidenceItem(
                evidence_id=item.evidence_id,
                chunk_id=item.chunk_id,
                source_ranges=item.source_spans,
                support_status=_diagnostic_support_status(item),
                selected_for_answer=(item.evidence_id in selected_evidence_ids),
                selection_reason=(
                    "ANSWER_SUPPORT_SELECTED"
                    if item.evidence_id in selected_evidence_ids
                    else rejected_by_chunk.get(
                        item.chunk_id,
                        "NOT_PUBLISHED",
                    )
                ),
            )
            for item in model_evidence_candidates
        ),
        evidence=tuple(
            DiagnosticEvidenceItem(
                evidence_id=item.evidence_id,
                chunk_id=item.chunk_id,
                source_ranges=item.source_spans,
                support_status=_diagnostic_support_status(item),
                selected_for_answer=True,
                selection_reason="PUBLISHED_CITATION",
            )
            for item in evidence
        ),
        cited_chunk_ids=(
            tuple(item.chunk_id for item in evidence)
            if answer_published
            else ()
        ),
        provider_calls=tuple(
            ProviderCallCount(
                operation=operation,
                call_count=counts[0],
                retry_count=counts[1],
            )
            for operation, counts in sorted(call_totals.items())
        ),
        provider_call_details=provider_calls,
        stage_timings=stage_timings,
        degraded_reason_codes=degraded,
    )


def _diagnostic_support_status(item: EvidenceItem) -> str | None:
    """提取 Evidence 已持久化的支持状态，不读取或复制正文。

    Args:
        item: 已经过 Evidence 选择的候选或发布证据。

    Returns:
        稳定支持状态；旧 Evidence 没有该元数据时为空。

    """
    support = dict(item.metadata).get("answer_support")
    if not isinstance(support, dict):
        return None
    status = support.get("status")
    return status if isinstance(status, str) else None


def _diagnostics_summary(
    diagnostics: RetrievalDiagnostics,
) -> RetrievalDiagnosticsSummary:
    return RetrievalDiagnosticsSummary(
        channel_count=len(diagnostics.channel_chunk_ids),
        fused_count=len(diagnostics.fused_chunk_ids),
        reranked_count=len(diagnostics.reranked),
        evidence_count=len(diagnostics.evidence),
        provider_call_count=sum(
            item.call_count for item in diagnostics.provider_calls
        ),
        provider_retry_count=sum(
            item.retry_count for item in diagnostics.provider_calls
        ),
        cache_hit=diagnostics.cache_hit,
    )


__all__ = ["RetrievalService"]
