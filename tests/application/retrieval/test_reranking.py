from __future__ import annotations

import pytest

from rag_app.application.provider_health import (
    CircuitKey,
    ProviderCircuitBreaker,
)
from rag_app.application.retrieval.reranking import CircuitAwareReranker
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.models import (
    ProviderFailureCategory,
    RerankExecutionMode,
    RerankItem,
    RerankRequest,
    RerankResult,
    RetrievalPolicy,
)
from rag_app.core.policies import EgressPolicy
from tests.application.retrieval.helpers import make_ranked_chunk


class _Reranker:
    descriptor = ComponentDescriptor(
        kind=ComponentKind.RERANKER,
        name="jina-reranker",
        version="test-model",
        mode=ProviderMode.DETERMINISTIC,
        capabilities=ComponentCapabilities(supports_batch=True),
    )

    def __init__(self, *, invalid: bool = False, equal: bool = False) -> None:
        self.invalid = invalid
        self.equal = equal
        self.calls = 0

    def rerank(self, request: RerankRequest) -> RerankResult:
        self.calls += 1
        count = max(0, request.limit - int(self.invalid))
        return RerankResult(
            mode=RerankExecutionMode.PROVIDER,
            items=tuple(
                RerankItem(
                    candidate_id=candidate_id,
                    score=1.0 if self.equal else float(count - index),
                )
                for index, (candidate_id, _) in enumerate(
                    request.candidates[:count]
                )
            ),
        )


class _FixedReranker(_Reranker):
    def __init__(self, items: tuple[RerankItem, ...]) -> None:
        super().__init__()
        self.items = items

    def rerank(self, request: RerankRequest) -> RerankResult:
        del request
        self.calls += 1
        return RerankResult(
            mode=RerankExecutionMode.PROVIDER,
            items=self.items,
        )


def test_reranker_returns_requested_limit_and_preserves_equal_order() -> None:
    candidates = tuple(
        make_ranked_chunk(index, f"text {index}") for index in range(1, 6)
    )
    outcome = CircuitAwareReranker(_Reranker(equal=True)).rerank(
        "query",
        candidates,
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=3,
    )

    assert len(outcome.candidates) == 3
    assert [item.hydrated.chunk.chunk_id for item in outcome.candidates] == [
        item.hydrated.chunk.chunk_id for item in candidates[:3]
    ]


def test_invalid_reranker_response_bypasses_without_zero_scores() -> None:
    candidates = tuple(
        make_ranked_chunk(index, f"text {index}") for index in range(1, 6)
    )
    outcome = CircuitAwareReranker(_Reranker(invalid=True)).rerank(
        "query",
        candidates,
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=3,
    )

    assert outcome.reason_code == "RERANK_BYPASSED_PROVIDER_UNAVAILABLE"
    assert all(item.rerank_score is None for item in outcome.candidates)


def test_open_reranker_circuit_does_not_call_provider() -> None:
    provider = _Reranker()
    circuit = ProviderCircuitBreaker()
    key = CircuitKey("jina-reranker", "reranking", "test-model")
    circuit.record_failure(key, ProviderFailureCategory.AUTH_OR_MODEL)
    outcome = CircuitAwareReranker(provider, circuit=circuit).rerank(
        "query",
        (make_ranked_chunk(1, "text"),),
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=1,
    )

    assert outcome.reason_code == "RERANK_BYPASSED_CIRCUIT_OPEN"
    assert provider.calls == 0


@pytest.mark.parametrize("invalid_kind", ("duplicate", "unknown", "nan"))
def test_invalid_reranker_items_bypass_and_preserve_rrf(
    invalid_kind: str,
) -> None:
    candidates = tuple(
        make_ranked_chunk(index, f"text {index}") for index in range(1, 4)
    )
    first_id = candidates[0].hydrated.chunk.chunk_id
    second_id = candidates[1].hydrated.chunk.chunk_id
    third_id = candidates[2].hydrated.chunk.chunk_id
    items = {
        "duplicate": (
            RerankItem(candidate_id=first_id, score=3.0),
            RerankItem(candidate_id=first_id, score=2.0),
            RerankItem(candidate_id=third_id, score=1.0),
        ),
        "unknown": (
            RerankItem(candidate_id=first_id, score=3.0),
            RerankItem(candidate_id=second_id, score=2.0),
            RerankItem(candidate_id="chunk_unknown", score=1.0),
        ),
        "nan": (
            RerankItem(candidate_id=first_id, score=3.0),
            RerankItem(candidate_id=second_id, score=float("nan")),
            RerankItem(candidate_id=third_id, score=1.0),
        ),
    }[invalid_kind]

    outcome = CircuitAwareReranker(_FixedReranker(items)).rerank(
        "query",
        candidates,
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=3,
    )

    assert outcome.reason_code == "RERANK_BYPASSED_PROVIDER_UNAVAILABLE"
    assert outcome.candidates == candidates
    assert all(item.rerank_score is None for item in outcome.candidates)


def test_reranker_restores_must_keep_exact_candidate() -> None:
    candidates = tuple(
        make_ranked_chunk(
            index,
            f"text {index}",
            must_keep=index == 5,
        )
        for index in range(1, 6)
    )

    outcome = CircuitAwareReranker(_Reranker()).rerank(
        "query",
        candidates,
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=3,
    )

    assert candidates[4].hydrated.chunk.chunk_id in {
        item.hydrated.chunk.chunk_id for item in outcome.candidates
    }
    assert len(outcome.candidates) == 3
