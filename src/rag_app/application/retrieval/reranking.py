"""独立 circuit、严格响应校验和稳定 bypass 的 P07 reranking。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rag_app.application.embedding_router import failure_category
from rag_app.application.provider_health import (
    CircuitKey,
    EgressGuard,
    ProviderCircuitBreaker,
)
from rag_app.core.errors import PolicyDenied, ProviderInvalidResponse, RagError
from rag_app.core.models import (
    ProviderCall,
    ProviderFailureCategory,
    RankedChunk,
    RerankItem,
    RerankRequest,
    RetrievalPolicy,
)
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import RerankerPort


@dataclass(frozen=True, slots=True)
class RerankingOutcome:
    """重排或显式旁路后的候选与实际模式。"""

    candidates: tuple[RankedChunk, ...]
    mode: str
    reason_code: str
    provider_calls: tuple[ProviderCall, ...] = ()
    failure_category: ProviderFailureCategory | None = None


class CircuitAwareReranker:
    """Jina rerank circuit 与 embedding circuit 完全独立。"""

    def __init__(
        self,
        reranker: RerankerPort,
        *,
        circuit: ProviderCircuitBreaker | None = None,
    ) -> None:
        self._reranker = reranker
        self._circuit = circuit or ProviderCircuitBreaker()

    def rerank(  # noqa: PLR0913
        self,
        query: str,
        candidates: tuple[RankedChunk, ...],
        egress: EgressPolicy,
        policy: RetrievalPolicy,
        *,
        enabled: bool,
        result_limit: int,
        required_candidate_ids: frozenset[str] = frozenset(),
    ) -> RerankingOutcome:
        """重排 bounded fusion prefix 或稳定保留 RRF 顺序。

        Args:
            query: normalized query。
            candidates: RRF 顺序的 canonical candidates。
            egress: 默认拒绝的 rerank 出网策略。
            policy: rerank 数量、文本和 bypass 策略。
            enabled: Planner 是否要求 rerank。
            result_limit: 用户请求的最终候选数。
            required_candidate_ids: 必须保留到结构证据闭合的候选 ID。

        Returns:
            实际 Provider 或明确 bypass 后的候选与模式。

        """
        limited = candidates[: policy.rerank_candidate_limit]
        output_limit = min(result_limit, len(limited))
        if not enabled or not limited:
            return _bypass(
                _restore_protected(
                    limited[:output_limit],
                    limited,
                    limit=output_limit,
                    must_keep_limit=policy.must_keep_limit,
                    required_candidate_ids=required_candidate_ids,
                ),
                "RERANK_DISABLED_BY_PLAN",
            )
        descriptor = self._reranker.descriptor
        key = CircuitKey(descriptor.name, "reranking", descriptor.version)
        if descriptor.capabilities.permits_network:
            try:
                EgressGuard.require_reranking(egress)
            except PolicyDenied:
                if not policy.bypass_policy_denied:
                    raise
                return _bypass(
                    _restore_protected(
                        limited[:output_limit],
                        limited,
                        limit=output_limit,
                        must_keep_limit=policy.must_keep_limit,
                        required_candidate_ids=required_candidate_ids,
                    ),
                    "RERANK_BYPASSED_POLICY_DENIED",
                )
        if not self._circuit.allow_call(key):
            return _bypass(
                _restore_protected(
                    limited[:output_limit],
                    limited,
                    limit=output_limit,
                    must_keep_limit=policy.must_keep_limit,
                    required_candidate_ids=required_candidate_ids,
                ),
                "RERANK_BYPASSED_CIRCUIT_OPEN",
                category=_circuit_failure_category(
                    self._circuit.snapshot(key).reason_code
                ),
            )
        request = RerankRequest(
            query=query,
            candidates=tuple(
                (
                    item.hydrated.chunk.chunk_id,
                    _bounded_text(item, policy.rerank_text_char_limit),
                )
                for item in limited
            ),
            limit=output_limit,
        )
        try:
            result = self._reranker.rerank(request)
            ordered = _validate_and_order(result.items, limited, output_limit)
        except (RagError, ValueError) as error:
            category = (
                failure_category(error)
                if isinstance(error, RagError)
                else ProviderFailureCategory.RESPONSE_CONTRACT
            )
            self._circuit.record_failure(key, category)
            return _bypass(
                _restore_protected(
                    limited[:output_limit],
                    limited,
                    limit=output_limit,
                    must_keep_limit=policy.must_keep_limit,
                    required_candidate_ids=required_candidate_ids,
                ),
                "RERANK_BYPASSED_PROVIDER_UNAVAILABLE",
                category=category,
            )
        self._circuit.record_success(key)
        protected = _restore_protected(
            ordered,
            limited,
            limit=output_limit,
            must_keep_limit=policy.must_keep_limit,
            required_candidate_ids=required_candidate_ids,
        )
        return RerankingOutcome(
            candidates=tuple(
                item.model_copy(update={"rerank_rank": rank})
                for rank, item in enumerate(protected, start=1)
            ),
            mode=result.mode.value,
            reason_code="RERANK_EXECUTED",
            provider_calls=result.calls,
        )


def _bounded_text(candidate: RankedChunk, limit: int) -> str:
    chunk = candidate.hydrated.chunk
    display_name = candidate.hydrated.display_name
    heading = " / ".join(chunk.heading_path)
    labels = "\n".join(value for value in (display_name, heading) if value)
    value = f"{labels}\n{chunk.citation_text}"
    if len(value) <= limit:
        return value
    head = int(limit * 0.7)
    return f"{value[:head]}\n[…]\n{value[-(limit - head - 5) :]}"


def _validate_and_order(
    items: tuple[RerankItem, ...],
    candidates: tuple[RankedChunk, ...],
    limit: int,
) -> tuple[RankedChunk, ...]:
    expected_ids = {
        item.hydrated.chunk.chunk_id: index
        for index, item in enumerate(candidates)
    }
    if len(items) != limit:
        raise ProviderInvalidResponse(
            "Reranker 返回候选数量不完整。", stage="retrieval.rerank"
        )
    candidate_ids = [item.candidate_id for item in items]
    if len(set(candidate_ids)) != len(candidate_ids) or any(
        item not in expected_ids for item in candidate_ids
    ):
        raise ProviderInvalidResponse(
            "Reranker 返回重复或越界候选。", stage="retrieval.rerank"
        )
    scores = {str(item.candidate_id): float(item.score) for item in items}
    if any(not math.isfinite(score) for score in scores.values()):
        raise ProviderInvalidResponse(
            "Reranker 返回非有限分数。", stage="retrieval.rerank"
        )
    selected = [
        candidate
        for candidate in candidates
        if candidate.hydrated.chunk.chunk_id in scores
    ]
    selected.sort(
        key=lambda candidate: (
            -scores[candidate.hydrated.chunk.chunk_id],
            expected_ids[candidate.hydrated.chunk.chunk_id],
        )
    )
    return tuple(
        candidate.model_copy(
            update={"rerank_score": scores[candidate.hydrated.chunk.chunk_id]}
        )
        for candidate in selected
    )


def _restore_protected(
    selected: tuple[RankedChunk, ...],
    all_candidates: tuple[RankedChunk, ...],
    *,
    limit: int,
    must_keep_limit: int,
    required_candidate_ids: frozenset[str],
) -> tuple[RankedChunk, ...]:
    """先恢复结构闭合成员，再恢复既有 exact must-keep。"""
    result = list(selected[:limit])
    present = {item.hydrated.chunk.chunk_id for item in result}
    required = [
        item
        for item in all_candidates
        if item.hydrated.chunk.chunk_id in required_candidate_ids
    ][:limit]
    required_ids = {item.hydrated.chunk.chunk_id for item in required}
    protected = [
        *required,
        *[
            item
            for item in all_candidates
            if item.must_keep
            and item.hydrated.chunk.chunk_id not in required_ids
        ][:must_keep_limit],
    ]
    protected_ids = {item.hydrated.chunk.chunk_id for item in protected}
    for candidate in protected:
        if candidate.hydrated.chunk.chunk_id in present:
            continue
        replace = next(
            (
                index
                for index in range(len(result) - 1, -1, -1)
                if result[index].hydrated.chunk.chunk_id not in protected_ids
            ),
            None,
        )
        if replace is None:
            break
        present.discard(result[replace].hydrated.chunk.chunk_id)
        result[replace] = candidate
        present.add(candidate.hydrated.chunk.chunk_id)
    return tuple(result)


def _bypass(
    candidates: tuple[RankedChunk, ...],
    reason_code: str,
    *,
    category: ProviderFailureCategory | None = None,
) -> RerankingOutcome:
    return RerankingOutcome(
        candidates=candidates,
        mode=reason_code.casefold(),
        reason_code=reason_code,
        provider_calls=(),
        failure_category=category,
    )


def _circuit_failure_category(
    reason_code: str,
) -> ProviderFailureCategory | None:
    try:
        return ProviderFailureCategory(reason_code.casefold())
    except ValueError:
        return None


__all__ = ["CircuitAwareReranker", "RerankingOutcome"]
