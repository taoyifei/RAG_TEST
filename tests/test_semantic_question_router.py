from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rag_app.generation.question_profile import (
    PrimaryOperation,
    extract_structural_signals,
)
from rag_app.generation.semantic_router import (
    IntentRouterConfig,
    IntentRouterMode,
    Prototype,
    QuestionProfileCalibration,
    SemanticQuestionRouter,
)
from rag_app.state.intent_router_cache import (
    CachedPrototype,
    IntentRouterCache,
    PrototypeNamespace,
)


def test_router_uses_top_k_and_returns_general_for_small_margin() -> None:
    namespace = _namespace()
    router = SemanticQuestionRouter(
        config=_config(),
        calibration=_calibration(namespace),
        namespace=namespace,
    )
    router.set_prototypes(_vectors(namespace))

    profile = router.route(
        "选择一种方式",
        (1.0, 0.0),
        extract_structural_signals("选择一种方式"),
    )

    assert profile.primary_operation is PrimaryOperation.DECISION
    assert profile.reason_code == "SEMANTIC_CONFIDENT"

    uncertain = router.route(
        "选择一种方式",
        (1.0, 1.0),
        extract_structural_signals("选择一种方式"),
    )

    assert uncertain.primary_operation is PrimaryOperation.GENERAL
    assert uncertain.reason_code == "SEMANTIC_UNCERTAIN"


def test_router_safely_falls_back_for_invalid_vector_and_calibration() -> None:
    namespace = _namespace()
    router = SemanticQuestionRouter(
        config=_config(),
        calibration=QuestionProfileCalibration(
            status="unverified",
            canonical_sha256="calibration",
        ),
        namespace=namespace,
    )

    unavailable = router.route(
        "询问内容",
        None,
        extract_structural_signals("询问内容"),
    )
    invalid = router.route(
        "询问内容",
        (float("nan"), 0.0),
        extract_structural_signals("询问内容"),
    )
    unverified = router.route(
        "询问内容",
        (1.0, 0.0),
        extract_structural_signals("询问内容"),
    )

    assert unavailable.reason_code == "QUERY_VECTOR_UNAVAILABLE"
    assert invalid.reason_code == "QUERY_VECTOR_INVALID"
    assert unverified.reason_code == "CALIBRATION_UNVERIFIED"


def test_cache_rejects_incomplete_namespace_and_reuses_complete_cache(
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    cache = IntentRouterCache(tmp_path / "intent-router.sqlite3")
    cache.initialize()

    assert cache.load_complete(namespace) == ()
    cache.publish(namespace, _vectors(namespace))

    loaded = cache.load_complete(namespace)

    assert len(loaded) == namespace.expected_example_count
    assert loaded[0].vector == _vectors(namespace)[0].vector


def test_config_rejects_duplicate_example_id_across_operations() -> None:
    operations = list(_config().operations)
    _, first_examples = operations[0]
    second_operation, second_examples = operations[1]
    operations[1] = (
        second_operation,
        (
            Prototype(
                example_id=first_examples[0].example_id,
                text="不同 operation 的重复 ID",
            ),
            *second_examples[1:],
        ),
    )

    with pytest.raises(ValueError, match="example ID"):
        IntentRouterConfig(
            router_revision="test-router",
            mode=IntentRouterMode.SHADOW,
            aggregation_candidates=("max",),
            top_k_candidates=(1,),
            llm_fallback_enabled=False,
            llm_fallback_max_output_tokens=96,
            operations=tuple(operations),
            canonical_sha256="config",
        )


def _namespace() -> PrototypeNamespace:
    return PrototypeNamespace(
        config_sha256="config",
        embedding_model="embedding",
        embedding_revision="revision",
        tokenizer_sha256="tokenizer",
        dimension=2,
        expected_example_count=len(PrimaryOperation) * 20,
    )


def _config() -> IntentRouterConfig:
    return IntentRouterConfig(
        router_revision="test-router",
        mode=IntentRouterMode.SHADOW,
        aggregation_candidates=("max",),
        top_k_candidates=(1,),
        llm_fallback_enabled=False,
        llm_fallback_max_output_tokens=96,
        operations=tuple(
            (
                operation,
                tuple(
                    Prototype(
                        example_id=f"{operation.value.lower()}-{index}",
                        text=f"{operation.value} 样本 {index}",
                    )
                    for index in range(20)
                ),
            )
            for operation in PrimaryOperation
        ),
        canonical_sha256="config",
    )


def _calibration(namespace: PrototypeNamespace) -> QuestionProfileCalibration:
    return QuestionProfileCalibration(
        status="verified",
        canonical_sha256="calibration",
        intent_config_sha256=namespace.config_sha256,
        embedding_model=namespace.embedding_model,
        embedding_revision=namespace.embedding_revision,
        tokenizer_sha256=namespace.tokenizer_sha256,
        vector_dimension=namespace.dimension,
        aggregation="max",
        top_k=1,
        min_score=0.8,
        min_margin=0.1,
        secondary_min_score=0.6,
        secondary_max_gap=0.3,
    )


def _vectors(
    namespace: PrototypeNamespace,
) -> tuple[CachedPrototype, ...]:
    vectors: list[CachedPrototype] = []
    for operation in PrimaryOperation:
        vector = (
            (1.0, 0.0)
            if operation is PrimaryOperation.DECISION
            else (0.0, 1.0)
        )
        for index in range(20):
            example_id = f"{operation.value.lower()}-{index}"
            vectors.append(
                CachedPrototype(
                    example_id=example_id,
                    operation=operation,
                    text_sha256=hashlib.sha256(
                        example_id.encode("utf-8")
                    ).hexdigest(),
                    vector=vector,
                )
            )
    assert len(vectors) == namespace.expected_example_count
    return tuple(vectors)
