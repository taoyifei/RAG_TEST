"""把模型短引用绑定到当前发送包内的真实来源与物理结构。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from rag_app.core.models.generation_packet import (
    EvidenceReadUnit,
    PreparedGenerationPacket,
    stable_read_unit_digest,
    stable_support_key,
)
from rag_app.core.models.retrieval import (
    AnswerClaim,
    AtomFactBinding,
    ClaimSupport,
    EvidenceItem,
    GroundedWireClaim,
    NaturalClaim,
    PhysicalTableFact,
)


class EvidenceBindingError(ValueError):
    """不含业务正文的确定性来源绑定错误。"""

    def __init__(self, failure_code: str, json_path: str) -> None:
        self.failure_code = failure_code
        self.json_path = json_path
        super().__init__(failure_code)


@dataclass(frozen=True, slots=True)
class BoundClaim:
    """服务端恢复完整 provenance 后、尚未取得语义发布许可的事实。"""

    claim_id: str
    atom_id: str
    text: str
    selected_unit_ids: tuple[str, ...]
    provenance: tuple[ClaimSupport, ...]
    physical_fact_ids: tuple[str, ...]
    binding_status: Literal["bound"] = "bound"
    semantic_status: Literal[
        "pending", "supported", "contradicted", "unknown"
    ] = "pending"

    @property
    def answer_claim(self) -> AnswerClaim:
        """投影为现有渲染与引用入口消费的事实结构。"""
        return AnswerClaim(text=self.text, supports=self.provenance)

    @property
    def natural_claim(self) -> NaturalClaim:
        """投影为既有逐 Atom 内部合同，不恢复模型 Quote 协议。"""
        return NaturalClaim(
            atom_id=self.atom_id,
            text=self.text,
            supports=self.provenance,
        )

    def with_semantic_status(
        self,
        status: Literal["supported", "contradicted", "unknown"],
    ) -> BoundClaim:
        """返回带批量语义判定的新对象。"""
        return replace(self, semantic_status=status)


def bind_wire_claim(  # noqa: PLR0913
    claim: GroundedWireClaim,
    *,
    claim_id: str,
    read_units: tuple[EvidenceReadUnit, ...],
    evidence: tuple[EvidenceItem, ...],
    allowed_unit_ids: frozenset[str],
    physical_table_facts: tuple[PhysicalTableFact, ...] = (),
    atom_fact_bindings: tuple[AtomFactBinding, ...] = (),
) -> BoundClaim:
    """只证明短引用的身份与结构，不把可绑定误作语义支持。

    Args:
        claim: 已通过 Wire 字段校验的模型事实。
        claim_id: 服务端为本次事实分配的稳定短编号。
        read_units: 当前发送尝试实际包含的阅读单元。
        evidence: 当前尝试真实来源。
        allowed_unit_ids: 当前 Atom 获准引用的阅读单元。
        physical_table_facts: 本次完整物理表格事实。
        atom_fact_bindings: 当前 Atom 与物理事实的许可关系。

    Returns:
        持有全部真实来源跨度的内部绑定事实。

    Raises:
        EvidenceBindingError: 引用、坐标、来源或活动发送身份不闭合。

    """
    if not set(claim.refs) <= allowed_unit_ids:
        raise EvidenceBindingError(
            "REF_OUTSIDE_ATOM", f"claim[{claim_id}].refs"
        )
    unit_registry = {unit.unit_id: unit for unit in read_units}
    evidence_registry = {item.support_id: item for item in evidence}
    fact_registry = {fact.fact_id: fact for fact in physical_table_facts}
    permitted_facts = {
        binding.fact_id
        for binding in atom_fact_bindings
        if binding.atom_id == claim.atom_id
    }
    selected_units: list[EvidenceReadUnit] = []
    fact_ids: list[str] = []
    support_ids: list[str] = []
    for ref in claim.refs:
        unit = unit_registry.get(ref)
        if unit is None:
            raise EvidenceBindingError(
                "READ_UNIT_NOT_SENT", f"claim[{claim_id}].refs"
            )
        if not unit.source_complete:
            raise EvidenceBindingError(
                "READ_UNIT_SOURCE_INCOMPLETE", f"read_units[{ref}]"
            )
        if unit.fact_id is not None:
            fact = fact_registry.get(unit.fact_id)
            if (
                fact is None
                or unit.fact_id not in permitted_facts
                or tuple(unit.support_ids) != tuple(fact.all_support_ids)
            ):
                raise EvidenceBindingError(
                    "TABLE_FACT_BINDING_INVALID", f"read_units[{ref}]"
                )
            fact_ids.append(unit.fact_id)
        selected_units.append(unit)
        support_ids.extend(unit.support_ids)
    ordered_support_ids = tuple(dict.fromkeys(support_ids))
    try:
        source_items = tuple(
            evidence_registry[support_id] for support_id in ordered_support_ids
        )
    except KeyError:
        raise EvidenceBindingError(
            "SOURCE_NOT_IN_REQUEST", f"claim[{claim_id}].refs"
        ) from None
    if any(
        not item.publishable
        or not item.source_spans
        or any(not span.is_citable for span in item.source_spans)
        for item in source_items
    ):
        raise EvidenceBindingError(
            "SOURCE_NOT_CITABLE", f"claim[{claim_id}].refs"
        )
    return BoundClaim(
        claim_id=claim_id,
        atom_id=claim.atom_id,
        text=claim.text,
        selected_unit_ids=tuple(unit.unit_id for unit in selected_units),
        provenance=tuple(
            ClaimSupport(
                support_id=item.support_id,
                quote=item.citation_text,
            )
            for item in source_items
        ),
        physical_fact_ids=tuple(dict.fromkeys(fact_ids)),
    )


def revalidate_bound_claim(  # noqa: PLR0913
    claim: BoundClaim,
    *,
    packet: PreparedGenerationPacket,
    read_units: tuple[EvidenceReadUnit, ...],
    evidence: tuple[EvidenceItem, ...],
    allowed_unit_ids: frozenset[str],
    physical_table_facts: tuple[PhysicalTableFact, ...] = (),
    atom_fact_bindings: tuple[AtomFactBinding, ...] = (),
) -> BoundClaim:
    """语义 supported 后只重验发送身份与确定性来源硬边界。"""
    packet_units = dict(packet.read_unit_bindings)
    packet_unit_digests = dict(packet.read_unit_sha256s)
    by_unit = {unit.unit_id: unit for unit in read_units}
    evidence_registry = {item.support_id: item for item in evidence}
    if any(unit_id not in packet_units for unit_id in claim.selected_unit_ids):
        raise EvidenceBindingError(
            "READ_UNIT_NOT_IN_SENT_PACKET", f"claim[{claim.claim_id}].refs"
        )
    for unit_id in claim.selected_unit_ids:
        unit = by_unit.get(unit_id)
        if unit is None:
            raise EvidenceBindingError(
                "READ_UNIT_NOT_IN_REQUEST", f"claim[{claim.claim_id}].refs"
            )
        if any(
            support_id not in evidence_registry
            for support_id in unit.support_ids
        ):
            raise EvidenceBindingError(
                "SOURCE_NOT_IN_REQUEST", f"read_units[{unit_id}]"
            )
        expected = tuple(
            stable_support_key(evidence_registry[support_id])
            for support_id in unit.support_ids
        )
        if tuple(packet_units[unit_id]) != expected:
            raise EvidenceBindingError(
                "READ_UNIT_IDENTITY_CHANGED", f"read_units[{unit_id}]"
            )
        if packet_unit_digests and packet_unit_digests.get(
            unit_id
        ) != stable_read_unit_digest(unit):
            raise EvidenceBindingError(
                "READ_UNIT_IDENTITY_CHANGED", f"read_units[{unit_id}]"
            )
    rebound = bind_wire_claim(
        GroundedWireClaim(
            atom_id=claim.atom_id,
            text=claim.text,
            refs=claim.selected_unit_ids,
        ),
        claim_id=claim.claim_id,
        read_units=read_units,
        evidence=evidence,
        allowed_unit_ids=allowed_unit_ids,
        physical_table_facts=physical_table_facts,
        atom_fact_bindings=atom_fact_bindings,
    )
    if claim.semantic_status == "pending":
        raise EvidenceBindingError(
            "SEMANTIC_STATUS_NOT_FINAL", f"claim[{claim.claim_id}]"
        )
    return rebound.with_semantic_status(claim.semantic_status)
