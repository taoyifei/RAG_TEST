"""复合查询的有界事实原子与逐原子证据状态。"""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query import QueryAnalysis, RequestedAnswerType

QUERY_PLAN_SCHEMA_REVISION = "wb08r-query-plan-v2"
QUERY_UNIT_FUSION_REVISION = "wb08r-root-atom-fusion-v1"
EVIDENCE_GROUP_SCHEMA_REVISION = "wb08r-evidence-group-v1"
GROUNDED_CLAIM_SCHEMA_REVISION = "wb08r-grounded-claim-v3"
NATURAL_RENDERER_REVISION = "wb08r-natural-renderer-v2"
CORRECTIVE_RETRIEVAL_REVISION = "wb08r-closed-correction-v1"
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


class QueryAtom(FrozenModel):
    """一项可以独立检索和验证的用户要求。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    target: str = Field(min_length=1, max_length=160)
    relation: str = Field(min_length=1, max_length=160)
    answer_shape: AtomAnswerShape
    source_qualifier: str | None = Field(default=None, max_length=160)
    constraints: tuple[QueryConstraint, ...] = Field(default=(), max_length=12)
    original_fragment: str | None = Field(default=None, max_length=320)

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
    intent: str = Field(min_length=1, max_length=40)
    effort: ReasoningEffortValue
    atoms: tuple[QueryAtom, ...] = Field(min_length=1, max_length=4)
    needs_clarification: StrictBool = False
    clarification_question: str | None = Field(default=None, max_length=200)
    route_hints: tuple[str, ...] = Field(default=(), max_length=4)
    planner_reason_code: str = Field(min_length=1, max_length=120)
    planner_called: StrictBool = False

    @model_validator(mode="after")
    def _unique_atoms(self) -> Self:
        if tuple(atom.atom_id for atom in self.atoms) != tuple(
            f"A{index}" for index in range(1, len(self.atoms) + 1)
        ):
            raise ValueError("Atom ID 必须连续且唯一。")
        if len({atom.dedup_key for atom in self.atoms}) != len(self.atoms):
            raise ValueError("QueryPlan 不接受重复原子。")
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


def fallback_query_plan(
    analysis: QueryAnalysis,
    *,
    effort: ReasoningEffortValue,
    reason_code: str,
    planner_called: bool,
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
        standalone_query=(analysis.resolved_query or analysis.normalized_query),
        intent="FACT",
        effort=effort,
        atoms=(atom,),
        reason_code=reason_code,
        planner_called=planner_called,
    )


def make_query_plan(  # noqa: PLR0913
    *,
    standalone_query: str,
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
    identity = canonical_sha256(
        {
            "schema": QUERY_PLAN_SCHEMA_REVISION,
            "query": standalone_query,
            "intent": intent,
            "effort": effort,
            "atoms": [atom.model_dump(mode="json") for atom in numbered],
        }
    )
    return QueryPlan(
        plan_id=identity,
        standalone_query=standalone_query,
        intent=intent,
        effort=effort,
        atoms=numbered,
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
        route_hints=route_hints,
        planner_reason_code=reason_code,
        planner_called=planner_called,
    )
