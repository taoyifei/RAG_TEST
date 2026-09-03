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
    ) -> RerankingOutcome:
        """重排 bounded fusion prefix 或稳定保留 RRF 顺序。

        Args:
            query: normalized query。
            candidates: RRF 顺序的 canonical candidates。
            egress: 默认拒绝的 rerank 出网策略。
            policy: rerank 数量、文本和 bypass 策略。
            enabled: Planner 是否要求 rerank。
            result_limit: 用户请求的最终候选数。

        Returns:
            实际 Provider 或明确 bypass 后的候选与模式。

        """
        limited = candidates[: policy.rerank_candidate_limit]
        output_limit = min(result_limit, len(limited))
        if not enabled or not limited:
            return _bypass(limited[:output_limit], "RERANK_DISABLED_BY_PLAN")
        descriptor = self._reranker.descriptor
        key = CircuitKey(descriptor.name, "reranking", descriptor.version)
        if descriptor.capabilities.permits_network:
            try:
                EgressGuard.require_reranking(egress)
            except PolicyDenied:
                if not policy.bypass_policy_denied:
                    raise
                return _bypass(
                    limited[:output_limit], "RERANK_BYPASSED_POLICY_DENIED"
                )
        if not self._circuit.allow_call(key):
            return _bypass(
                limited[:output_limit], "RERANK_BYPASSED_CIRCUIT_OPEN"
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
                limited[:output_limit],
                "RERANK_BYPASSED_PROVIDER_UNAVAILABLE",
            )
        self._circuit.record_success(key)
        protected = _restore_must_keep(
            ordered,
            limited,
            limit=output_limit,
            must_keep_limit=policy.must_keep_limit,
        )
        return RerankingOutcome(
            candidates=tuple(
                item.model_copy(update={"rerank_rank": rank})
                for rank, item in enumerate(protected, start=1)
            ),
            mode=result.mode.value,
            reason_code="RERANK_EXECUTED",
        )


def _bounded_text(candidate: RankedChunk, limit: int) -> str:
    chunk = candidate.hydrated.chunk
    heading = " / ".join(chunk.heading_path)
    value = (
        f"{heading}\n{chunk.citation_text}" if heading else chunk.citation_text
    )
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


def _restore_must_keep(
    selected: tuple[RankedChunk, ...],
    all_candidates: tuple[RankedChunk, ...],
    *,
    limit: int,
    must_keep_limit: int,
) -> tuple[RankedChunk, ...]:
    result = list(selected[:limit])
    present = {item.hydrated.chunk.chunk_id for item in result}
    protected = [item for item in all_candidates if item.must_keep][
        :must_keep_limit
    ]
    for candidate in protected:
        if candidate.hydrated.chunk.chunk_id in present:
            continue
        replace = next(
            (
                index
                for index in range(len(result) - 1, -1, -1)
                if not result[index].must_keep
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
    candidates: tuple[RankedChunk, ...], reason_code: str
) -> RerankingOutcome:
    return RerankingOutcome(
        candidates=candidates,
        mode=reason_code.casefold(),
        reason_code=reason_code,
    )


__all__ = ["CircuitAwareReranker", "RerankingOutcome"]
