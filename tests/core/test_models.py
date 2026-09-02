from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rag_app.core.events import TraceEvent
from rag_app.core.models import (
    EmbeddingRequestRole,
    EmbeddingResult,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
    SecretRef,
)


def _slot(
    slot_id: str,
    role: EmbeddingSlotRole,
    vector_name: str,
    provider_id: str,
) -> EmbeddingSlotIdentity:
    return EmbeddingSlotIdentity(
        slot_id=slot_id,
        role=role,
        provider_id=provider_id,
        model=f"{provider_id}-model",
        vector_name=vector_name,
        dimension=1024,
        document_request_policy={"role": "document"},
        query_request_policy={"role": "query"},
        normalization="l2",
    )


def test_hot_standby_keeps_equal_dimensions_in_distinct_vector_spaces() -> None:
    primary = _slot(
        "primary",
        EmbeddingSlotRole.PRIMARY,
        "dense_primary",
        "jina",
    )
    standby = _slot(
        "standby",
        EmbeddingSlotRole.STANDBY,
        "dense_standby",
        "aliyun-qwen37",
    )
    topology = EmbeddingTopology(
        mode="hot_standby",
        primary_slot_id="primary",
        standby_slot_id="standby",
        slots=(primary, standby),
    )
    assert topology.slots[0].dimension == topology.slots[1].dimension
    assert (
        topology.slots[0].vector_space_identity
        != topology.slots[1].vector_space_identity
    )


def test_hot_standby_rejects_duplicate_vector_name() -> None:
    primary = _slot(
        "primary",
        EmbeddingSlotRole.PRIMARY,
        "dense",
        "jina",
    )
    standby = _slot(
        "standby",
        EmbeddingSlotRole.STANDBY,
        "dense",
        "aliyun-qwen37",
    )
    with pytest.raises(ValidationError):
        EmbeddingTopology(
            mode="hot_standby",
            primary_slot_id="primary",
            standby_slot_id="standby",
            slots=(primary, standby),
        )


def test_models_are_frozen_and_reject_extra_fields() -> None:
    secret = SecretRef(env_name="JINA_API_KEY")
    with pytest.raises(ValidationError):
        SecretRef.model_validate(
            {"env_name": "JINA_API_KEY", "value": "secret"}
        )
    with pytest.raises(ValidationError):
        secret.env_name = "OTHER_KEY"


def test_datetime_must_have_timezone() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(
            trace_id=f"trace_{'a' * 32}",
            event_name="query.start",
            occurred_at=datetime(2026, 9, 1),
        )
    event = TraceEvent(
        trace_id=f"trace_{'a' * 32}",
        event_name="query.start",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert event.occurred_at.tzinfo is UTC


def test_embedding_result_rejects_nan_and_dimension_drift() -> None:
    with pytest.raises(ValidationError):
        EmbeddingResult(
            slot_id="primary",
            role=EmbeddingRequestRole.QUERY,
            vectors=((float("nan"), 0.0),),
            observed_dimension=2,
            request_policy_identity="query-v1",
        )
    with pytest.raises(ValidationError):
        EmbeddingResult(
            slot_id="primary",
            role=EmbeddingRequestRole.QUERY,
            vectors=((1.0,),),
            observed_dimension=2,
            request_policy_identity="query-v1",
        )


def test_schema_round_trip_preserves_tuple_collections() -> None:
    slot = _slot(
        "primary",
        EmbeddingSlotRole.PRIMARY,
        "dense_primary",
        "jina",
    )
    restored = EmbeddingSlotIdentity.model_validate_json(slot.model_dump_json())
    assert restored == slot
    assert isinstance(restored.query_request_policy, tuple)
