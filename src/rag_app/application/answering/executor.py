"""执行冻结问答计划中的确定性来源事实任务。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.application.answering.plan_coverage import (
    CompiledPlanCoverage,
    ValidatedPlanArtifact,
)
from rag_app.application.answering.source_projection import (
    SourceProjectionError,
    render_physical_table_fact,
)
from rag_app.application.retrieval.generation_evidence import (
    GenerationEvidencePack,
)
from rag_app.core.errors import QueryCancelled
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.answer_plan import (
    AnswerTaskMode,
    CompiledAnswerPlan,
    EvidenceSelection,
)
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.retrieval import (
    AnswerClaim,
    ClaimSupport,
    EvidenceItem,
    PhysicalTableFact,
)
from rag_app.core.ports.cancellation import CancellationPort


class AnswerExecutionError(ValueError):
    """冻结计划与运行时来源发生不可发布的不一致。"""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


@dataclass(frozen=True, slots=True)
class DeterministicExecutionRecord:
    """不伪装模型传输的确定性执行审计记录。"""

    task_id: str
    plan_id: str
    selection_digest: str
    source_digest: str
    checked_support_ids: tuple[str, ...]
    published_member_keys: tuple[str, ...]
    satisfied_qualifier_ids: tuple[str, ...]
    claim_sha256: str
    origin: str = "DETERMINISTIC_EXECUTION"


@dataclass(frozen=True, slots=True)
class AnswerExecutionResult:
    """确定性产物与仍需开放生成的义务。"""

    artifacts: tuple[ValidatedPlanArtifact, ...]
    records: tuple[DeterministicExecutionRecord, ...]
    deferred_obligation_ids: tuple[str, ...]


def _validate_selection(
    selection: EvidenceSelection,
    fact: PhysicalTableFact,
    registry: dict[str, EvidenceItem],
) -> tuple[EvidenceItem, ...]:
    """执行前重验事实身份、正向依赖和可引用跨度。"""
    if (
        fact.fact_id != selection.fact_id
        or fact.document_id != selection.document_id
        or fact.document_version_id != selection.document_version_id
        or fact.table_key != selection.table_key
        or fact.row_index != selection.row_index
        or fact.value_column_index != selection.value_column_index
        or fact.all_support_ids != selection.dependency_support_ids
        or fact.value_support_ids != selection.value_support_ids
        or selection.member_keys != (fact.fact_id,)
    ):
        raise AnswerExecutionError("ANSWER_SELECTION_RUNTIME_MISMATCH")
    try:
        sources = tuple(
            registry[support_id]
            for support_id in selection.dependency_support_ids
        )
    except KeyError as error:
        raise AnswerExecutionError("ANSWER_SELECTION_SOURCE_MISSING") from error
    if any(
        item.document_id != selection.document_id
        or item.document_version_id != selection.document_version_id
        or not item.publishable
        or not item.source_spans
        or any(not span.is_citable for span in item.source_spans)
        for item in sources
    ):
        raise AnswerExecutionError("ANSWER_SELECTION_SOURCE_INVALID")
    return sources


def execute_deterministic_tasks(
    plan: CompiledAnswerPlan,
    pack: GenerationEvidencePack,
    *,
    cancellation: CancellationPort | None = None,
) -> AnswerExecutionResult:
    """读取并发布 D 路径事实，不调用生成或语义复核。

    Args:
        plan: 已冻结的统一问答计划。
        pack: 编译时所用的授权来源和物理事实。
        cancellation: 可选的请求取消端口。

    Returns:
        可发布事实、SAFE 执行记录和仍需 G 路径处理的义务。

    Raises:
        AnswerExecutionError: 运行时来源与冻结选择不一致。
        QueryCancelled: 请求在执行过程中被取消。

    """
    registry = {item.support_id: item for item in pack.evidence}
    if len(registry) != len(pack.evidence):
        raise AnswerExecutionError("ANSWER_EXECUTION_DUPLICATE_SUPPORT_ID")
    facts = {item.fact_id: item for item in pack.physical_table_facts}
    selections = {item.selection_id: item for item in plan.selections}
    obligations = {item.obligation_id: item for item in plan.obligations}
    artifacts: list[ValidatedPlanArtifact] = []
    records: list[DeterministicExecutionRecord] = []
    deferred: list[str] = []
    for task in plan.physical_tasks:
        if cancellation is not None and cancellation.is_cancelled():
            raise QueryCancelled("QUERY_CANCELLED")
        if task.mode is AnswerTaskMode.GROUNDED_GENERATION:
            deferred.extend(task.obligation_ids)
            continue
        for selection_id in task.selection_ids:
            selection = selections[selection_id]
            fact = facts.get(selection.fact_id)
            if fact is None:
                raise AnswerExecutionError("ANSWER_SELECTION_FACT_MISSING")
            sources = _validate_selection(selection, fact, registry)
            related_obligations = tuple(
                obligation
                for obligation_id in task.obligation_ids
                if selection_id
                in (obligation := obligations[obligation_id]).selection_ids
            )
            if not related_obligations:
                raise AnswerExecutionError("ANSWER_SELECTION_UNUSED")
            try:
                text = render_physical_table_fact(fact, registry)
            except SourceProjectionError as error:
                raise AnswerExecutionError(error.failure_code) from error
            claim = AnswerClaim(
                text=text,
                supports=tuple(
                    ClaimSupport(
                        support_id=item.support_id,
                        quote=item.citation_text,
                    )
                    for item in sources
                ),
            )
            qualifier_ids = tuple(
                qualifier.qualifier_id
                for obligation in related_obligations
                for qualifier in obligation.qualifiers
                if qualifier.supported
                and set(qualifier.support_ids)
                <= set(selection.dependency_support_ids)
            )
            obligation_ids = tuple(
                item.obligation_id for item in related_obligations
            )
            artifact = ValidatedPlanArtifact(
                artifact_id=f"D{len(artifacts) + 1}",
                plan_id=plan.plan_id,
                obligation_ids=obligation_ids,
                selection_digests=(selection.selection_digest,),
                covered_member_keys=selection.member_keys,
                satisfied_qualifier_ids=tuple(dict.fromkeys(qualifier_ids)),
                source_closed=True,
                origin="DETERMINISTIC_EXECUTION",
                claim=claim,
            )
            artifacts.append(artifact)
            records.append(
                DeterministicExecutionRecord(
                    task_id=task.task_id,
                    plan_id=plan.plan_id,
                    selection_digest=selection.selection_digest,
                    source_digest=canonical_sha256(
                        tuple(stable_support_key(item) for item in sources)
                    ),
                    checked_support_ids=tuple(
                        item.support_id for item in sources
                    ),
                    published_member_keys=selection.member_keys,
                    satisfied_qualifier_ids=artifact.satisfied_qualifier_ids,
                    claim_sha256=canonical_sha256(claim.text),
                )
            )
    return AnswerExecutionResult(
        artifacts=tuple(artifacts),
        records=tuple(records),
        deferred_obligation_ids=tuple(dict.fromkeys(deferred)),
    )


def render_deterministic_answer(
    plan: CompiledAnswerPlan,
    execution: AnswerExecutionResult,
    coverage: CompiledPlanCoverage,
) -> str | None:
    """只组织受检事实和冻结限定缺口，不调用润色模型。"""
    lines: list[str] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for artifact in execution.artifacts:
        if artifact.claim is None:
            continue
        fact = (
            artifact.claim.text,
            tuple(item.support_id for item in artifact.claim.supports),
        )
        if fact in seen:
            continue
        seen.add(fact)
        citations = " ".join(
            f"[{item.support_id}]" for item in artifact.claim.supports
        )
        lines.append(f"{artifact.claim.text} {citations}")
    missing_qualifiers = {
        qualifier_id
        for item in coverage.obligations
        for qualifier_id in item.missing_qualifier_ids
    }
    qualifier_text = tuple(
        dict.fromkeys(
            qualifier.text
            for obligation in plan.obligations
            for qualifier in obligation.qualifiers
            if qualifier.qualifier_id in missing_qualifiers
        )
    )
    if qualifier_text:
        joined = "、".join(f"“{item}”" for item in qualifier_text)
        lines.append(
            f"现有来源未直接证明{joined}这些限定，因此不把它们作为结论。"
        )
    return "\n".join(lines) if lines else None


def compiled_atom_coverage(
    plan: CompiledAnswerPlan,
    coverage: CompiledPlanCoverage,
) -> tuple[tuple[str, str], ...]:
    """把义务覆盖稳定映射回现有逐 Atom 公开诊断。"""
    by_obligation = {item.obligation_id: item for item in coverage.obligations}
    result: list[tuple[str, str]] = []
    atom_ids = tuple(
        dict.fromkeys(
            atom_id
            for obligation in plan.obligations
            for atom_id in obligation.atom_ids
        )
    )
    for atom_id in atom_ids:
        statuses = tuple(
            by_obligation[obligation.obligation_id].status
            for obligation in plan.obligations
            if atom_id in obligation.atom_ids
        )
        status = (
            "SUPPORTED"
            if statuses and all(item == "FULL" for item in statuses)
            else "PARTIAL"
            if any(item in {"FULL", "PARTIAL"} for item in statuses)
            else "MISSING"
        )
        result.append((atom_id, status))
    return tuple(result)


__all__ = [
    "AnswerExecutionError",
    "AnswerExecutionResult",
    "DeterministicExecutionRecord",
    "compiled_atom_coverage",
    "execute_deterministic_tasks",
    "render_deterministic_answer",
]
