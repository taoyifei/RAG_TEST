"""P11 独立 pilot 的双路质量判定，沿用 V3 与 R2 已接受阈值。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evaluation.v2.gates import evaluate_gates, load_gate_configuration
from evaluation.v2.metrics import MetricContext, compute_metric_report
from evaluation.v2.models import (
    CaseObservation,
    EvaluationCase,
    GateOutcome,
    GateReport,
    MetricReport,
)
from rag_app.product import quality as quality_contract

_LANES = ("primary", "standby")
_GATES = Path(__file__).parent / "gates" / "p08-gates.json"


class PilotLiveEvidence(BaseModel):
    """由受信任 Runner 从真实 Runtime 和持久账本构造的安全身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    validation_mode: Literal["offline", "mock", "live"]
    profile_revision_id: str
    binding_identity: str
    campaign_id: str
    dataset_sha256: str
    case_attempts: dict[str, tuple[str, ...]]
    provider_models: tuple[str, ...] = Field(min_length=1)


class PilotReport(BaseModel):
    """小样本结果明确区分离线观测与已接受的真实质量。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["PASS", "FAIL", "BLOCKED", "NOT_RUN"]
    reason: str
    pilot: Literal[True] = True
    sample_count: int
    positive_samples: int
    negative_samples: int
    metrics: dict[str, MetricReport]
    gates: dict[str, GateReport]
    failures: dict[str, tuple[str, ...]]
    identity: PilotLiveEvidence | None = None


def evaluate_pilot(
    cases: tuple[EvaluationCase, ...],
    observations: dict[str, tuple[CaseObservation, ...]],
    identity: PilotLiveEvidence | None = None,
) -> PilotReport:
    """以同一固定标签分别评估主路和备用路，不合并伪增样本量。

    Args:
        cases: 查询前绑定的独立 holdout 标签。
        observations: 实际 V3 观测，键必须为 primary 和 standby。
        identity: 受信任 Runner 从持久 HTTP 账本取得的逐 Case 证明。

    Returns:
        每路指标、未降低的门禁和失败 Case；离线只能为 NOT_RUN。

    """
    metrics: dict[str, MetricReport] = {}
    gates: dict[str, GateReport] = {}
    failures: dict[str, tuple[str, ...]] = {}
    for lane in _LANES:
        lane_observations = observations.get(lane, ())
        if {item.case_id for item in lane_observations} != {
            case.case_id for case in cases
        }:
            continue
        metrics[lane] = compute_metric_report(
            cases,
            lane_observations,
            context=MetricContext(
                lane=f"live-{lane}"
                if identity is not None and identity.validation_mode == "live"
                else "offline-structural",
                variant_id="p11-fixed-profile",
                split="holdout",
                seed=20260905,
            ),
        )
        original = evaluate_gates(
            metrics[lane], load_gate_configuration(_GATES)
        )
        outcomes = (*original.outcomes, *_r2_gates(cases, metrics[lane]))
        gates[lane] = GateReport(
            passed=all(item.passed for item in outcomes), outcomes=outcomes
        )
        failures[lane] = _failed_cases(cases, lane_observations)
    status: Literal["PASS", "FAIL", "BLOCKED", "NOT_RUN"]
    if identity is None or identity.validation_mode != "live":
        status, reason = "NOT_RUN", "REAL_MODEL_QUALITY_NOT_RUN"
    elif set(metrics) != set(_LANES):
        status, reason = "BLOCKED", "INCOMPLETE_DUAL_LANE_LABEL_EVALUATION"
    elif not _live_provenance(cases, observations, identity):
        status, reason = "BLOCKED", "MISSING_CASE_BOUND_LIVE_ATTEMPTS_OR_ROUTE"
    elif all(item.passed for item in gates.values()):
        status, reason = "PASS", "INDEPENDENT_PILOT_ACCEPTED"
    else:
        status, reason = "FAIL", "ACCEPTED_QUALITY_THRESHOLDS_NOT_MET"
    positive = sum(case.expected.answerable for case in cases)
    return PilotReport(
        status=status,
        reason=reason,
        sample_count=len(cases),
        positive_samples=positive,
        negative_samples=len(cases) - positive,
        metrics=metrics,
        gates=gates,
        failures=failures,
        identity=identity,
    )


def _r2_gates(
    cases: tuple[EvaluationCase, ...], metrics: MetricReport
) -> tuple[GateOutcome, ...]:
    positive = sum(case.expected.answerable for case in cases)
    negative = len(cases) - positive
    definitions = (
        (
            "r2_labeled_queries",
            positive,
            quality_contract._MIN_LABELED_QUERIES,
            "ge",
        ),
        (
            "r2_negative_queries",
            negative,
            quality_contract._MIN_NEGATIVE_QUERIES,
            "ge",
        ),
        (
            "citation_source_precision",
            metrics.metrics["citation_source_precision"].value,
            quality_contract._MIN_SOURCE_PRECISION,
            "ge",
        ),
        (
            "recall_at_5",
            metrics.metrics["recall_at_5"].value,
            quality_contract._MIN_RECALL,
            "ge",
        ),
        (
            "negative_leakage_at_10",
            metrics.metrics["negative_leakage_at_10"].value,
            0,
            "eq",
        ),
    )
    outcomes = []
    for name, observed, expected, operator in definitions:
        passed = observed is not None and (
            observed == expected if operator == "eq" else observed >= expected
        )
        outcomes.append(
            GateOutcome(
                gate_id=f"p11_{name}",
                metric=name,
                source="accepted product requirement",
                passed=passed,
                observed=observed,
                expected={"operator": operator, "value": expected},
                reason=f"observed={observed} {operator} {expected}",
            )
        )
    return tuple(outcomes)


def _live_provenance(
    cases: tuple[EvaluationCase, ...],
    observations: dict[str, tuple[CaseObservation, ...]],
    identity: PilotLiveEvidence,
) -> bool:
    expected_keys = {
        f"{lane}:{case.case_id}" for lane in _LANES for case in cases
    }
    attempts = identity.case_attempts
    if set(attempts) != expected_keys or any(
        not ids for ids in attempts.values()
    ):
        return False
    all_ids = [identifier for ids in attempts.values() for identifier in ids]
    if len(all_ids) != len(set(all_ids)):
        return False
    return all(
        item.selected_embedding_slot == lane
        and item.selected_vector_name == f"dense_{lane}"
        and not item.cache_hit
        and item.embedding_call_count > 0
        for lane in _LANES
        for item in observations[lane]
    )


def _failed_cases(
    cases: tuple[EvaluationCase, ...],
    observations: tuple[CaseObservation, ...],
) -> tuple[str, ...]:
    labels = {case.case_id: case for case in cases}
    failed = []
    for item in observations:
        label = labels[item.case_id]
        expected = label.expected
        if (
            (item.status == "ANSWERABLE") != expected.answerable
            or item.wrong_scope_hit_count
            or item.wrong_revision_hit_count
            or item.wrong_vector_space_attempt_count
            or set(item.retrieved_document_ids)
            & set(label.constraints.forbidden_document_ids)
            or (
                expected.answerable
                and (
                    not item.citation_valid
                    or item.matched_source_range_count
                    != item.required_source_range_count
                    or not set(item.reranked_chunk_ids[:5])
                    & set(expected.relevant_chunk_ids)
                    or set(item.evidence_chunk_ids)
                    - set(expected.relevant_chunk_ids)
                )
            )
        ):
            failed.append(item.case_id)
    return tuple(failed)
