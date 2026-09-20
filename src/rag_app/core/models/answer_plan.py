"""统一问答计划的冻结内部合同。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from rag_app.core.models.common import FrozenModel

ANSWER_PLAN_SCHEMA_REVISION = "wb08r-answer-plan-v3"
ANSWER_PLAN_POLICY_REVISION = "wb08r-answer-plan-policy-v3"
_MIN_AMBIGUOUS_CANDIDATES = 2


class SourceMentionRole(StrEnum):
    """来源名称在原问题中的语义角色。"""

    AUTHORITY = "AUTHORITY"
    SOURCE_OWNER = "SOURCE_OWNER"
    REFERENCED_OBJECT = "REFERENCED_OBJECT"


class AnswerOperation(StrEnum):
    """一项独立回答义务的操作类型。"""

    FIELD_LOOKUP = "FIELD_LOOKUP"
    OPEN_TEXT = "OPEN_TEXT"


class AnswerQualifierKind(StrEnum):
    """不得由基础事实自动满足的关系限定。"""

    BEFORE = "BEFORE"
    AFTER = "AFTER"
    MUST = "MUST"
    PROHIBITED = "PROHIBITED"


class QualifierStatus(StrEnum):
    """限定命题的三态受检结果。"""

    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class AnswerTaskMode(StrEnum):
    """物理执行方式；逻辑义务与调用次数保持分离。"""

    DETERMINISTIC = "DETERMINISTIC"
    GROUNDED_GENERATION = "GROUNDED_GENERATION"


class FieldResolutionStatus(StrEnum):
    """真实 schema 字段与原问之间的基础映射状态。"""

    EXACT = "EXACT"
    SUPPORTED_PARAPHRASE = "SUPPORTED_PARAPHRASE"
    RELATED_FIELD = "RELATED_FIELD"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class FieldCandidate(FrozenModel):
    """从获准 canonical 表结构生成的实际字段候选。"""

    candidate_id: str = Field(pattern=r"^F[1-9][0-9]*$")
    atom_id: str = Field(pattern=r"^A[1-4]$")
    fact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    table_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    row_index: StrictInt = Field(ge=0)
    value_column_index: StrictInt = Field(ge=0)
    target_label: str = Field(min_length=1, max_length=1000)
    field_label: str = Field(min_length=1, max_length=1000)
    value_preview: str = Field(min_length=1, max_length=320, repr=False)
    dependency_support_ids: tuple[str, ...] = Field(
        min_length=1, max_length=256
    )


class FieldResolution(FrozenModel):
    """字段解析结果；只冻结真实候选 ID，不把映射当来源证明。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    status: FieldResolutionStatus
    candidate_ids: tuple[str, ...] = Field(default=(), max_length=16)
    query_span_start: StrictInt | None = Field(default=None, ge=0)
    query_span_end: StrictInt | None = Field(default=None, gt=0)
    reason_code: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def _validate_mapping(self) -> Self:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("字段解析候选 ID 不允许重复。")
        selected = self.status in {
            FieldResolutionStatus.EXACT,
            FieldResolutionStatus.SUPPORTED_PARAPHRASE,
            FieldResolutionStatus.RELATED_FIELD,
        }
        if selected != (len(self.candidate_ids) == 1):
            raise ValueError("确定字段映射必须且只能选择一个候选。")
        if (
            self.status is FieldResolutionStatus.AMBIGUOUS
            and len(self.candidate_ids) < _MIN_AMBIGUOUS_CANDIDATES
        ):
            raise ValueError("AMBIGUOUS 至少需要两个真实竞争候选。")
        if (
            self.status is FieldResolutionStatus.NOT_FOUND
            and self.candidate_ids
        ):
            raise ValueError("NOT_FOUND 不能携带候选。")
        if (self.query_span_start is None) != (self.query_span_end is None):
            raise ValueError("字段解析原问跨度必须同时存在或同时为空。")
        if (
            self.query_span_start is not None
            and self.query_span_end is not None
            and self.query_span_end <= self.query_span_start
        ):
            raise ValueError("字段解析原问跨度不能倒置。")
        return self


class NormalizedOffsetSpan(FrozenModel):
    """一个原始字符区间到 NFKC 文本区间的映射。"""

    original_start: StrictInt = Field(ge=0)
    original_end: StrictInt = Field(gt=0)
    normalized_start: StrictInt = Field(ge=0)
    normalized_end: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _validate_ranges(self) -> Self:
        if self.original_end <= self.original_start:
            raise ValueError("原始文本跨度必须非空。")
        if self.normalized_end < self.normalized_start:
            raise ValueError("规范化文本跨度不能倒置。")
        return self


class SourceMentionSpan(FrozenModel):
    """保留来源提及的原始字符位置和实际语义角色。"""

    text: str = Field(min_length=1, max_length=320)
    role: SourceMentionRole
    original_start: StrictInt = Field(ge=0)
    original_end: StrictInt = Field(gt=0)
    scope_key: str | None = Field(default=None, pattern=r"^SOURCE_[A-H]$")
    scope_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        if self.original_end <= self.original_start:
            raise ValueError("来源提及跨度必须非空。")
        return self


class ResolvedQueryView(FrozenModel):
    """来源角色解析后供所有业务语义共同消费的问题视图。"""

    original_query: str = Field(min_length=1, max_length=8000)
    normalized_query: str = Field(min_length=1, max_length=8000)
    business_query: str = Field(min_length=1, max_length=8000)
    normalized_offsets: tuple[NormalizedOffsetSpan, ...] = Field(
        min_length=1, max_length=8000
    )
    source_mentions: tuple[SourceMentionSpan, ...] = Field(
        default=(), max_length=8
    )

    @model_validator(mode="after")
    def _validate_spans(self) -> Self:
        if len(self.normalized_offsets) != len(self.original_query):
            raise ValueError("NFKC offset 映射必须覆盖每个原始字符。")
        if any(
            mention.original_end > len(self.original_query)
            or self.original_query[
                mention.original_start : mention.original_end
            ]
            != mention.text
            for mention in self.source_mentions
        ):
            raise ValueError("来源提及必须逐字对应原始问题。")
        return self


class QualifierProofReference(FrozenModel):
    """一项限定证明的来源跨度、结构组及实际覆盖成员。"""

    support_id: str = Field(min_length=1)
    source_span_digests: tuple[str, ...] = Field(min_length=1, max_length=32)
    proof_group_id: str | None = Field(default=None, max_length=160)
    covered_member_keys: tuple[str, ...] = Field(min_length=1, max_length=128)


class AnswerQualifier(FrozenModel):
    """一项冻结在计划中的限定命题，不携带证据判定结果。"""

    obligation_id: str = Field(pattern=r"^O[1-9][0-9]*$")
    qualifier_id: str = Field(pattern=r"^Q[1-9][0-9]*$")
    kind: AnswerQualifierKind
    text: str = Field(min_length=1, max_length=80)
    query_span_start: StrictInt | None = Field(default=None, ge=0)
    query_span_end: StrictInt | None = Field(default=None, gt=0)
    subject: str | None = Field(default=None, max_length=320)
    action: str | None = Field(default=None, max_length=320)
    target_label: str | None = Field(default=None, max_length=1000)
    object_label: str | None = Field(default=None, max_length=1000)
    selected_member_refs: tuple[str, ...] = Field(default=(), max_length=128)
    event_anchor: str | None = Field(default=None, max_length=320)
    polarity: Literal["POSITIVE", "NEGATIVE"] = "POSITIVE"
    conditions: tuple[str, ...] = Field(default=(), max_length=16)
    candidate_support_ids: tuple[str, ...] = Field(default=(), max_length=256)
    structural_predicate_support_ids: tuple[str, ...] = Field(
        default=(), max_length=64
    )

    @property
    def qualifier_key(self) -> tuple[str, str]:
        """返回不可跨义务复用的限定身份。"""
        return self.obligation_id, self.qualifier_id

    @model_validator(mode="after")
    def _validate_requirement(self) -> Self:
        collections = (
            self.selected_member_refs,
            self.conditions,
            self.candidate_support_ids,
            self.structural_predicate_support_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("限定条件的成员、条件或来源身份不允许重复。")
        if (self.query_span_start is None) != (self.query_span_end is None):
            raise ValueError("限定条件的原问跨度必须同时存在或同时为空。")
        if (
            self.query_span_start is not None
            and self.query_span_end is not None
            and self.query_span_end <= self.query_span_start
        ):
            raise ValueError("限定条件的原问跨度不能倒置。")
        if not set(self.structural_predicate_support_ids) <= set(
            self.candidate_support_ids
        ):
            raise ValueError("结构谓词来源必须属于本限定的候选来源。")
        return self


class QualifierEvidenceResult(FrozenModel):
    """受检执行产物中的限定三态、定位来源和闭合证明。"""

    obligation_id: str = Field(pattern=r"^O[1-9][0-9]*$")
    qualifier_id: str = Field(pattern=r"^Q[1-9][0-9]*$")
    status: QualifierStatus
    locator_support_ids: tuple[str, ...] = Field(default=(), max_length=64)
    proof_references: tuple[QualifierProofReference, ...] = Field(
        default=(), max_length=64
    )
    covered_member_keys: tuple[str, ...] = Field(default=(), max_length=128)
    source_conditions: tuple[str, ...] = Field(default=(), max_length=16)
    reason_code: str = Field(min_length=1, max_length=120)

    @property
    def qualifier_key(self) -> tuple[str, str]:
        """返回不可跨义务复用的限定身份。"""
        return self.obligation_id, self.qualifier_id

    @property
    def support_ids(self) -> tuple[str, ...]:
        """返回已绑定真实跨度的证明来源。"""
        return tuple(
            dict.fromkeys(item.support_id for item in self.proof_references)
        )

    @model_validator(mode="after")
    def _validate_result(self) -> Self:
        collections = (
            self.locator_support_ids,
            self.covered_member_keys,
            self.source_conditions,
            tuple(item.support_id for item in self.proof_references),
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("限定证据结果的成员、条件或来源不允许重复。")
        if self.status is QualifierStatus.SUPPORTED and (
            not self.proof_references or not self.covered_member_keys
        ):
            raise ValueError("SUPPORTED 限定必须绑定跨度和覆盖成员。")
        if any(
            not set(item.covered_member_keys) <= set(self.covered_member_keys)
            for item in self.proof_references
        ):
            raise ValueError("限定证明不能覆盖未登记的业务成员。")
        return self


class AnswerMemberDependency(FrozenModel):
    """一个冻结业务成员及其正向来源依赖。"""

    member_key: str = Field(min_length=1, max_length=160)
    support_ids: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def _validate_support_ids(self) -> Self:
        if len(self.support_ids) != len(set(self.support_ids)):
            raise ValueError("成员来源 ID 不允许重复。")
        return self


class AnswerObligation(FrozenModel):
    """生成前冻结的一项逻辑回答责任。"""

    obligation_id: str = Field(pattern=r"^O[1-9][0-9]*$")
    operation: AnswerOperation
    atom_ids: tuple[str, ...] = Field(min_length=1, max_length=4)
    selection_ids: tuple[str, ...] = Field(default=(), max_length=8)
    required_member_keys: tuple[str, ...] = Field(default=(), max_length=128)
    member_dependencies: tuple[AnswerMemberDependency, ...] = Field(
        default=(), max_length=128
    )
    qualifiers: tuple[AnswerQualifier, ...] = Field(default=(), max_length=8)
    source_resolved: StrictBool
    source_closed: StrictBool

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        collections = (
            self.atom_ids,
            self.selection_ids,
            self.required_member_keys,
            tuple(item.member_key for item in self.member_dependencies),
            tuple(item.qualifier_id for item in self.qualifiers),
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("回答义务中的身份字段不允许重复。")
        if any(
            item.obligation_id != self.obligation_id for item in self.qualifiers
        ):
            raise ValueError("限定条件必须绑定所在回答义务。")
        if self.operation is AnswerOperation.FIELD_LOOKUP and (
            not self.selection_ids or not self.required_member_keys
        ):
            raise ValueError("字段查询必须冻结选择和必要成员。")
        if self.operation is AnswerOperation.OPEN_TEXT and self.selection_ids:
            raise ValueError("开放文本义务不能伪造确定性结构选择。")
        dependency_keys = {item.member_key for item in self.member_dependencies}
        if dependency_keys and dependency_keys != set(
            self.required_member_keys
        ):
            raise ValueError("成员依赖必须与冻结必要成员一一对应。")
        if (
            self.operation is AnswerOperation.OPEN_TEXT
            and self.required_member_keys
            and not dependency_keys
        ):
            raise ValueError("结构化开放义务必须冻结成员来源依赖。")
        return self


class EvidenceSelection(FrozenModel):
    """已绑定到可信物理结构的唯一来源选择。"""

    selection_id: str = Field(pattern=r"^S[1-9][0-9]*$")
    selection_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    table_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    row_index: StrictInt = Field(ge=0)
    value_column_index: StrictInt = Field(ge=0)
    target_label: str = Field(min_length=1, max_length=1000)
    field_label: str = Field(min_length=1, max_length=1000)
    member_keys: tuple[str, ...] = Field(min_length=1, max_length=128)
    value_support_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    dependency_support_ids: tuple[str, ...] = Field(
        min_length=1, max_length=256
    )
    scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    binding_basis: str = Field(min_length=1, max_length=80)
    field_candidate_id: str | None = Field(
        default=None, pattern=r"^F[1-9][0-9]*$"
    )
    field_resolution_status: FieldResolutionStatus = FieldResolutionStatus.EXACT
    field_resolution_reason: str = Field(
        default="LEGACY_EXACT_BINDING", min_length=1, max_length=120
    )
    field_query_span: tuple[StrictInt, StrictInt] | None = None

    @model_validator(mode="after")
    def _validate_dependencies(self) -> Self:
        collections = (
            self.member_keys,
            self.value_support_ids,
            self.dependency_support_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("证据选择中的成员或依赖不允许重复。")
        if not set(self.value_support_ids) <= set(self.dependency_support_ids):
            raise ValueError("值来源必须属于该事实的正向依赖闭包。")
        return self


class PhysicalTask(FrozenModel):
    """一次可共享来源的实际执行任务。"""

    task_id: str = Field(pattern=r"^T[1-9][0-9]*$")
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mode: AnswerTaskMode
    obligation_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    atom_ids: tuple[str, ...] = Field(min_length=1, max_length=4)
    selection_ids: tuple[str, ...] = Field(default=(), max_length=16)
    dependency_support_ids: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def _validate_mode(self) -> Self:
        collections = (
            self.obligation_ids,
            self.atom_ids,
            self.selection_ids,
            self.dependency_support_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("物理任务中的身份字段不允许重复。")
        if self.mode is AnswerTaskMode.DETERMINISTIC and (
            not self.selection_ids or not self.dependency_support_ids
        ):
            raise ValueError("确定性任务必须携带结构选择和正向依赖。")
        if (
            self.mode is AnswerTaskMode.GROUNDED_GENERATION
            and self.selection_ids
        ):
            raise ValueError("开放生成任务不能伪造确定性结构选择。")
        return self


class CompiledAnswerPlan(FrozenModel):
    """回答执行、覆盖与发布共同消费的唯一冻结计划。"""

    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_id: str = Field(min_length=1, max_length=200)
    scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    query_view: ResolvedQueryView
    obligations: tuple[AnswerObligation, ...] = Field(
        min_length=1, max_length=16
    )
    selections: tuple[EvidenceSelection, ...] = Field(default=(), max_length=16)
    physical_tasks: tuple[PhysicalTask, ...] = Field(min_length=1, max_length=8)
    policy_revision: str = ANSWER_PLAN_POLICY_REVISION
    schema_revision: str = ANSWER_PLAN_SCHEMA_REVISION

    @model_validator(mode="after")
    def _validate_graph(self) -> Self:
        obligation_ids = tuple(item.obligation_id for item in self.obligations)
        selection_ids = tuple(item.selection_id for item in self.selections)
        task_ids = tuple(item.task_id for item in self.physical_tasks)
        if any(
            len(values) != len(set(values))
            for values in (obligation_ids, selection_ids, task_ids)
        ):
            raise ValueError("问答计划的节点身份不允许重复。")
        obligation_set = set(obligation_ids)
        selection_set = set(selection_ids)
        if any(
            not set(item.selection_ids) <= selection_set
            for item in self.obligations
        ):
            raise ValueError("回答义务引用了不存在的证据选择。")
        if any(
            not set(task.obligation_ids) <= obligation_set
            or not set(task.selection_ids) <= selection_set
            for task in self.physical_tasks
        ):
            raise ValueError("物理任务引用了不存在的计划节点。")
        assigned = {
            obligation_id
            for task in self.physical_tasks
            for obligation_id in task.obligation_ids
        }
        if assigned != obligation_set:
            raise ValueError("每项回答义务必须恰好进入执行任务。")
        if sum(
            obligation_id in task.obligation_ids
            for obligation_id in obligation_ids
            for task in self.physical_tasks
        ) != len(obligation_ids):
            raise ValueError("回答义务不能被复制到多个物理任务。")
        return self


__all__ = [
    "ANSWER_PLAN_POLICY_REVISION",
    "ANSWER_PLAN_SCHEMA_REVISION",
    "AnswerMemberDependency",
    "AnswerObligation",
    "AnswerOperation",
    "AnswerQualifier",
    "AnswerQualifierKind",
    "AnswerTaskMode",
    "CompiledAnswerPlan",
    "EvidenceSelection",
    "FieldCandidate",
    "FieldResolution",
    "FieldResolutionStatus",
    "NormalizedOffsetSpan",
    "PhysicalTask",
    "QualifierEvidenceResult",
    "QualifierProofReference",
    "QualifierStatus",
    "ResolvedQueryView",
    "SourceMentionRole",
    "SourceMentionSpan",
]
