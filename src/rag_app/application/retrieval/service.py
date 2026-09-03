"""P07 Active Snapshot 到 extractive answer 的统一同步路由。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from rag_app.application.answering import ExtractiveAnsweringService
from rag_app.application.embedding_router import search_cache_key
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
from rag_app.application.retrieval.neighbors import NeighborExpander
from rag_app.application.retrieval.planner import QueryPlanner
from rag_app.application.retrieval.reranking import CircuitAwareReranker
from rag_app.core.errors import (
    DenseUnavailable,
    IndexCompatibilityError,
    IndexCorrupt,
    PolicyDenied,
    RagError,
)
from rag_app.core.events import TraceEvent
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ChannelHit,
    CircuitSnapshot,
    ConfidenceStatus,
    RetrievalPolicy,
    SearchAnswerResult,
    SearchRequest,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import (
    EvidenceSourcePort,
    ExactStorePort,
    GeneratorPort,
    LexicalStorePort,
    QueryEmbeddingPort,
    RerankerPort,
    RetrievalCachePort,
    TracePort,
    VectorStorePort,
)


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
    ) -> None:
        self._source = source
        self._exact = ExactChannel(exact_store)
        self._lexical = LexicalChannel(lexical_store)
        self._dense = DenseChannel(query_embedding, vector_store)
        self._reranker = CircuitAwareReranker(reranker)
        self._answering = ExtractiveAnsweringService(generator)
        self._trace = trace
        self._cache = cache
        self._serving_fingerprint = serving_fingerprint
        self._egress = egress_policy
        self._policy = policy or RetrievalPolicy()
        self._analyzer = QueryAnalyzer()
        self._expander = RuleBasedNormalizer()
        self._planner = QueryPlanner()
        self._hydrator = CandidateHydrator(source)
        self._neighbors = NeighborExpander(source)
        self._evidence = EvidenceAssembler()
        self._confidence = ConfidenceEvaluator()

    def search_and_answer(  # noqa: PLR0912, PLR0915
        self, request: SearchRequest
    ) -> SearchAnswerResult:
        """执行一次 revision-sticky、bounded、fail-closed 查询。

        Args:
            request: scope、query、过滤和有限 conversation context。

        Returns:
            实际 route/rerank、证据、置信和 extractive answer。

        Raises:
            IndexNotReady: 没有 Active Revision。
            IndexCorrupt: 通道身份无法 canonical hydrate。
            PolicyDenied: 未支持 filter 或 profile 要求 fail closed。

        """
        trace_id = f"trace_{uuid.uuid4().hex}"
        snapshot = self._source.active_query_snapshot(
            request.scope,
            serving_fingerprint=self._serving_fingerprint,
            retrieval_policy=self._policy,
        )
        self._record(
            trace_id,
            "snapshot",
            {
                "revision_id": snapshot.revision.index_revision_id,
                "index_fingerprint": snapshot.revision.index_fingerprint,
                "serving_fingerprint": snapshot.serving_fingerprint,
            },
        )
        analysis = self._analyzer.analyze(request)
        self._record(
            trace_id,
            "analyze",
            {
                "query_sha256": hashlib.sha256(
                    request.text.encode("utf-8")
                ).hexdigest(),
                "query_length": len(request.text),
                "identifier_count": len(analysis.identifiers),
                "reason_codes": analysis.reason_codes,
            },
        )
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
        top_k = dict(plan.channel_top_k)
        channel_hits: dict[str, tuple[ChannelHit, ...]] = {}
        degraded: list[str] = []
        if "exact" in plan.channels:
            try:
                hits = apply_candidate_filters(
                    self._exact.search(
                        snapshot, analysis, limit=top_k["exact"]
                    ),
                    request,
                )
            except PolicyDenied:
                raise
            # Exact is optional; isolate unexpected store-driver failures.
            except Exception as error:
                degraded.append(f"EXACT_STORE_FAILURE:{type(error).__name__}")
                hits = ()
            channel_hits["exact"] = hits
            self._record(trace_id, "exact", {"hit_count": len(hits)})
        if "lexical" in plan.channels:
            for variant in plan.variants:
                try:
                    hits = apply_candidate_filters(
                        self._lexical.search(
                            snapshot, variant, limit=top_k["lexical"]
                        ),
                        request,
                    )
                except PolicyDenied:
                    raise
                # Lexical is optional; isolate unexpected store-driver failures.
                except Exception as error:
                    degraded.append(
                        f"LEXICAL_STORE_FAILURE:{type(error).__name__}"
                    )
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
        selected_slot: str | None = None
        selected_vector: str | None = None
        route_reason = "DENSE_DISABLED_BY_PLAN"
        if "dense" in plan.channels:
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
                    plan.variants[0].text,
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
        if len(channel_hits) > self._policy.max_channels:
            raise ValueError("检索实际通道数超过 P07 policy。")
        fused = reciprocal_rank_fusion(
            channel_hits,
            expected_revision_id=snapshot.revision.index_revision_id,
            k=self._policy.rrf_k,
            limit=self._policy.fusion_candidate_limit,
        )
        self._record(
            trace_id,
            "fuse",
            {
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
        hydrated = self._hydrator.hydrate(snapshot, fused)
        self._record(trace_id, "hydrate", {"candidate_count": len(hydrated)})
        reranked = self._reranker.rerank(
            analysis.normalized_query,
            hydrated,
            self._egress,
            self._policy,
            enabled=plan.use_reranker,
            result_limit=request.limit,
        )
        self._record(
            trace_id,
            "rerank",
            {
                "mode": reranked.mode,
                "reason_code": reranked.reason_code,
                "candidate_count": len(reranked.candidates),
            },
        )
        rewrite_identity = canonical_sha256(
            tuple(variant.identity for variant in plan.variants)
        )
        cache_key = search_cache_key(
            project_id=request.scope.project_id,
            knowledge_base_id=request.scope.knowledge_base_id,
            active_index_revision_id=snapshot.revision.index_revision_id,
            serving_fingerprint=snapshot.serving_fingerprint,
            selected_embedding_slot=selected_slot or "none",
            rerank_mode=reranked.mode,
            query=request.text,
            metadata_filters=request.metadata_filters,
            access_filters=request.access_filters,
            conversation_identity=analysis.conversation_fingerprint,
            rewrite_identity=rewrite_identity,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._record(trace_id, "cache", {"result": "hit"})
            self._record(trace_id, "complete", {"status": cached.status.value})
            return cached.model_copy(update={"trace_id": trace_id})
        self._record(trace_id, "cache", {"result": "miss"})
        expansion = self._neighbors.expand(
            snapshot, reranked.candidates, plan.neighbor_mode, self._policy
        )
        degraded.extend(expansion.degraded_reason_codes)
        self._record(
            trace_id,
            "expand_neighbors",
            {
                "candidate_count": len(expansion.candidates),
                "reason_codes": expansion.degraded_reason_codes,
            },
        )
        evidence = self._evidence.assemble(expansion.candidates, self._policy)
        self._record(
            trace_id, "assemble_evidence", {"evidence_count": len(evidence)}
        )
        confidence = self._confidence.evaluate(
            analysis,
            plan.query_kind,
            reranked.candidates,
            evidence,
            tuple(degraded),
        )
        self._record(
            trace_id,
            "confidence",
            {"status": confidence.status.value, "score": confidence.score},
        )
        try:
            answer = self._answering.answer(
                analysis.original_query, evidence, confidence
            )
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
        self._record(
            trace_id,
            "generate",
            {"mode": "extractive" if answer is not None else "none"},
        )
        self._record(
            trace_id,
            "validate",
            {"published": answer is not None, "support_count": len(evidence)},
        )
        result = SearchAnswerResult(
            trace_id=trace_id,
            status=confidence.status,
            reason_code=confidence.status.value,
            answer=answer,
            evidence=evidence,
            confidence=confidence,
            query_kind=plan.query_kind,
            active_index_revision_id=snapshot.revision.index_revision_id,
            index_fingerprint=snapshot.revision.index_fingerprint,
            serving_fingerprint=snapshot.serving_fingerprint,
            selected_embedding_slot=selected_slot,
            selected_vector_name=selected_vector,
            route_reason_code=route_reason,
            rerank_execution_mode=reranked.mode,
            generation_mode="extractive" if answer is not None else "none",
            degraded_reason_codes=tuple(dict.fromkeys(degraded)),
            cache_key=cache_key,
        )
        if result.status is ConfidenceStatus.ANSWERABLE:
            self._cache.put(cache_key, result)
        self._record(trace_id, "complete", {"status": result.status.value})
        return result

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


__all__ = ["RetrievalService"]
