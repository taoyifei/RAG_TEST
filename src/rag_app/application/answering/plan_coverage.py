"""只消费冻结问答计划和受检执行产物的覆盖归并器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rag_app.core.models import AnswerClaim
from rag_app.core.models.answer_plan import CompiledAnswerPlan


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
    claim: AnswerClaim | None = None
    resource_limited: bool = False

    def __post_init__(self) -> None:
        """拒绝重复身份和没有终态的空产物。"""
        collections = (
            self.obligation_ids,
            self.selection_digests,
            self.covered_member_keys,
            self.satisfied_qualifier_ids,
        )
        if not self.artifact_id or any(
            len(values) != len(set(values)) for values in collections
        ):
            raise AnswerPlanContractError("ANSWER_ARTIFACT_IDENTITY_INVALID")
        if (self.claim is None) == (not self.resource_limited):
            raise AnswerPlanContractError("ANSWER_ARTIFACT_TERMINAL_INVALID")


@dataclass(frozen=True, slots=True)
class ObligationCoverage:
    """单项义务的必要成员、限定和执行终态。"""

    obligation_id: str
    status: Literal["FULL", "PARTIAL", "MISSING", "RESOURCE_LIMITED"]
    covered_member_keys: tuple[str, ...]
    missing_member_keys: tuple[str, ...]
    satisfied_qualifier_ids: tuple[str, ...]
    missing_qualifier_ids: tuple[str, ...]
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


def reduce_plan_coverage(
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
            qualifier.qualifier_id
            for obligation_id in artifact.obligation_ids
            for qualifier in obligations[obligation_id].qualifiers
        }
        if not set(artifact.satisfied_qualifier_ids) <= expected_qualifiers:
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
        satisfied = {
            qualifier_id
            for artifact in related
            for qualifier_id in artifact.satisfied_qualifier_ids
        }
        required_qualifiers = tuple(
            qualifier.qualifier_id for qualifier in obligation.qualifiers
        )
        missing_qualifiers = tuple(
            qualifier_id
            for qualifier_id in required_qualifiers
            if qualifier_id not in satisfied
        )
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
                    qualifier_id
                    for qualifier_id in required_qualifiers
                    if qualifier_id in satisfied
                ),
                missing_qualifier_ids=missing_qualifiers,
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
