"""验证 intent router 数据集切分与标签合同。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from rag_app.generation.question_profile import (
    PrimaryOperation,
    RequestedSlot,
)
from rag_app.generation.semantic_router import _operation_scores

_MIN_TUNING_COUNT = 140
_MIN_HOLDOUT_COUNT = 70
_MAX_SECONDARY_OPERATIONS = 2


@dataclass(frozen=True, slots=True)
class EvaluationExample:
    """一条不包含答案的人工标注意图样本。"""

    example_id: str
    question: str
    primary_operation: PrimaryOperation
    secondary_operations: tuple[PrimaryOperation, ...]
    requested_slots: tuple[RequestedSlot, ...]


def load_examples(path: Path) -> tuple[EvaluationExample, ...]:
    """加载并校验一份 JSONL 评测样本。

    Args:
        path: tuning 或 holdout 的本地 JSONL 文件。

    Returns:
        ID 和标签都有效的样本元组。

    Raises:
        ValueError: 行格式、枚举或重复 ID 无效。

    """
    examples: list[EvaluationExample] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{path.name}:{line_number} 不是 JSON。"
            ) from error
        if not isinstance(payload, dict):
            raise ValueError(f"{path.name}:{line_number} 必须是对象。")
        examples.append(
            _parse_example(payload, path=path, line_number=line_number)
        )
    if not examples:
        raise ValueError(f"{path.name} 不能为空。")
    if len({example.example_id for example in examples}) != len(examples):
        raise ValueError(f"{path.name} 的样本 ID 不能重复。")
    return tuple(examples)


def validate_splits(
    tuning: tuple[EvaluationExample, ...],
    holdout: tuple[EvaluationExample, ...],
) -> None:
    """校验样本规模、operation 覆盖和跨 split 正规化重复。

    Args:
        tuning: 仅用于选择阈值的训练样本。
        holdout: 仅用于最终报告的保留样本。

    Returns:
        无返回值；通过时表示切分满足最小合同。

    Raises:
        ValueError: 样本量、类别覆盖或跨 split 文本重复不满足要求。

    """
    if len(tuning) < _MIN_TUNING_COUNT or len(holdout) < _MIN_HOLDOUT_COUNT:
        raise ValueError("tuning 至少 140 条且 holdout 至少 70 条。")
    for operation in PrimaryOperation:
        if not any(item.primary_operation is operation for item in tuning):
            raise ValueError(f"tuning 缺少 {operation.value}。")
        if not any(item.primary_operation is operation for item in holdout):
            raise ValueError(f"holdout 缺少 {operation.value}。")
    texts = [_normalize(item.question) for item in (*tuning, *holdout)]
    if len(set(texts)) != len(texts):
        raise ValueError("tuning 与 holdout 之间不能有规范化重复问题。")


def main() -> None:
    """执行本地数据集合同检查并输出脱敏计数。

    Args:
        无参数；从命令行取得两份 JSONL 路径。

    Returns:
        无返回值；错误时由 argparse 或 ValueError 终止。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--tuning", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--pipeline", type=Path)
    parser.add_argument("--intent-config", type=Path)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--embedding-endpoint")
    parser.add_argument("--query-instruction")
    parser.add_argument("--embedding-api-token")
    args = parser.parse_args()
    tuning = load_examples(args.tuning)
    holdout = load_examples(args.holdout)
    validate_splits(tuning, holdout)
    report: dict[str, object] = {
        "tuning_count": len(tuning),
        "holdout_count": len(holdout),
    }
    evaluation_arguments = (
        args.pipeline,
        args.intent_config,
        args.calibration,
        args.embedding_endpoint,
        args.query_instruction,
    )
    if any(evaluation_arguments):
        if not all(evaluation_arguments):
            raise ValueError("执行 holdout 指标时必须提供全部 embedding 参数。")
        report["holdout_metrics"] = _evaluate_holdout(
            pipeline_path=args.pipeline,
            intent_config_path=args.intent_config,
            calibration_path=args.calibration,
            holdout=holdout,
            endpoint=args.embedding_endpoint,
            query_instruction=args.query_instruction,
            api_token=args.embedding_api_token,
        )
    print(json.dumps(report, ensure_ascii=False))


def _evaluate_holdout(  # noqa: PLR0913
    *,
    pipeline_path: Path,
    intent_config_path: Path,
    calibration_path: Path,
    holdout: tuple[EvaluationExample, ...],
    endpoint: str,
    query_instruction: str,
    api_token: str | None,
) -> dict[str, float]:
    """用固定校准产物和真实 endpoint 仅报告一次 holdout 指标。"""
    from evaluation.calibrate_intent_router import (  # noqa: PLC0415
        _embed_calibration_inputs,
        _metrics,
    )
    from rag_app.generation.semantic_router import (  # noqa: PLC0415
        load_intent_router_config,
        load_question_profile_calibration,
    )
    from rag_app.runtime import load_pipeline  # noqa: PLC0415
    from rag_app.state.intent_router_cache import (  # noqa: PLC0415
        PrototypeNamespace,
    )

    pipeline = load_pipeline(pipeline_path)
    config = load_intent_router_config(intent_config_path)
    calibration = load_question_profile_calibration(calibration_path)
    namespace = PrototypeNamespace(
        config_sha256=config.canonical_sha256,
        embedding_model=pipeline.embedding_model,
        embedding_revision=pipeline.embedding_revision,
        tokenizer_sha256=pipeline.embedding_tokenizer_sha256,
        dimension=pipeline.embedding_dimension,
        expected_example_count=config.example_count,
    )
    if not calibration.matches(namespace):
        raise ValueError(
            "calibration 与当前 intent config 或 embedding 身份不匹配。"
        )
    if (
        calibration.min_score is None
        or calibration.min_margin is None
        or calibration.secondary_min_score is None
        or calibration.secondary_max_gap is None
    ):
        raise ValueError("calibration 阈值不完整。")
    prototypes, vectors = _embed_calibration_inputs(
        config=config,
        tuning=holdout,
        namespace=namespace,
        endpoint=endpoint,
        instruction=query_instruction,
        api_token=api_token,
    )
    return _metrics(
        holdout,
        tuple(
            _operation_scores(
                vector,
                prototypes,
                aggregation=calibration.aggregation,
                top_k=calibration.top_k,
            )
            for vector in vectors
        ),
        min_score=calibration.min_score,
        min_margin=calibration.min_margin,
        secondary_min_score=calibration.secondary_min_score,
        secondary_max_gap=calibration.secondary_max_gap,
    )


def _parse_example(
    payload: dict[str, object],
    *,
    path: Path,
    line_number: int,
) -> EvaluationExample:
    required = {
        "id",
        "question",
        "primary_operation",
        "secondary_operations",
        "requested_slots",
        "source",
        "notes",
    }
    if set(payload) != required:
        raise ValueError(f"{path.name}:{line_number} 字段无效。")
    return EvaluationExample(
        example_id=_required_string(payload["id"], "id", path, line_number),
        question=_required_string(
            payload["question"], "question", path, line_number
        ),
        primary_operation=PrimaryOperation(
            _required_string(
                payload["primary_operation"],
                "primary_operation",
                path,
                line_number,
            )
        ),
        secondary_operations=_operations(
            payload["secondary_operations"],
            path,
            line_number,
        ),
        requested_slots=_slots(payload["requested_slots"], path, line_number),
    )


def _required_string(
    value: object, field: str, path: Path, line_number: int
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path.name}:{line_number} 的 {field} 无效。")
    return value


def _operations(
    value: object, path: Path, line_number: int
) -> tuple[PrimaryOperation, ...]:
    if not isinstance(value, list) or len(value) > _MAX_SECONDARY_OPERATIONS:
        raise ValueError(
            f"{path.name}:{line_number} 的 secondary_operations 无效。"
        )
    operations = tuple(PrimaryOperation(str(item)) for item in value)
    if (
        len(set(operations)) != len(operations)
        or PrimaryOperation.GENERAL in operations
    ):
        raise ValueError(
            f"{path.name}:{line_number} 的 secondary_operations 无效。"
        )
    return operations


def _slots(
    value: object, path: Path, line_number: int
) -> tuple[RequestedSlot, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path.name}:{line_number} 的 requested_slots 无效。")
    slots = tuple(RequestedSlot(str(item)) for item in value)
    if len(set(slots)) != len(slots):
        raise ValueError(f"{path.name}:{line_number} 的 requested_slots 重复。")
    return slots


def _normalize(value: str) -> str:
    return "".join(value.split()).casefold()


if __name__ == "__main__":
    main()
