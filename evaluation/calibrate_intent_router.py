"""用真实检索 embedding 为语义问题路由选择可审计阈值。"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from evaluation.evaluate_intent_router import EvaluationExample, load_examples
from rag_app.clients.model_services import (
    EmbeddingClientConfig,
    TeiEmbeddingClient,
)
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.generation.question_profile import (
    PrimaryOperation,
    RequestedSlot,
    extract_structural_signals,
)
from rag_app.generation.semantic_router import (
    IntentRouterConfig,
    _operation_scores,
    load_intent_router_config,
)
from rag_app.runtime import load_pipeline
from rag_app.state.intent_router_cache import (
    CachedPrototype,
    PrototypeNamespace,
)


@dataclass(frozen=True, slots=True)
class CalibrationCandidate:
    """一组候选阈值及其 tuning 指标。"""

    aggregation: str
    top_k: int
    min_score: float
    min_margin: float
    secondary_min_score: float
    secondary_max_gap: float
    metrics: dict[str, float]


def main() -> None:
    """从 tuning 集和真实 endpoint 生成 verified 校准 JSON。

    Args:
        无参数；从命令行读取受控配置、样本、endpoint 和输出路径。

    Returns:
        无返回值；成功时原子写入一份可被运行时加载的产物。

    Raises:
        ValueError: endpoint、身份、数据或最小指标不满足要求。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--intent-config", type=Path, required=True)
    parser.add_argument("--tuning", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-endpoint", required=True)
    parser.add_argument("--query-instruction", required=True)
    parser.add_argument("--embedding-api-token")
    parser.add_argument("--minimum-primary-accuracy", type=float, default=0.80)
    parser.add_argument(
        "--minimum-secondary-macro-f1", type=float, default=0.50
    )
    args = parser.parse_args()
    if not args.query_instruction.strip():
        raise ValueError("query instruction 不能为空。")

    pipeline = load_pipeline(args.pipeline)
    config = load_intent_router_config(args.intent_config)
    tuning = load_examples(args.tuning)
    namespace = PrototypeNamespace(
        config_sha256=config.canonical_sha256,
        embedding_model=pipeline.embedding_model,
        embedding_revision=pipeline.embedding_revision,
        tokenizer_sha256=pipeline.embedding_tokenizer_sha256,
        dimension=pipeline.embedding_dimension,
        expected_example_count=config.example_count,
    )
    prototypes, question_vectors = _embed_calibration_inputs(
        config=config,
        tuning=tuning,
        namespace=namespace,
        endpoint=args.embedding_endpoint,
        instruction=args.query_instruction,
        api_token=args.embedding_api_token,
    )
    candidate = select_candidate(
        config=config,
        namespace=namespace,
        prototypes=prototypes,
        tuning=tuning,
        question_vectors=question_vectors,
    )
    if candidate.metrics["primary_accuracy"] < args.minimum_primary_accuracy:
        raise ValueError("tuning primary accuracy 未达到最小要求。")
    if (
        candidate.metrics["secondary_macro_f1"]
        < args.minimum_secondary_macro_f1
    ):
        raise ValueError("tuning secondary macro F1 未达到最小要求。")
    payload = {
        "schema_version": 1,
        "status": "verified",
        "intent_config_sha256": config.canonical_sha256,
        "embedding_model": namespace.embedding_model,
        "embedding_revision": namespace.embedding_revision,
        "tokenizer_sha256": namespace.tokenizer_sha256,
        "vector_dimension": namespace.dimension,
        "aggregation": candidate.aggregation,
        "top_k": candidate.top_k,
        "min_score": candidate.min_score,
        "min_margin": candidate.min_margin,
        "secondary_min_score": candidate.secondary_min_score,
        "secondary_max_gap": candidate.secondary_max_gap,
        "tuning_dataset_sha256": _sha256(args.tuning),
        "tuning_metrics": candidate.metrics,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    _write_json(args.output, payload)
    print(json.dumps(_redacted_report(candidate.metrics), ensure_ascii=False))


def select_candidate(
    *,
    config: IntentRouterConfig,
    namespace: PrototypeNamespace,
    prototypes: tuple[CachedPrototype, ...],
    tuning: tuple[EvaluationExample, ...],
    question_vectors: tuple[tuple[float, ...], ...],
) -> CalibrationCandidate:
    """只使用 tuning 标签选择 aggregation、top-k 与四个阈值。

    Args:
        config: 候选聚合方式和 top-k 的受控配置。
        namespace: prototype 与 embedding 的联合身份。
        prototypes: 已由真实 endpoint 生成的 prototype 向量。
        tuning: 仅用于本次校准的人工标注样本。
        question_vectors: 与 tuning 顺序一致的 query 向量。

    Returns:
        primary accuracy 优先、secondary macro F1 次优的候选。

    Raises:
        ValueError: 向量数量或维度不匹配，或没有可用候选。

    """
    if len(tuning) != len(question_vectors):
        raise ValueError("tuning 与 query vector 数量不一致。")
    if any(len(vector) != namespace.dimension for vector in question_vectors):
        raise ValueError("query vector 维度不匹配。")
    candidates: list[CalibrationCandidate] = []
    for aggregation in config.aggregation_candidates:
        for top_k in config.top_k_candidates:
            scores = tuple(
                _operation_scores(
                    vector,
                    prototypes,
                    aggregation=aggregation,
                    top_k=top_k,
                )
                for vector in question_vectors
            )
            for min_score, min_margin in _primary_threshold_pairs(scores):
                secondary_score, secondary_gap = _best_secondary_thresholds(
                    tuning,
                    scores,
                    min_score=min_score,
                    min_margin=min_margin,
                )
                metrics = _metrics(
                    tuning,
                    scores,
                    min_score=min_score,
                    min_margin=min_margin,
                    secondary_min_score=secondary_score,
                    secondary_max_gap=secondary_gap,
                )
                candidates.append(
                    CalibrationCandidate(
                        aggregation=aggregation,
                        top_k=top_k,
                        min_score=min_score,
                        min_margin=min_margin,
                        secondary_min_score=secondary_score,
                        secondary_max_gap=secondary_gap,
                        metrics=metrics,
                    )
                )
    if not candidates:
        raise ValueError("没有可用的 calibration candidate。")
    return max(
        candidates,
        key=lambda item: (
            item.metrics["primary_accuracy"],
            item.metrics["secondary_macro_f1"],
            item.metrics["slot_macro_f1"],
            -item.metrics["general_fallback_rate"],
        ),
    )


def _embed_calibration_inputs(  # noqa: PLR0913
    *,
    config: IntentRouterConfig,
    tuning: tuple[EvaluationExample, ...],
    namespace: PrototypeNamespace,
    endpoint: str,
    instruction: str,
    api_token: str | None,
) -> tuple[tuple[CachedPrototype, ...], tuple[tuple[float, ...], ...]]:
    """调用一个明确指定的 endpoint，并复用检索 query instruction。"""
    pairs = config.prototypes()
    texts = tuple(prototype.text for _, prototype in pairs) + tuple(
        item.question for item in tuning
    )
    policy = ResiliencePolicy(
        max_attempts=1,
        failure_threshold=1,
        cooldown_seconds=1.0,
        max_concurrency=1,
    )
    with httpx.Client(timeout=httpx.Timeout(60.0)) as http_client:
        client = TeiEmbeddingClient(
            ResilientHttpPool((endpoint,), client=http_client, policy=policy),
            config=EmbeddingClientConfig(
                model=namespace.embedding_model,
                dimension=namespace.dimension,
                max_batch_size=8,
                max_batch_chars=8000,
            ),
            api_token=api_token,
        )
        vectors = client.embed(texts, instruction=instruction).vectors
    prototype_vectors = tuple(
        CachedPrototype(
            example_id=prototype.example_id,
            operation=operation,
            text_sha256=hashlib.sha256(
                prototype.text.encode("utf-8")
            ).hexdigest(),
            vector=vector,
        )
        for (operation, prototype), vector in zip(
            pairs, vectors[: len(pairs)], strict=True
        )
    )
    return prototype_vectors, vectors[len(pairs) :]


def _primary_threshold_pairs(
    scores: tuple[tuple[tuple[PrimaryOperation, float], ...], ...],
) -> tuple[tuple[float, float], ...]:
    values: set[tuple[float, float]] = {(0.0, 0.0)}
    for sample_scores in scores:
        if not sample_scores:
            continue
        top_score = sample_scores[0][1]
        next_score = sample_scores[1][1] if len(sample_scores) > 1 else 0.0
        values.add((top_score, max(0.0, top_score - next_score)))
    return tuple(sorted(values))


def _best_secondary_thresholds(
    tuning: tuple[EvaluationExample, ...],
    scores: tuple[tuple[tuple[PrimaryOperation, float], ...], ...],
    *,
    min_score: float,
    min_margin: float,
) -> tuple[float, float]:
    values = {0.0}
    for sample_scores in scores:
        values.update(score for _, score in sample_scores)
    best = (0.0, 0.0, -1.0)
    for score in sorted(values):
        for gap in sorted(values):
            metrics = _metrics(
                tuning,
                scores,
                min_score=min_score,
                min_margin=min_margin,
                secondary_min_score=score,
                secondary_max_gap=gap,
            )
            if metrics["secondary_macro_f1"] > best[2]:
                best = (score, gap, metrics["secondary_macro_f1"])
    return best[0], best[1]


def _metrics(  # noqa: PLR0913
    tuning: tuple[EvaluationExample, ...],
    scores: tuple[tuple[tuple[PrimaryOperation, float], ...], ...],
    *,
    min_score: float,
    min_margin: float,
    secondary_min_score: float,
    secondary_max_gap: float,
) -> dict[str, float]:
    predictions = tuple(
        _predict(
            sample_scores,
            min_score=min_score,
            min_margin=min_margin,
            secondary_min_score=secondary_min_score,
            secondary_max_gap=secondary_max_gap,
        )
        for sample_scores in scores
    )
    primary = _macro_f1(
        tuple(item.primary_operation for item in tuning),
        tuple(item[0] for item in predictions),
        tuple(PrimaryOperation),
    )
    secondary = _set_macro_f1(
        tuple(item.secondary_operations for item in tuning),
        tuple(item[1] for item in predictions),
        tuple(
            operation
            for operation in PrimaryOperation
            if operation is not PrimaryOperation.GENERAL
        ),
    )
    slots = _set_macro_f1(
        tuple(item.requested_slots for item in tuning),
        tuple(
            extract_structural_signals(item.question).requested_slots
            for item in tuning
        ),
        tuple(RequestedSlot),
    )
    correct = sum(
        expected.primary_operation is predicted[0]
        for expected, predicted in zip(tuning, predictions, strict=True)
    )
    general_count = sum(
        predicted[0] is PrimaryOperation.GENERAL for predicted in predictions
    )
    return {
        "primary_accuracy": correct / len(tuning),
        "primary_macro_f1": primary,
        "secondary_macro_f1": secondary,
        "slot_macro_f1": slots,
        "general_fallback_rate": general_count / len(tuning),
        "coverage": 1.0 - general_count / len(tuning),
    }


def _predict(
    scores: tuple[tuple[PrimaryOperation, float], ...],
    *,
    min_score: float,
    min_margin: float,
    secondary_min_score: float,
    secondary_max_gap: float,
) -> tuple[PrimaryOperation, tuple[PrimaryOperation, ...]]:
    if not scores:
        return PrimaryOperation.GENERAL, ()
    primary, confidence = scores[0]
    margin = confidence - (scores[1][1] if len(scores) > 1 else 0.0)
    if confidence < min_score or margin < min_margin:
        return PrimaryOperation.GENERAL, ()
    return (
        primary,
        tuple(
            operation
            for operation, score in scores[1:]
            if (
                operation is not PrimaryOperation.GENERAL
                and score >= secondary_min_score
                and confidence - score <= secondary_max_gap
            )
        )[:2],
    )


def _macro_f1(
    expected: tuple[PrimaryOperation, ...],
    predicted: tuple[PrimaryOperation, ...],
    labels: tuple[PrimaryOperation, ...],
) -> float:
    return sum(
        _f1(
            sum(
                left is label and right is label
                for left, right in zip(expected, predicted, strict=True)
            ),
            sum(
                left is not label and right is label
                for left, right in zip(expected, predicted, strict=True)
            ),
            sum(
                left is label and right is not label
                for left, right in zip(expected, predicted, strict=True)
            ),
        )
        for label in labels
    ) / len(labels)


def _set_macro_f1(
    expected: tuple[tuple[object, ...], ...],
    predicted: tuple[tuple[object, ...], ...],
    labels: tuple[object, ...],
) -> float:
    return sum(
        _f1(
            sum(
                label in left and label in right
                for left, right in zip(expected, predicted, strict=True)
            ),
            sum(
                label not in left and label in right
                for left, right in zip(expected, predicted, strict=True)
            ),
            sum(
                label in left and label not in right
                for left, right in zip(expected, predicted, strict=True)
            ),
        )
        for label in labels
    ) / len(labels)


def _f1(true_positive: int, false_positive: int, false_negative: int) -> float:
    denominator = 2 * true_positive + false_positive + false_negative
    return 0.0 if denominator == 0 else 2 * true_positive / denominator


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _redacted_report(metrics: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 4) for key, value in metrics.items()}


if __name__ == "__main__":
    main()
