"""P07 Active Snapshot 到模型证据回答的统一同步路由。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import suppress
from copy import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal

from rag_app.application.answering.grounded import GroundedAnsweringService
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.dense import DenseChannel
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.application.retrieval.exact import ExactChannel
from rag_app.application.retrieval.expansion import RuleBasedNormalizer
from rag_app.application.retrieval.filters import apply_candidate_filters
from rag_app.application.retrieval.fusion import reciprocal_rank_fusion
from rag_app.application.retrieval.hydration import CandidateHydrator
from rag_app.application.retrieval.lexical import LexicalChannel
from rag_app.application.retrieval.neighbors import (
    ExpansionOutcome,
    NeighborExpander,
)
from rag_app.application.retrieval.planner import QueryPlanner
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
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import (
    CancellationPort,
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
        self._rewriter: QueryRewritePort | None = None
        self._trace = trace
        self._cache = cache
        # 检索实现演进仅改变 serving/query cache；文档索引与向量语义不变。
        self._serving_fingerprint = canonical_sha256(
            {
                "configured_serving": serving_fingerprint,
                "retrieval_implementation": "v3-07-grounded-answer-v7",
            }
        )
        self._egress = egress_policy
        self._policy = policy or RetrievalPolicy()
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

    def with_generation(
        self,
        generator: GeneratorPort,
        *,
        serving_identity: str,
        interpreter: QueryInterpretPort | None = None,
        rewriter: QueryRewritePort | None = None,
    ) -> RetrievalService:
        """为单次知识库解析创建轻量配置副本，共用原索引与检索通道。

        Args:
            generator: 已绑定知识库与出站授权的生成器。
            serving_identity: 模型及查询策略缓存身份。
            interpreter: 可选的一次结构化问题解释端口。
            rewriter: 可选的一次问题改写端口。

        Returns:
            与原文档向量配置共存的查询服务。

        """
        configured = copy(self)
        configured._grounded = GroundedAnsweringService(generator)
        configured._interpreter = interpreter
        configured._rewriter = rewriter
        descriptor = getattr(
            generator, "descriptor", self._default_generator_descriptor
        )
        configured._data_plane_context = replace(
            self._data_plane_context,
            generation_provider_id=descriptor.name,
            generation_model=descriptor.version,
            interpret_provider_id=(
                descriptor.name if interpreter is not None else None
            ),
            interpret_model=(
                descriptor.version if interpreter is not None else None
            ),
            rewrite_provider_id=(
                descriptor.name if rewriter is not None else None
            ),
            rewrite_model=descriptor.version if rewriter is not None else None,
            model_configuration_state="CONFIGURED",
        )
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
        effective_analysis = analysis
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
            },
        )
        stage_started = _finish_timing(stage_timings, "analyze", stage_started)
        variants = self._expander.expand(analysis)
        self._record(
            trace_id,
            "expand",
            {
                "variant_count": len(variants),
                "variant_kinds": [variant.kind for variant in variants],
            },
        )
        plan = self._planner.plan(
            analysis,
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
        interpret_attempted = False
        interpret_reason = "INTERPRET_NOT_CONFIGURED"
        if self._interpreter is not None:
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
                            "policy": "bounded-interpret-v1",
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
        if self._rewriter is not None and not interpret_attempted:
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
        top_k = dict(plan.channel_top_k)
        channel_hits: dict[str, tuple[ChannelHit, ...]] = {}
        degraded: list[str] = []
        if "exact" in plan.channels:
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
        if "structural" in plan.channels:
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
        if "lexical" in plan.channels:
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
        if "dense" in plan.channels:
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
        )
        fused = selection.fused
        reranked = selection.reranked
        expansion = selection.expansion
        evidence = selection.evidence
        model_evidence_candidates = selection.model_evidence_candidates
        evidence_decisions = selection.evidence_decisions
        confidence = selection.confidence
        _raise_if_cancelled(cancellation, provider_calls)
        if (
            self._rewriter is not None
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
                    effective_analysis.original_query,
                    generation_evidence,
                    confidence,
                    answer_support_set=evidence,
                    analysis=effective_analysis,
                    on_claim=None if on_claim is None else publish_claim,
                    cancellation=cancellation,
                )
                answer = generated.answer
                generation_mode = generated.mode
                generation_reason = generated.reason_code
                provider_calls.extend(generated.calls)
                if answer is not None:
                    published = _published_evidence(
                        generation_evidence,
                        generated.published_support_ids,
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
                "reason_code": generation_reason,
                "provider_calls": [
                    call.model_dump(mode="json")
                    for call in provider_calls
                    if call.operation
                    in {"generation", "query.interpret", "query.rewrite"}
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
            interpret_reason_code=interpret_reason,
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
        rewrite_identity = canonical_sha256(
            {
                "variants": tuple(
                    variant.identity for variant in plan.variants
                ),
                "semantic_policy": "shared-query-semantics-v3-07",
                "rewrite_policy": "bounded-rewrite-v3",
                "answer_support_policy": "minimum-supported-set-v3-07",
                "answer_generation_policy": "model-grounded-claims-v1",
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
                request.text.encode("utf-8")
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
                    "plan_variants": tuple(
                        variant.identity for variant in plan.variants
                    ),
                }
            ),
            cache_schema=self._policy.cache_schema_version,
            limit=request.limit,
            dense_required=request.dense_required,
            generation_behavior=(
                "grounded" if self._grounded is not None else "model_required"
            ),
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

    def _rank_and_select(  # noqa: PLR0913
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
        structural_closure_ids = _required_structural_candidate_ids(
            channel_hits
        )
        fused = reciprocal_rank_fusion(
            channel_hits,
            expected_revision_id=snapshot.revision.index_revision_id,
            k=self._policy.rrf_k,
            limit=self._policy.fusion_candidate_limit,
            required_candidate_ids=frozenset(structural_closure_ids),
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
        resolved_query = analysis.resolved_query or analysis.normalized_query
        reranked = self._reranker.rerank(
            resolved_query,
            hydrated,
            self._egress,
            self._policy,
            enabled=plan.use_reranker,
            result_limit=max(request.limit, len(structural_closure_ids)),
            required_candidate_ids=frozenset(structural_closure_ids),
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
            },
        )
        expansion = self._neighbors.expand(
            snapshot,
            reranked.candidates,
            plan.neighbor_mode,
            self._policy,
            source_qualifier=analysis.semantics.source_qualifier,
        )
        degraded.extend(expansion.degraded_reason_codes)
        self._record(
            trace_id,
            "expand_neighbors",
            {
                "pass": retrieval_phase,
                "candidate_count": len(expansion.candidates),
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
    """正式引用采用引用片段内偏移，来源身份和原文字节仍必须完全匹配。"""
    raw_quote = chunk.citation_text[
        original.chunk_start_char : original.chunk_end_char
    ]
    quote = raw_quote.strip()
    leading_trim = len(raw_quote) - len(raw_quote.lstrip())
    updates: dict[str, int] = {
        "chunk_start_char": 0,
        "chunk_end_char": len(quote),
    }
    if original.source_start_char is not None:
        source_start = original.source_start_char + leading_trim
        updates.update(
            {
                "source_start_char": source_start,
                "source_end_char": source_start + len(quote),
            }
        )
    return (
        span == original.model_copy(update=updates)
        and item.citation_text == quote
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
    return tuple(by_id[support_id] for support_id in support_ids)


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
