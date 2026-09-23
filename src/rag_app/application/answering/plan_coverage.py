"""只消费冻结问答计划和受检执行产物的覆盖归并器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rag_app.core.models import AnswerClaim
from rag_app.core.models.answer_plan import (
    CompiledAnswerPlan,
    QualifierEvidenceResult,
    QualifierStatus,
)


class AnswerPlanContractError(ValueError):
    """执行产物与冻结计划的身份或选择不一致。"""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


@dataclass(frozen=True, slots=True)
class ValidatedPlanArtifact:
    """供纯覆盖归并器消费的受检发布产物。"""

    artifact_id: str
    plan_id: str
    obligation_ids: tuple[str, ...]
    selection_digests: tuple[str, ...]
    covered_member_keys: tuple[str, ...]
    satisfied_qualifier_ids: tuple[str, ...]
    source_closed: bool
    origin: Literal["DETERMINISTIC_EXECUTION", "GROUNDED_GENERATION"]
    satisfied_qualifier_keys: tuple[tuple[str, str], ...] = ()
    qualifier_results: tuple[QualifierEvidenceResult, ...] = ()
    claim: AnswerClaim | None = None
    resource_limited: bool = False

    def __post_init__(self) -> None:
        """拒绝重复身份和没有终态的空产物。"""
        collections = (
            self.obligation_ids,
            self.selection_digests,
            self.covered_member_keys,
            self.satisfied_qualifier_ids,
            self.satisfied_qualifier_keys,
            tuple(item.qualifier_key for item in self.qualifier_results),
        )
        if not self.artifact_id or any(
            len(values) != len(set(values)) for values in collections
        ):
            raise AnswerPlanContractError("ANSWER_ARTIFACT_IDENTITY_INVALID")
        if (self.claim is None) == (not self.resource_limited):
            raise AnswerPlanContractError("ANSWER_ARTIFACT_TERMINAL_INVALID")
        if (
            self.satisfied_qualifier_keys
            and tuple(item[1] for item in self.satisfied_qualifier_keys)
            != self.satisfied_qualifier_ids
        ):
            raise AnswerPlanContractError("ANSWER_QUALIFIER_KEY_INVALID")
        supported = {
            item.qualifier_key
            for item in self.qualifier_results
            if item.status is QualifierStatus.SUPPORTED
        }
        if (
            self.qualifier_results
            and not set(self.satisfied_qualifier_keys) <= supported
        ):
            raise AnswerPlanContractError("ANSWER_QUALIFIER_RESULT_INVALID")


@dataclass(frozen=True, slots=True)
class ObligationCoverage:
    """单项义务的必要成员、限定和执行终态。"""

    obligation_id: str
    status: Literal["FULL", "PARTIAL", "MISSING", "RESOURCE_LIMITED"]
    covered_member_keys: tuple[str, ...]
    missing_member_keys: tuple[str, ...]
    satisfied_qualifier_ids: tuple[str, ...]
    missing_qualifier_ids: tuple[str, ...]
    satisfied_qualifier_keys: tuple[tuple[str, str], ...]
    missing_qualifier_keys: tuple[tuple[str, str], ...]
    source_closed: bool


@dataclass(frozen=True, slots=True)
class CompiledPlanCoverage:
    """不读取原问的请求级覆盖归并结果。"""

    plan_id: str
    obligations: tuple[ObligationCoverage, ...]

    @property
    def complete(self) -> bool:
        """全部冻结义务均为 FULL 时才算完整。"""
        return bool(self.obligations) and all(
            item.status == "FULL" for item in self.obligations
        )


def reduce_plan_coverage(  # noqa: PLR0912
    plan: CompiledAnswerPlan,
    artifacts: tuple[ValidatedPlanArtifact, ...],
) -> CompiledPlanCoverage:
    """只按冻结义务和受检产物归并完整性。

    Args:
        plan: 生成前冻结的唯一问答计划。
        artifacts: 确定性执行或开放生成形成的受检产物。

    Returns:
        每项义务的必要成员和限定缺口。

    Raises:
        AnswerPlanContractError: 产物来自其它计划或替换了结构选择。

    """
    obligations = {item.obligation_id: item for item in plan.obligations}
    selections = {item.selection_id: item for item in plan.selections}
    for artifact in artifacts:
        if artifact.plan_id != plan.plan_id:
            raise AnswerPlanContractError("ANSWER_PLAN_IDENTITY_CHANGED")
        if not set(artifact.obligation_ids) <= obligations.keys():
            raise AnswerPlanContractError("ANSWER_ARTIFACT_UNKNOWN_OBLIGATION")
        expected_digests = {
            selections[selection_id].selection_digest
            for obligation_id in artifact.obligation_ids
            for selection_id in obligations[obligation_id].selection_ids
        }
        if not set(artifact.selection_digests) <= expected_digests:
            raise AnswerPlanContractError("ANSWER_SELECTION_IDENTITY_CHANGED")
        expected_members = {
            member
            for obligation_id in artifact.obligation_ids
            for member in obligations[obligation_id].required_member_keys
        }
        if not set(artifact.covered_member_keys) <= expected_members:
            raise AnswerPlanContractError("ANSWER_MEMBER_IDENTITY_CHANGED")
        expected_qualifiers = {
            qualifier.qualifier_key
            for obligation_id in artifact.obligation_ids
            for qualifier in obligations[obligation_id].qualifiers
        }
        artifact_keys = (
            set(artifact.satisfied_qualifier_keys)
            if artifact.satisfied_qualifier_keys
            else {
                (artifact.obligation_ids[0], qualifier_id)
                for qualifier_id in artifact.satisfied_qualifier_ids
            }
            if len(artifact.obligation_ids) == 1
            else set()
        )
        if not artifact_keys <= expected_qualifiers:
            raise AnswerPlanContractError("ANSWER_QUALIFIER_IDENTITY_CHANGED")
        if any(
            result.qualifier_key not in expected_qualifiers
            or result.obligation_id not in artifact.obligation_ids
            for result in artifact.qualifier_results
        ):
            raise AnswerPlanContractError("ANSWER_QUALIFIER_IDENTITY_CHANGED")
        required_selection = any(
            obligations[obligation_id].selection_ids
            for obligation_id in artifact.obligation_ids
        )
        if (
            required_selection
            and artifact.claim is not None
            and not set(artifact.selection_digests) == expected_digests
        ):
            raise AnswerPlanContractError("ANSWER_SELECTION_IDENTITY_CHANGED")

    coverage: list[ObligationCoverage] = []
    for obligation in plan.obligations:
        related = tuple(
            artifact
            for artifact in artifacts
            if obligation.obligation_id in artifact.obligation_ids
        )
        covered = {
            member
            for artifact in related
            for member in artifact.covered_member_keys
        }
        required = tuple(obligation.required_member_keys)
        missing_members = tuple(
            member for member in required if member not in covered
        )
        satisfied_keys = {
            qualifier_key
            for artifact in related
            for qualifier_key in (
                artifact.satisfied_qualifier_keys
                or tuple(
                    (obligation.obligation_id, qualifier_id)
                    for qualifier_id in artifact.satisfied_qualifier_ids
                )
            )
        }
        required_qualifier_keys = tuple(
            qualifier.qualifier_key for qualifier in obligation.qualifiers
        )
        missing_qualifier_keys = tuple(
            qualifier_key
            for qualifier_key in required_qualifier_keys
            if qualifier_key not in satisfied_keys
        )
        missing_qualifiers = tuple(item[1] for item in missing_qualifier_keys)
        source_closed = obligation.source_closed or any(
            artifact.source_closed for artifact in related
        )
        validated = any(
            artifact.claim is not None and not artifact.resource_limited
            for artifact in related
        )
        limited = any(artifact.resource_limited for artifact in related)
        if limited and (
            not validated
            or missing_members
            or missing_qualifiers
            or not source_closed
        ):
            status: Literal[
                "FULL", "PARTIAL", "MISSING", "RESOURCE_LIMITED"
            ] = "RESOURCE_LIMITED"
        elif (
            obligation.source_resolved
            and source_closed
            and validated
            and not missing_members
            and not missing_qualifiers
            and (
                not obligation.collection_requires_member_proof
                or bool(required)
            )
        ):
            status = "FULL"
        elif validated:
            status = "PARTIAL"
        else:
            status = "MISSING"
        coverage.append(
            ObligationCoverage(
                obligation_id=obligation.obligation_id,
                status=status,
                covered_member_keys=tuple(
                    member for member in required if member in covered
                ),
                missing_member_keys=missing_members,
                satisfied_qualifier_ids=tuple(
                    qualifier_key[1]
                    for qualifier_key in required_qualifier_keys
                    if qualifier_key in satisfied_keys
                ),
                missing_qualifier_ids=missing_qualifiers,
                satisfied_qualifier_keys=tuple(
                    qualifier_key
                    for qualifier_key in required_qualifier_keys
                    if qualifier_key in satisfied_keys
                ),
                missing_qualifier_keys=missing_qualifier_keys,
                source_closed=source_closed,
            )
        )
    return CompiledPlanCoverage(
        plan_id=plan.plan_id,
        obligations=tuple(coverage),
    )


__all__ = [
    "AnswerPlanContractError",
    "CompiledPlanCoverage",
    "ObligationCoverage",
    "ValidatedPlanArtifact",
    "reduce_plan_coverage",
]
