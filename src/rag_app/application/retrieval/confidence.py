"""不比较跨 Provider 原始分数的 P07 confidence/refusal。"""

from __future__ import annotations

from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    QueryAnalysis,
    QueryKind,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.query_text import normalize_identifier

_MIN_AMBIGUOUS_EVIDENCE = 2
_MIN_RERANK_MARGIN_ITEMS = 2
_MAX_DENSE_EVIDENCE_RANK = 10


class ConfidenceEvaluator:
    """从可解释 rank、coverage、evidence 和 degraded flags 决策。"""

    def evaluate(  # noqa: PLR0913
        self,
        analysis: QueryAnalysis,
        query_kind: QueryKind,
        candidates: tuple[RankedChunk, ...],
        evidence: tuple[EvidenceItem, ...],
        degraded: tuple[str, ...],
        *,
        policy: RetrievalPolicy | None = None,
        rerank_mode: str = "",
        selected_vector_space: str | None = None,
    ) -> ConfidenceDecision:
        """返回 provisional 特征与稳定拒答状态。

        Args:
            analysis: QueryAnalyzer 的确定性信号。
            query_kind: Planner 的五类查询结果。
            candidates: 已重排或明确 bypass 的候选。
            evidence: 已完成来源验证和预算 packing 的证据。
            degraded: 实际 Provider、Store 或 expansion 降级原因。
            policy: Dense semantic 校准与最小支持策略。
            rerank_mode: 实际重排执行模式。
            selected_vector_space: 当前查询实际使用的向量空间身份。

        Returns:
            不使用跨 Provider raw score 的置信决策。

        """
        resolved_policy = policy or RetrievalPolicy()
        evidence_chunk_ids = {item.chunk_id for item in evidence}
        supported_candidates = tuple(
            item
            for item in candidates
            if item.hydrated.chunk.chunk_id in evidence_chunk_ids
        )
        exact = float(
            any(
                contribution.channel == "exact"
                for item in supported_candidates
                for contribution in item.contributions
            )
        )
        lexical = float(
            any(
                contribution.channel.startswith("lexical")
                for item in supported_candidates
                for contribution in item.contributions
            )
        )
        agreement = float(
            max(
                (len(item.contributions) for item in supported_candidates),
                default=0,
            )
        )
        diversity = float(len({item.document_id for item in evidence}))
        evidence_count = float(len(evidence))
        rank_stability = _rank_stability(supported_candidates)
        rerank_margin = _rerank_margin(supported_candidates)
        identifier_coverage = _identifier_coverage(
            analysis, supported_candidates
        )
        citable_coverage = float(
            bool(evidence)
            and all(
                item.source_spans
                and all(span.is_citable for span in item.source_spans)
                for item in evidence
            )
        )
        metadata_only = bool(evidence) and all(
            "METADATA_ONLY" in item.quality_flags for item in evidence
        )
        dense_only = bool(supported_candidates) and not (exact or lexical)
        dense_allowed = _dense_semantic_allowed(
            resolved_policy,
            supported_candidates,
            rerank_mode=rerank_mode,
            selected_vector_space=selected_vector_space,
        )
        independent_supports = len(evidence_chunk_ids)
        if not evidence:
            status = _empty_status(query_kind, degraded)
            score = 0.0
        elif metadata_only:
            status = ConfidenceStatus.INSUFFICIENT_EVIDENCE
            score = 0.1
        elif dense_only and not dense_allowed:
            status = ConfidenceStatus.INSUFFICIENT_EVIDENCE
            score = 0.15
        elif (
            query_kind is QueryKind.AMBIGUOUS
            and independent_supports
            < max(
                _MIN_AMBIGUOUS_EVIDENCE,
                resolved_policy.minimum_support_items,
            )
        ):
            status = ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION
            score = 0.25
        else:
            status = ConfidenceStatus.ANSWERABLE
            score = min(
                1.0,
                0.35
                + 0.15 * exact
                + 0.1 * min(agreement, 3.0)
                + 0.1 * identifier_coverage
                + 0.1 * citable_coverage
                + 0.05 * rank_stability
                + 0.05 * rerank_margin,
            )
        return ConfidenceDecision(
            status=status,
            score=score,
            reason_codes=(
                "P07_RULE_CONFIDENCE",
                "EVIDENCE_BOUND_CONFIDENCE_V2",
                *(
                    (resolved_policy.dense_semantic_calibration_state,)
                    if dense_only and dense_allowed
                    else ()
                ),
                *(("DEGRADED_RETRIEVAL",) if degraded else ()),
            ),
            feature_values=(
                ("exact_match", exact),
                ("lexical_match", lexical),
                ("channel_agreement", agreement),
                ("rank_stability", rank_stability),
                ("rerank_margin", rerank_margin),
                ("identifier_coverage", identifier_coverage),
                ("citable_span_coverage", citable_coverage),
                ("evidence_count", evidence_count),
                ("source_diversity", diversity),
                ("degraded_count", float(len(degraded))),
                ("independent_support_count", float(independent_supports)),
                ("dense_semantic_allowed", float(dense_allowed)),
            ),
        )


def _dense_semantic_allowed(
    policy: RetrievalPolicy,
    candidates: tuple[RankedChunk, ...],
    *,
    rerank_mode: str,
    selected_vector_space: str | None,
) -> bool:
    if (
        not policy.dense_semantic_enabled
        or selected_vector_space is None
        or selected_vector_space not in policy.dense_calibrated_vector_spaces
        or rerank_mode.casefold().startswith("rerank_")
    ):
        return False
    return all(
        item.rerank_rank is not None
        and item.rerank_rank <= _MAX_DENSE_EVIDENCE_RANK
        and any(
            contribution.channel.startswith("dense:")
            for contribution in item.contributions
        )
        for item in candidates
    )


def _identifier_coverage(
    analysis: QueryAnalysis, candidates: tuple[RankedChunk, ...]
) -> float:
    if not analysis.identifiers:
        return 1.0
    expected = {normalize_identifier(item) for item in analysis.identifiers}
    found = {
        normalize_identifier(identifier)
        for candidate in candidates
        for identifier in candidate.hydrated.chunk.identifiers
    }
    return len(expected & found) / len(expected)


def _empty_status(
    query_kind: QueryKind, degraded: tuple[str, ...]
) -> ConfidenceStatus:
    if query_kind is QueryKind.AMBIGUOUS:
        return ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION
    if any("POLICY_DENIED" in reason for reason in degraded):
        return ConfidenceStatus.POLICY_DENIED
    if any("DENSE_UNAVAILABLE" in reason for reason in degraded):
        return ConfidenceStatus.PROVIDER_UNAVAILABLE
    return ConfidenceStatus.INSUFFICIENT_EVIDENCE


def _rank_stability(candidates: tuple[RankedChunk, ...]) -> float:
    compared = [item for item in candidates if item.rerank_rank is not None]
    if not compared:
        return 0.0
    displacement = 0
    for item in compared:
        rerank_rank = item.rerank_rank
        if rerank_rank is None:
            continue
        displacement += abs(item.fusion_rank - rerank_rank)
    return max(0.0, 1.0 - displacement / (len(compared) ** 2))


def _rerank_margin(candidates: tuple[RankedChunk, ...]) -> float:
    scores = [
        item.rerank_score
        for item in sorted(
            candidates,
            key=lambda item: item.rerank_rank or len(candidates) + 1,
        )
        if item.rerank_score is not None
    ]
    if len(scores) < _MIN_RERANK_MARGIN_ITEMS:
        return 0.0
    return min(
        1.0,
        max(0.0, scores[0] - scores[1]) / max(abs(scores[0]), 1.0),
    )


__all__ = ["ConfidenceEvaluator"]
