"""同步 Generator 端口。"""

from __future__ import annotations

from typing import Protocol, Self
from uuid import uuid4

from pydantic import Field, model_validator

from rag_app.core.capabilities import ComponentCapabilities, ComponentDescriptor
from rag_app.core.models import (
    AnswerDraft,
    AtomFactBinding,
    EvidenceItem,
    PhysicalTableFact,
    ProviderHealth,
    QuerySemantics,
)
from rag_app.core.models.common import FrozenModel, JsonObject
from rag_app.core.models.evidence_group import EvidenceGroup
from rag_app.core.models.generation_packet import (
    EvidenceReadUnit,
    stable_support_key,
)
from rag_app.core.models.query_plan import AtomSupportMatrix, QueryPlan


class GenerationRequest(FrozenModel):
    """格式中立且有明确证据集的生成请求。"""

    query: str = Field(min_length=1, repr=False)
    evidence: tuple[EvidenceItem, ...]
    citation_protocol: str = Field(min_length=1)
    repair_reason: str | None = Field(default=None, max_length=200)
    typed_semantics: QuerySemantics | None = None
    answer_support_set: tuple[EvidenceItem, ...] = ()
    model_evidence_candidates: tuple[EvidenceItem, ...] = ()
    query_plan: QueryPlan | None = None
    atom_support_matrix: AtomSupportMatrix | None = None
    per_atom_candidate_support_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    execution_atom_ids: tuple[str, ...] = ()
    repair_atom_ids: tuple[str, ...] = ()
    accepted_claim_ids: tuple[str, ...] = ()
    request_id: str = Field(default_factory=lambda: uuid4().hex, exclude=True)
    attempt_id: str = Field(default_factory=lambda: uuid4().hex, exclude=True)
    repair_raw_failures: tuple[tuple[str, str], ...] = Field(
        default=(), exclude=True
    )
    repair_allowed_support_keys: tuple[tuple[str, tuple[str, ...]], ...] = (
        Field(default=(), exclude=True)
    )
    trusted_source_groups: tuple[EvidenceGroup, ...] = Field(
        default=(), exclude=True, repr=False
    )
    per_atom_source_certificates: tuple[tuple[str, str, JsonObject], ...] = (
        Field(default=(), exclude=True, repr=False)
    )
    priority_source_units: tuple[tuple[str, tuple[str, ...]], ...] = Field(
        default=(), exclude=True, repr=False
    )
    physical_table_facts: tuple[PhysicalTableFact, ...] = Field(
        default=(), exclude=True, repr=False
    )
    atom_fact_bindings: tuple[AtomFactBinding, ...] = Field(
        default=(), exclude=True, repr=False
    )
    evidence_read_units: tuple[EvidenceReadUnit, ...] = Field(
        default=(), exclude=True, repr=False
    )

    @model_validator(mode="after")
    def _validate_evidence_sets(self) -> Self:  # noqa: PLR0912, PLR0915
        """保证直接支持集和模型候选均来自有界 evidence 包。"""
        evidence_ids = {item.support_id for item in self.evidence}
        for name, items in (
            ("answer_support_set", self.answer_support_set),
            ("model_evidence_candidates", self.model_evidence_candidates),
        ):
            ids = [item.support_id for item in items]
            if len(ids) != len(set(ids)) or not set(ids) <= evidence_ids:
                raise ValueError(f"{name} 必须是 evidence 的无重复子集。")
        if (self.query_plan is None) != (self.atom_support_matrix is None):
            raise ValueError("类型化生成必须同时提供计划和支持矩阵。")
        if self.query_plan is not None:
            atom_ids = {atom.atom_id for atom in self.query_plan.atoms}
            if self.atom_support_matrix is None:
                raise ValueError("类型化生成缺少支持矩阵。")
            support_ids = {
                support.atom_id for support in self.atom_support_matrix.atoms
            }
            if support_ids != atom_ids:
                raise ValueError("支持矩阵必须与计划 Atom 一一对应。")
            if not set(self.repair_atom_ids) <= atom_ids:
                raise ValueError("局部修复只允许计划中的 Atom。")
            if (
                len(self.execution_atom_ids)
                != len(set(self.execution_atom_ids))
                or not set(self.execution_atom_ids) <= atom_ids
            ):
                raise ValueError("执行范围只允许计划中的无重复 Atom。")
            if self.repair_atom_ids and self.execution_atom_ids:
                raise ValueError("初次执行范围与局部修复范围不能同时存在。")
            linked_atoms = [
                atom_id for atom_id, _ in self.per_atom_candidate_support_ids
            ]
            if (
                len(linked_atoms) != len(set(linked_atoms))
                or not set(linked_atoms) <= atom_ids
            ):
                raise ValueError("逐原子候选必须绑定唯一计划 Atom。")
            if any(
                len(ids) != len(set(ids)) or not set(ids) <= evidence_ids
                for _, ids in self.per_atom_candidate_support_ids
            ):
                raise ValueError("逐原子候选必须来自本次无重复 Evidence。")
        elif (
            self.repair_atom_ids
            or self.execution_atom_ids
            or self.accepted_claim_ids
            or self.per_atom_candidate_support_ids
            or self.priority_source_units
        ):
            raise ValueError("无计划请求不能执行逐原子修复。")
        owners = [owner for owner, _ in self.priority_source_units]
        allowed_owners = {"ROOT"} | (
            {atom.atom_id for atom in self.query_plan.atoms}
            if self.query_plan is not None
            else set()
        )
        source_keys = {stable_support_key(item) for item in self.evidence}
        if len(owners) != len(set(owners)) or not set(owners) <= allowed_owners:
            raise ValueError("优先阅读单元每个 Root 或 Atom 最多一个。")
        if any(
            not keys
            or len(keys) != len(set(keys))
            or not set(keys) <= source_keys
            for _, keys in self.priority_source_units
        ):
            raise ValueError("优先阅读单元必须完整引用本次真实来源身份。")
        fact_ids = [fact.fact_id for fact in self.physical_table_facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("物理表格事实 ID 不允许重复。")
        if any(
            not set(fact.all_support_ids) <= evidence_ids
            for fact in self.physical_table_facts
        ):
            raise ValueError("物理表格事实必须完整来自本次 Evidence。")
        binding_keys = [
            (binding.atom_id, binding.fact_id)
            for binding in self.atom_fact_bindings
        ]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("Atom 与物理事实绑定不允许重复。")
        if self.atom_fact_bindings and self.query_plan is None:
            raise ValueError("无计划请求不能绑定物理表格事实。")
        if self.query_plan is not None:
            fact_id_set = set(fact_ids)
            atom_id_set = {atom.atom_id for atom in self.query_plan.atoms}
            if any(
                binding.fact_id not in fact_id_set
                or binding.atom_id not in atom_id_set
                for binding in self.atom_fact_bindings
            ):
                raise ValueError("Atom 只能绑定本次请求内的物理表格事实。")
        unit_ids = [unit.unit_id for unit in self.evidence_read_units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("阅读单元短编号不允许重复。")
        if any(
            not set(unit.support_ids) <= evidence_ids
            for unit in self.evidence_read_units
        ):
            raise ValueError("阅读单元必须完整来自本次 Evidence。")
        if any(
            unit.fact_id is not None and unit.fact_id not in set(fact_ids)
            for unit in self.evidence_read_units
        ):
            raise ValueError("表格阅读单元必须绑定本次物理事实。")
        return self


class GeneratorPort(Protocol):
    """同步生成端口；网络和 secret 由 adapter/策略边界处理。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Generator 身份。

        Args:
            无参数；读取当前 Generator。

        Returns:
            可审计组件描述符。

        """
        ...

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回生成能力。

        Args:
            无参数；读取当前 Generator。

        Returns:
            组合阶段能力声明。

        """
        ...

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """从显式证据生成回答草稿。

        Args:
            request: 查询、证据与引用协议。

        Returns:
            尚未发布的回答草稿。

        """
        ...

    def health(self, *, network: bool = False) -> ProviderHealth:
        """读取健康；默认禁止网络探测。

        Args:
            network: 是否明确允许实际网络探测。

        Returns:
            不含正文的健康状态。

        """
        ...
