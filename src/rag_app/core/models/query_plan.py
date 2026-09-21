"""复合查询的有界事实原子与逐原子证据状态。"""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query import QueryAnalysis, RequestedAnswerType

QUERY_PLAN_SCHEMA_REVISION = "wb08r-query-plan-v11"
QUERY_UNIT_FUSION_REVISION = "wb08r-root-atom-fusion-v3"
ATOM_GROUP_ALIGNMENT_REVISION = "wb08r-atom-group-alignment-v3"
EVIDENCE_GROUP_SCHEMA_REVISION = "wb08r-evidence-group-v2"
GROUNDED_CLAIM_SCHEMA_REVISION = "wb08r-grounded-wire-v10"
NATURAL_RENDERER_REVISION = "wb08r-natural-renderer-v6"
CORRECTIVE_RETRIEVAL_REVISION = "wb08r-per-atom-correction-v2"
SOURCE_SCOPE_SCHEMA_REVISION = "wb08r-source-scope-v2"
_MAX_ATOMS = 4
ReasoningEffortValue = Literal["DIRECT", "ASSISTED", "DEEP"]


class AtomAnswerShape(StrEnum):
    """与具体业务无关的事实回答形状。"""

    FACT = "FACT"
    DEFINITION = "DEFINITION"
    ENUMERATION = "ENUMERATION"
    PROCEDURE = "PROCEDURE"
    DUTIES = "DUTIES"
    RESPONSIBLE_PARTY = "RESPONSIBLE_PARTY"
    DURATION = "DURATION"
    COUNT = "COUNT"
    COMPARISON = "COMPARISON"
    CATALOG_REFERENCE = "CATALOG_REFERENCE"


class AtomConstraintKind(StrEnum):
    """Planner 必须保留的字面限制。"""

    NUMBER = "NUMBER"
    DURATION = "DURATION"
    DATE_TIME = "DATE_TIME"
    VERSION = "VERSION"
    NEGATION = "NEGATION"
    SOURCE = "SOURCE"
    ROLE = "ROLE"


class QueryConstraint(FrozenModel):
    """单个事实原子的原文限制，不作为检索过滤条件。"""

    kind: AtomConstraintKind
    value: str = Field(min_length=1, max_length=160)
    polarity: Literal["POSITIVE", "NEGATIVE"] | None = None
    unit: str | None = Field(default=None, max_length=40)


class SourceIntent(StrEnum):
    """原问题中来源名称对当前 Atom 的约束用途。"""

    OPEN = "OPEN"
    DOCUMENT_AUTHORITY = "DOCUMENT_AUTHORITY"
    DOCUMENT_SET = "DOCUMENT_SET"
    MENTION_IN_SOURCE = "MENTION_IN_SOURCE"


class SourceResolution(StrEnum):
    """来源名称解析到活动文档身份后的确定性状态。"""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"
    CATALOG_ONLY = "CATALOG_ONLY"


class SourceContentRequirement(StrEnum):
    """当前问题要求来源证明的内容层级。"""

    BODY = "BODY"
    EXISTENCE = "EXISTENCE"
    REFERENCE = "REFERENCE"


class SourceDocumentIdentity(FrozenModel):
    """用户指定来源解析出的逻辑文档及不可变版本。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")


class SourceScopeDecision(FrozenModel):
    """逐 Atom 的来源所有权合同；空许可绝不代表开放查询。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    source_intent: SourceIntent
    resolution: SourceResolution
    allowed_documents: tuple[SourceDocumentIdentity, ...] = ()
    required_content: SourceContentRequirement = SourceContentRequirement.BODY
    mention_sha256: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    registry_revision: str = Field(min_length=1, max_length=160)
    scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_resolution(self) -> Self:
        identities = tuple(
            (item.document_id, item.document_version_id)
            for item in self.allowed_documents
        )
        if len(identities) != len(set(identities)):
            raise ValueError("来源许可文档身份不允许重复。")
        if self.resolution is SourceResolution.OPEN:
            if (
                self.source_intent is not SourceIntent.OPEN
                or self.allowed_documents
                or self.mention_sha256 is not None
            ):
                raise ValueError("OPEN 来源范围不能携带文档许可或来源提及。")
        elif self.source_intent is SourceIntent.OPEN:
            raise ValueError("显式来源解析状态不能使用 OPEN 意图。")
        elif self.resolution is SourceResolution.RESOLVED:
            if not self.allowed_documents or self.mention_sha256 is None:
                raise ValueError("RESOLVED 来源范围必须包含身份与提及摘要。")
        elif self.allowed_documents:
            raise ValueError("未确定来源不能签发文档许可。")
        return self


class QueryAtom(FrozenModel):
    """一项可以独立检索和验证的用户要求。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    target: str = Field(min_length=1, max_length=160)
    relation: str = Field(min_length=1, max_length=160)
    answer_shape: AtomAnswerShape
    source_qualifier: str | None = Field(default=None, max_length=160)
    constraints: tuple[QueryConstraint, ...] = Field(default=(), max_length=12)
    original_fragment: str | None = Field(default=None, max_length=320)
    source_scope: SourceScopeDecision | None = Field(
        default=None, exclude=True, repr=False
    )

    @property
    def search_text(self) -> str:
        """组合当前原子检索文本，不补充知识库事实。"""
        return " ".join(
            dict.fromkeys(
                part.strip()
                for part in (
                    self.source_qualifier,
                    self.target,
                    self.relation,
                    self.original_fragment,
                )
                if part and part.strip()
            )
        )

    @property
    def dedup_key(self) -> str:
        """按通用语义字段去重。"""

        def normalized(value: str | None) -> str:
            return " ".join(
                unicodedata.normalize("NFKC", value or "").casefold().split()
            )

        return canonical_sha256(
            {
                "target": normalized(self.target),
                "relation": normalized(self.relation),
                "shape": self.answer_shape.value,
                "source": normalized(self.source_qualifier),
                "source_scope": (
                    self.source_scope.scope_digest
                    if self.source_scope is not None
                    else None
                ),
                "constraints": sorted(
                    (
                        item.kind.value,
                        normalized(item.value),
                        item.polarity,
                        normalized(item.unit),
                    )
                    for item in self.constraints
                ),
            }
        )


class QueryPlan(FrozenModel):
    """同一请求的一次有界 Planner 结果。"""

    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    standalone_query: str = Field(min_length=1, max_length=512)
    original_query: str = Field(min_length=1, max_length=8000)
    resolved_root_query: str = Field(min_length=1, max_length=512)
    context_resolution_mode: str = "ORIGINAL"
    context_resolution_revision: str = "wb08r-context-resolution-v2"
    context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    referenced_span_ids: tuple[str, ...] = ()
    intent: str = Field(min_length=1, max_length=40)
    effort: ReasoningEffortValue
    atoms: tuple[QueryAtom, ...] = Field(min_length=1, max_length=4)
    needs_clarification: StrictBool = False
    clarification_question: str | None = Field(default=None, max_length=200)
    route_hints: tuple[str, ...] = Field(default=(), max_length=4)
    planner_reason_code: str = Field(min_length=1, max_length=120)
    planner_called: StrictBool = False
    fallback_mode: str | None = None
    coverage_confidence: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH"

    @model_validator(mode="after")
    def _unique_atoms(self) -> Self:
        if tuple(atom.atom_id for atom in self.atoms) != tuple(
            f"A{index}" for index in range(1, len(self.atoms) + 1)
        ):
            raise ValueError("Atom ID 必须连续且唯一。")
        if len({atom.dedup_key for atom in self.atoms}) != len(self.atoms):
            raise ValueError("QueryPlan 不接受重复原子。")
        if any(
            atom.source_scope is not None
            and atom.source_scope.atom_id != atom.atom_id
            for atom in self.atoms
        ):
            raise ValueError("来源范围必须与所在 Atom 身份一致。")
        return self


class AtomStatus(StrEnum):
    """逐原子的证据支持等级。"""

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CONTRADICTORY = "CONTRADICTORY"


class AtomCandidateLink(FrozenModel):
    """保留初召回的 atom 和通道来源。"""

    atom_id: str | None = Field(pattern=r"^A[1-4]$")
    unit_id: str | None = None
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    group_id: str | None = None
    channels: tuple[str, ...] = Field(min_length=1)
    best_rank: StrictInt = Field(gt=0)
    unit_fusion_rank: StrictInt | None = Field(default=None, gt=0)
    best_channel_rank: StrictInt | None = Field(default=None, gt=0)
    rerank_rank: StrictInt | None = Field(default=None, gt=0)
    score: float


class AtomCoverage(FrozenModel):
    """初召回至组选择的逐原子审计。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    source_hit: StrictBool
    reranker_hit: StrictBool
    candidate_group_ids: tuple[str, ...] = ()
    status: AtomStatus
    reason_codes: tuple[str, ...] = ()


class AtomSupport(FrozenModel):
    """进入生成前的逐原子支持集合与确定性核验。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    status: AtomStatus
    supporting_group_ids: tuple[str, ...] = ()
    supporting_support_ids: tuple[str, ...] = ()
    supporting_support_keys: tuple[str, ...] = Field(default=(), exclude=True)
    relation_certified_group_ids: tuple[str, ...] = ()
    missing_aspects: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    deterministic_checks: tuple[tuple[str, bool], ...] = ()


class AtomSupportMatrix(FrozenModel):
    """与 QueryPlan 一一对应的支持状态。"""

    atoms: tuple[AtomSupport, ...] = Field(min_length=1, max_length=4)

    def for_atom(self, atom_id: str) -> AtomSupport:
        """按原子身份读取支持结果。"""
        for atom in self.atoms:
            if atom.atom_id == atom_id:
                return atom
        raise KeyError(atom_id)


_ANSWER_SHAPES = {
    RequestedAnswerType.DEFINITION: AtomAnswerShape.DEFINITION,
    RequestedAnswerType.ENUMERATION: AtomAnswerShape.ENUMERATION,
    RequestedAnswerType.PROCEDURE: AtomAnswerShape.PROCEDURE,
    RequestedAnswerType.DUTIES: AtomAnswerShape.DUTIES,
    RequestedAnswerType.RESPONSIBLE_PARTY: AtomAnswerShape.RESPONSIBLE_PARTY,
    RequestedAnswerType.COUNT: AtomAnswerShape.COUNT,
}


def fallback_query_plan(  # noqa: PLR0913
    analysis: QueryAnalysis,
    *,
    effort: ReasoningEffortValue,
    reason_code: str,
    planner_called: bool,
    original_query: str | None = None,
    resolved_root_query: str | None = None,
    context_resolution_mode: str = "ORIGINAL",
    context_digest: str | None = None,
    referenced_span_ids: tuple[str, ...] = (),
    fallback_mode: str | None = None,
    coverage_confidence: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH",
) -> QueryPlan:
    """规则分析为任意失败 Planner 提供唯一安全原子。"""
    semantics = analysis.semantics
    constraints: list[QueryConstraint] = []
    for item in semantics.constraints:
        kind = item.kind.value
        mapped = {
            "NUMBER": AtomConstraintKind.NUMBER,
            "NEGATION": AtomConstraintKind.NEGATION,
            "DATE_VERSION": (
                AtomConstraintKind.VERSION
                if item.raw_text.casefold().startswith("v")
                else AtomConstraintKind.DATE_TIME
            ),
        }.get(kind)
        if mapped is not None:
            constraints.append(
                QueryConstraint(kind=mapped, value=item.raw_text)
            )
    atom = QueryAtom(
        atom_id="A1",
        target=semantics.target or analysis.normalized_query[:160],
        relation=semantics.relation or "事实关系",
        answer_shape=_ANSWER_SHAPES.get(
            semantics.answer_type, AtomAnswerShape.FACT
        ),
        source_qualifier=semantics.source_qualifier,
        constraints=tuple(constraints[:12]),
        original_fragment=(
            analysis.resolved_query or analysis.normalized_query
        )[:320],
    )
    return make_query_plan(
        standalone_query=(
            resolved_root_query
            or analysis.resolved_query
            or analysis.normalized_query
        ),
        original_query=original_query or analysis.original_query,
        context_resolution_mode=context_resolution_mode,
        context_digest=context_digest or analysis.conversation_fingerprint,
        referenced_span_ids=referenced_span_ids,
        fallback_mode=fallback_mode,
        coverage_confidence=coverage_confidence,
        intent="FACT",
        effort=effort,
        atoms=(atom,),
        reason_code=reason_code,
        planner_called=planner_called,
    )


def make_query_plan(  # noqa: PLR0913
    *,
    standalone_query: str,
    original_query: str | None = None,
    context_resolution_mode: str = "ORIGINAL",
    context_digest: str | None = None,
    referenced_span_ids: tuple[str, ...] = (),
    fallback_mode: str | None = None,
    coverage_confidence: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH",
    intent: str,
    effort: ReasoningEffortValue,
    atoms: tuple[QueryAtom, ...],
    reason_code: str,
    planner_called: bool,
    needs_clarification: bool = False,
    clarification_question: str | None = None,
    route_hints: tuple[str, ...] = (),
) -> QueryPlan:
    """稳定去重并重新分配有序 Atom ID。"""
    unique: dict[str, QueryAtom] = {}
    for atom in atoms:
        unique.setdefault(atom.dedup_key, atom)
    if not unique or len(unique) > _MAX_ATOMS:
        raise ValueError("QueryPlan 必须包含 1 至 4 个唯一原子。")
    numbered = tuple(
        atom.model_copy(update={"atom_id": f"A{index}"})
        for index, atom in enumerate(unique.values(), 1)
    )
    original = original_query or standalone_query
    digest = context_digest or canonical_sha256(())
    identity = canonical_sha256(
        {
            "schema": QUERY_PLAN_SCHEMA_REVISION,
            "original_query_hash": canonical_sha256(original),
            "resolved_root_query_hash": canonical_sha256(standalone_query),
            "context_digest": digest,
            "intent": intent,
            "effort": effort,
            "atoms": [atom.model_dump(mode="json") for atom in numbered],
        }
    )
    return QueryPlan(
        plan_id=identity,
        standalone_query=standalone_query,
        original_query=original,
        resolved_root_query=standalone_query,
        context_resolution_mode=context_resolution_mode,
        context_digest=digest,
        referenced_span_ids=referenced_span_ids,
        intent=intent,
        effort=effort,
        atoms=numbered,
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
        route_hints=route_hints,
        planner_reason_code=reason_code,
        planner_called=planner_called,
        fallback_mode=fallback_mode,
        coverage_confidence=coverage_confidence,
    )
