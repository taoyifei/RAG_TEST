"""读取带来源阈值并对 P08 指标执行失败关闭门禁。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evaluation.v2.models import GateOutcome, GateReport, MetricReport


class _GateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,80}$")
    metric: str
    operator: Literal["eq", "ge", "le"]
    value: float | int
    minimum_samples: int = Field(default=0, ge=0)
    source: Literal[
        "accepted product requirement",
        "historical baseline",
        "explicit provisional engineering gate",
    ]


class _GateConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    gates: tuple[_GateDefinition, ...]


def load_gate_configuration(path: Path) -> object:
    """读取并验证 P08 gate JSON。

    Args:
        path: 门禁配置文件。

    Returns:
        不向外暴露实现模型的已验证配置。

    """
    return _GateConfiguration.model_validate_json(path.read_text("utf-8"))


def evaluate_gates(metrics: MetricReport, configuration: object) -> GateReport:
    """对总体指标执行全部来源明确的门禁。

    Args:
        metrics: 固定 lane/variant/split 的指标报告。
        configuration: `load_gate_configuration()` 的返回值。

    Returns:
        每条判定及总结果。

    Raises:
        TypeError: 传入未经加载器验证的配置。
        ValueError: 门禁引用缺失或不可执行指标。

    """
    if not isinstance(configuration, _GateConfiguration):
        raise TypeError("Gate 配置必须来自 load_gate_configuration。")
    outcomes = tuple(
        _evaluate_definition(metrics, definition)
        for definition in configuration.gates
    )
    return GateReport(
        passed=all(outcome.passed for outcome in outcomes),
        outcomes=outcomes,
    )


def _evaluate_definition(
    metrics: MetricReport,
    definition: _GateDefinition,
) -> GateOutcome:
    metric = metrics.metrics.get(definition.metric)
    if metric is None:
        raise ValueError(f"Gate 引用了未知指标：{definition.metric}")
    if metric.value is None:
        passed = False
        reason = f"metric_status={metric.status}"
        observed = None
    elif metric.sample_count < definition.minimum_samples:
        passed = False
        reason = (
            f"sample_count={metric.sample_count} "
            f"minimum={definition.minimum_samples}"
        )
        observed = metric.value
    else:
        observed = metric.value
        passed = _compare(
            float(observed), definition.operator, definition.value
        )
        reason = f"observed={observed} {definition.operator} {definition.value}"
    return GateOutcome(
        gate_id=definition.gate_id,
        metric=definition.metric,
        source=definition.source,
        passed=passed,
        observed=observed,
        expected={
            "operator": definition.operator,
            "value": definition.value,
            "minimum_samples": definition.minimum_samples,
        },
        reason=reason,
    )


def _compare(observed: float, operator: str, expected: float | int) -> bool:
    if operator == "eq":
        return observed == expected
    if operator == "ge":
        return observed >= expected
    return observed <= expected


__all__ = [
    "evaluate_gates",
    "load_gate_configuration",
]
