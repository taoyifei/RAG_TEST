from __future__ import annotations

from collections.abc import Sequence

import pytest

from rag_app.application.embedding_router import QueryEmbeddingRouter
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import DenseUnavailable, ProviderUnavailable
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ActiveRevisionEmbeddingState,
    EmbeddingCoverage,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
    QueryEmbeddingRequest,
)
from rag_app.core.policies import EgressPolicy


class _Embedding:
    def __init__(
        self,
        slot: EmbeddingSlotIdentity,
        failures: Sequence[Exception] = (),
    ) -> None:
        self.slot = slot
        self.failures = list(failures)
        self.calls = 0
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
        if self.failures:
            raise self.failures.pop(0)
        return EmbeddingResult(
            slot_id=self.slot.slot_id,
            role=request.role,
            vectors=tuple((1.0, 0.0) for _ in request.texts),
            observed_dimension=2,
            request_policy_identity=canonical_sha256(
                self.slot.query_request_policy
            ),
        )


def _slot(
    slot_id: str, role: EmbeddingSlotRole, provider: str, vector_name: str
) -> EmbeddingSlotIdentity:
    return EmbeddingSlotIdentity(
        slot_id=slot_id,
        role=role,
        provider_id=provider,
        model=f"{provider}-model",
        vector_name=vector_name,
        dimension=2,
        normalization="l2-v1",
        query_request_policy={"task": "retrieval.query"},
    )


def _state(
    slots: tuple[EmbeddingSlotIdentity, ...], mode: str
) -> ActiveRevisionEmbeddingState:
    topology = EmbeddingTopology(
        mode=mode,
        primary_slot_id="primary",
        standby_slot_id="standby" if mode == "hot_standby" else None,
        slots=slots,
    )
    return ActiveRevisionEmbeddingState(
        topology=topology,
        coverages=tuple(
            EmbeddingCoverage(
                slot_id=slot.slot_id,
                vector_name=slot.vector_name,
                vector_count=1,
                chunk_count=1,
                observed_dimension=slot.dimension,
            )
            for slot in slots
        ),
    )


def test_single_deterministic_router_is_non_null_and_uses_primary() -> None:
    primary_slot = _slot(
        "primary", EmbeddingSlotRole.PRIMARY, "deterministic", "dense_primary"
    )
    primary = _Embedding(primary_slot)

    result = QueryEmbeddingRouter(primary).embed_query(
        QueryEmbeddingRequest("query"),
        _state((primary_slot,), "single"),
        EgressPolicy(),
    )

    assert result.selected_slot_id == "primary"
    assert result.vector_name == "dense_primary"
    assert primary.calls == 1


def test_jina_only_single_failure_has_no_invented_standby() -> None:
    primary_slot = _slot(
        "primary", EmbeddingSlotRole.PRIMARY, "jina-embedding", "dense_primary"
    )
    primary = _Embedding(
        primary_slot,
        (ProviderUnavailable("timeout", stage="test.query"),),
    )
    policy = EgressPolicy(
        remote_query_embedding=True,
        remote_query_embedding_jina=True,
    )

    with pytest.raises(DenseUnavailable):
        QueryEmbeddingRouter(primary).embed_query(
            QueryEmbeddingRequest("query"),
            _state((primary_slot,), "single"),
            policy,
        )
    assert primary.calls == 1


def test_fake_jina_failure_routes_only_to_fake_qwen_vector_space() -> None:
    primary_slot = _slot(
        "primary", EmbeddingSlotRole.PRIMARY, "jina-embedding", "dense_primary"
    )
    standby_slot = _slot(
        "standby",
        EmbeddingSlotRole.STANDBY,
        "aliyun-qwen37-embedding",
        "dense_standby",
    )
    primary = _Embedding(
        primary_slot,
        (ProviderUnavailable("timeout", stage="test.query"),),
    )
    standby = _Embedding(standby_slot)
    policy = EgressPolicy(
        remote_query_embedding=True,
        remote_query_embedding_jina=True,
        remote_query_embedding_aliyun=True,
        allow_aliyun_embedding_failover=True,
        aliyun_daily_request_budget=1,
        aliyun_daily_token_budget=100,
    )

    result = QueryEmbeddingRouter(primary, standby).embed_query(
        QueryEmbeddingRequest("query"),
        _state((primary_slot, standby_slot), "hot_standby"),
        policy,
    )

    assert result.selected_slot_id == "standby"
    assert result.vector_name == "dense_standby"
    assert primary.calls == standby.calls == 1
