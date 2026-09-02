from __future__ import annotations

from collections.abc import Sequence

import pytest

from rag_app.application.embedding_router import (
    ActiveRevisionEmbeddingState,
    DocumentEmbeddingInput,
    DualEmbeddingCoordinator,
    EmbeddingFailoverRouter,
    QueryEmbeddingRequest,
    embedding_cache_key,
    search_cache_key,
    validate_required_slot_coverage,
)
from rag_app.composition.profiles import default_hot_standby_profile
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    DenseUnavailable,
    PolicyDenied,
    ProviderAuthenticationError,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
    ProviderUnavailable,
    RagError,
)
from rag_app.core.models import (
    CircuitState,
    EmbeddingCoverage,
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingResult,
    EmbeddingSlotIdentity,
    EmbeddingTopology,
    ProviderHealth,
    ProviderHealthStatus,
)
from rag_app.core.policies import EgressPolicy


class _EmbeddingFake:
    def __init__(
        self,
        slot: EmbeddingSlotIdentity,
        failures: Sequence[RagError] = (),
    ) -> None:
        self.slot = slot
        self.failures = list(failures)
        self.calls = 0
        self.requests: list[EmbeddingRequest] = []
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name=slot.provider_id,
            version=slot.model,
            mode=ProviderMode.DETERMINISTIC,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                dimensions=(slot.dimension,),
                roles=("document", "query"),
            ),
        )

    @property
    def capabilities(self) -> ComponentCapabilities:
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.calls += 1
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        vector = (1.0,) + (0.0,) * (self.slot.dimension - 1)
        return EmbeddingResult(
            slot_id=request.slot_id,
            role=request.role,
            vectors=tuple(vector for _ in request.texts),
            observed_dimension=self.slot.dimension,
            request_policy_identity=f"{self.slot.slot_id}-policy",
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.HEALTHY,
            reason_code="FAKE_READY",
        )


class _ZeroEmbeddingFake(_EmbeddingFake):
    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.calls += 1
        vector = (0.0,) * self.slot.dimension
        return EmbeddingResult(
            slot_id=request.slot_id,
            role=request.role,
            vectors=tuple(vector for _ in request.texts),
            observed_dimension=self.slot.dimension,
            request_policy_identity=f"{self.slot.slot_id}-policy",
        )


def _topology() -> EmbeddingTopology:
    configured = default_hot_standby_profile().components.embedding_topology
    if isinstance(configured, str):
        raise AssertionError("test profile must use explicit topology")
    return configured.to_core()


def _slots() -> tuple[EmbeddingSlotIdentity, EmbeddingSlotIdentity]:
    topology = _topology()
    standby_id = topology.standby_slot_id
    if standby_id is None:
        raise AssertionError("test profile must include standby")
    return topology.slot(topology.primary_slot_id), topology.slot(standby_id)


def _revision(
    *,
    primary_ratio: float = 1.0,
    standby_ratio: float = 1.0,
) -> ActiveRevisionEmbeddingState:
    topology = _topology()
    primary, standby = _slots()
    chunk_count = 10
    return ActiveRevisionEmbeddingState(
        topology=topology,
        coverages=(
            EmbeddingCoverage(
                slot_id=primary.slot_id,
                vector_name=primary.vector_name,
                vector_count=round(chunk_count * primary_ratio),
                chunk_count=chunk_count,
                observed_dimension=primary.dimension,
            ),
            EmbeddingCoverage(
                slot_id=standby.slot_id,
                vector_name=standby.vector_name,
                vector_count=round(chunk_count * standby_ratio),
                chunk_count=chunk_count,
                observed_dimension=standby.dimension,
            ),
        ),
    )


def _egress(**updates: object) -> EgressPolicy:
    policy = EgressPolicy(
        remote_query_embedding=True,
        remote_query_embedding_jina=True,
        remote_query_embedding_aliyun=True,
        allow_aliyun_embedding_failover=True,
        aliyun_daily_request_budget=100,
        aliyun_daily_token_budget=100000,
    )
    return policy.model_copy(update=updates)


def _router(
    primary: _EmbeddingFake, standby: _EmbeddingFake
) -> EmbeddingFailoverRouter:
    return EmbeddingFailoverRouter(primary, standby)


def test_primary_success_does_not_call_standby() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(primary_slot)
    standby = _EmbeddingFake(standby_slot)
    result = _router(primary, standby).embed_query_with_failover(
        QueryEmbeddingRequest("query"), _revision(), _egress()
    )
    assert result.selected_slot_id == "primary"
    assert result.vector_name == "dense_primary"
    assert result.attempted_slot_ids == ("primary",)
    assert primary.calls == 1
    assert standby.calls == 0


@pytest.mark.parametrize(
    ("error", "reason", "state"),
    (
        (
            ProviderUnavailable("timeout", stage="test.primary"),
            "PRIMARY_TRANSIENT_FAILURE",
            CircuitState.CLOSED,
        ),
        (
            ProviderInvalidResponse("bad dimension", stage="test.primary"),
            "PRIMARY_RESPONSE_CONTRACT",
            CircuitState.QUARANTINED,
        ),
        (
            ProviderAuthenticationError("bad key", stage="test.primary"),
            "PRIMARY_AUTH_OR_MODEL",
            CircuitState.OPEN,
        ),
    ),
)
def test_allowed_primary_failure_selects_standby(
    error: RagError,
    reason: str,
    state: CircuitState,
) -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(primary_slot, (error,))
    standby = _EmbeddingFake(standby_slot)
    result = _router(primary, standby).embed_query_with_failover(
        QueryEmbeddingRequest("query"), _revision(), _egress()
    )
    assert result.selected_slot_id == "standby"
    assert result.vector_name == "dense_standby"
    assert result.attempted_slot_ids == ("primary", "standby")
    assert result.fallback_reason == reason
    assert result.circuit_after[0].state is state


def test_input_error_does_not_call_standby() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(
        primary_slot,
        (ProviderInputTooLarge("invalid", stage="test.primary"),),
    )
    standby = _EmbeddingFake(standby_slot)
    with pytest.raises(ProviderInputTooLarge):
        _router(primary, standby).embed_query_with_failover(
            QueryEmbeddingRequest("query"), _revision(), _egress()
        )
    assert standby.calls == 0


def test_incomplete_standby_coverage_prevents_failover() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(
        primary_slot,
        (ProviderUnavailable("timeout", stage="test.primary"),),
    )
    standby = _EmbeddingFake(standby_slot)
    with pytest.raises(DenseUnavailable):
        _router(primary, standby).embed_query_with_failover(
            QueryEmbeddingRequest("query"),
            _revision(standby_ratio=0.9),
            _egress(),
        )
    assert standby.calls == 0


@pytest.mark.parametrize(
    "updates",
    (
        {"remote_query_embedding_aliyun": False},
        {"allow_aliyun_embedding_failover": False},
    ),
)
def test_standby_egress_denial_prevents_call(
    updates: dict[str, object],
) -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(
        primary_slot,
        (ProviderUnavailable("timeout", stage="test.primary"),),
    )
    standby = _EmbeddingFake(standby_slot)
    with pytest.raises(PolicyDenied):
        _router(primary, standby).embed_query_with_failover(
            QueryEmbeddingRequest("query"),
            _revision(),
            _egress(**updates),
        )
    assert standby.calls == 0


def test_both_fail_returns_structured_dense_unavailable() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(
        primary_slot,
        (ProviderUnavailable("timeout", stage="test.primary"),),
    )
    standby = _EmbeddingFake(
        standby_slot,
        (ProviderUnavailable("timeout", stage="test.standby"),),
    )
    with pytest.raises(DenseUnavailable) as captured:
        _router(primary, standby).embed_query_with_failover(
            QueryEmbeddingRequest("query"), _revision(), _egress()
        )
    assert dict(captured.value.details)["attempted_slots"] == [
        "primary",
        "standby",
    ]


def test_open_primary_circuit_is_skipped_on_third_request() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(
        primary_slot,
        tuple(
            ProviderUnavailable("timeout", stage="test.primary")
            for _ in range(3)
        ),
    )
    standby = _EmbeddingFake(standby_slot)
    router = _router(primary, standby)
    first = router.embed_query_with_failover(
        QueryEmbeddingRequest("query one"), _revision(), _egress()
    )
    second = router.embed_query_with_failover(
        QueryEmbeddingRequest("query two"), _revision(), _egress()
    )
    third = router.embed_query_with_failover(
        QueryEmbeddingRequest("query three"), _revision(), _egress()
    )
    assert first.selected_slot_id == second.selected_slot_id == "standby"
    assert third.fallback_reason == "PRIMARY_CIRCUIT_OPEN"
    assert primary.calls == 2
    assert standby.calls == 3


def test_request_is_sticky_and_cache_keys_include_slot_and_mode() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(
        primary_slot,
        (ProviderUnavailable("timeout", stage="test.primary"),),
    )
    standby = _EmbeddingFake(standby_slot)
    result = _router(primary, standby).embed_query_with_failover(
        QueryEmbeddingRequest("query"), _revision(), _egress()
    )
    assert result.selected_slot_id == "standby"
    assert standby.requests == [
        EmbeddingRequest(
            slot_id="standby",
            role=EmbeddingRequestRole.QUERY,
            texts=("query",),
        )
    ]
    assert embedding_cache_key(
        primary_slot, EmbeddingRequestRole.QUERY, "query"
    ) != embedding_cache_key(
        standby_slot, EmbeddingRequestRole.QUERY, "query"
    )
    assert search_cache_key("fp", "standby", "provider", "query") != (
        search_cache_key("fp", "standby", "bypass_keep_rrf", "query")
    )


def test_dual_coordinator_keeps_slots_separate_and_uses_cache() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(primary_slot)
    standby = _EmbeddingFake(standby_slot)
    coordinator = DualEmbeddingCoordinator(
        {"primary": primary, "standby": standby}
    )
    chunks = (
        DocumentEmbeddingInput("chunk_a", "first"),
        DocumentEmbeddingInput("chunk_b", "second"),
    )
    first = coordinator.embed_documents_for_slots(chunks, _topology())
    second = coordinator.embed_documents_for_slots(chunks, _topology())

    assert validate_required_slot_coverage(first, _topology(), chunk_count=2)
    assert validate_required_slot_coverage(second, _topology(), chunk_count=2)
    assert (
        first.outcome("primary").vectors
        is not first.outcome("standby").vectors
    )
    assert second.outcome("primary").reason_code == "CACHE_HIT"
    assert primary.calls == standby.calls == 1


def test_coordinator_failure_keeps_revision_incomplete() -> None:
    primary_slot, standby_slot = _slots()
    primary = _EmbeddingFake(primary_slot)
    standby = _EmbeddingFake(
        standby_slot,
        (ProviderUnavailable("down", stage="test.standby"),),
    )
    result = DualEmbeddingCoordinator(
        {"primary": primary, "standby": standby}
    ).embed_documents_for_slots(
        (DocumentEmbeddingInput("chunk_a", "first"),),
        _topology(),
    )
    assert result.outcome("primary").reason_code == "COMPLETE"
    assert result.outcome("standby").reason_code == "PROVIDER_UNAVAILABLE"
    assert not validate_required_slot_coverage(
        result, _topology(), chunk_count=1
    )


def test_coverage_validator_rejects_zero_vectors() -> None:
    primary_slot, standby_slot = _slots()
    result = DualEmbeddingCoordinator(
        {
            "primary": _ZeroEmbeddingFake(primary_slot),
            "standby": _EmbeddingFake(standby_slot),
        }
    ).embed_documents_for_slots(
        (DocumentEmbeddingInput("chunk_a", "first"),),
        _topology(),
    )
    assert not validate_required_slot_coverage(
        result, _topology(), chunk_count=1
    )
