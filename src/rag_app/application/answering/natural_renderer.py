"""按已校验事实和逐原子状态组织回答，不改写事实或原文。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupportMatrix,
    QueryPlan,
)
from rag_app.core.models.retrieval import AnswerClaim, EvidenceItem

_MIN_DESCRIPTIVE_FRAGMENT_CHARS = 5


class MissingAtomReason(StrEnum):
    """区分资料缺失、结构未闭合和本次生成未覆盖。"""

    SOURCE_MISSING = "SOURCE_MISSING"
    EVIDENCE_NOT_DIRECT = "EVIDENCE_NOT_DIRECT"
    STRUCTURE_INCOMPLETE = "STRUCTURE_INCOMPLETE"
    GENERATION_INCOMPLETE = "GENERATION_INCOMPLETE"
    CLAIM_REJECTED = "CLAIM_REJECTED"
    CONTEXT_UNRESOLVED = "CONTEXT_UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ValidatedNaturalClaim:
    """已经通过服务端引用与事实核验的一条自然事实。"""

    claim_id: str
    atom_ids: tuple[str, ...]
    claim: AnswerClaim


def _cited_text(claim: AnswerClaim) -> str:
    """只在事实句后追加实际引用，不改变已校验正文。"""
    citations = " ".join(
        f"[{support.support_id}]" for support in claim.supports
    )
    return f"{claim.text.strip()} {citations}"


def _missing_description(shape: AtomAnswerShape, original: str) -> str:
    """结构问题只说明未闭合的范围，避免重复整个用户问句。"""
    if shape is AtomAnswerShape.ENUMERATION:
        return "该主题的完整列表"
    if shape is AtomAnswerShape.DUTIES:
        return "该角色的全部职责"
    if shape is AtomAnswerShape.PROCEDURE:
        return "完整的步骤和顺序"
    return (
        original
        if len(original) >= _MIN_DESCRIPTIVE_FRAGMENT_CHARS
        else "该子问题的明确规定"
    )


def _conflict_lines(
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[str, ...]:
    """只展示可引用的冲突来源原文，不替来源做结论裁决。"""
    by_id = {item.support_id: item for item in evidence}
    lines: list[str] = []
    for atom in plan.atoms:
        support = matrix.for_atom(atom.atom_id)
        if support.status is not AtomStatus.CONTRADICTORY:
            continue
        for support_id in support.supporting_support_ids:
            item = by_id.get(support_id)
            if (
                item is None
                or not item.publishable
                or not item.source_spans
                or any(not span.is_citable for span in item.source_spans)
            ):
                continue
            lines.append(
                f"- {item.source_label}：{item.citation_text} [{support_id}]"
            )
    return tuple(lines)


def _source_order(
    item: ValidatedNaturalClaim,
    by_id: dict[str, EvidenceItem],
) -> int:
    """按真实来源跨度定位流程或列表成员。"""
    return min(
        (
            span.source_anchor.ordinal
            for support in item.claim.supports
            for span in by_id[support.support_id].source_spans
            if span.source_anchor is not None
        ),
        default=2**31 - 1,
    )


def render_natural_answer(  # noqa: PLR0912
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    claims: tuple[ValidatedNaturalClaim, ...],
    evidence: tuple[EvidenceItem, ...],
    *,
    missing_atoms: Mapping[str, MissingAtomReason],
) -> str | None:
    """以来源顺序发布一次答案，并按缺项根因选择安全文案。"""
    by_id = {item.support_id: item for item in evidence}
    lines: list[str] = []
    represented_claim_ids: set[str] = set()
    represented_facts: set[tuple[str, tuple[str, ...]]] = set()
    for atom in plan.atoms:
        atom_claims: list[ValidatedNaturalClaim] = []
        for item in claims:
            if (
                atom.atom_id not in item.atom_ids
                or item.claim_id in represented_claim_ids
            ):
                continue
            fact_key = (
                " ".join(item.claim.text.split()),
                tuple(support.support_id for support in item.claim.supports),
            )
            represented_claim_ids.add(item.claim_id)
            if fact_key in represented_facts:
                continue
            represented_facts.add(fact_key)
            atom_claims.append(item)
        shape = atom.answer_shape
        if shape in {AtomAnswerShape.ENUMERATION, AtomAnswerShape.DUTIES}:
            lines.extend(
                f"- {_cited_text(item.claim)}"
                for item in sorted(
                    atom_claims, key=lambda item: _source_order(item, by_id)
                )
            )
        elif shape is AtomAnswerShape.PROCEDURE:
            lines.extend(
                f"{index}. {_cited_text(item.claim)}"
                for index, item in enumerate(
                    sorted(
                        atom_claims, key=lambda item: _source_order(item, by_id)
                    ),
                    1,
                )
            )
        else:
            lines.extend(_cited_text(item.claim) for item in atom_claims)

    contradictions = _conflict_lines(plan, matrix, evidence)
    if contradictions:
        lines.extend(("当前资料存在不一致：", *contradictions))
    if not lines:
        return None
    if missing_atoms:
        if claims:
            lines.insert(0, "当前资料能够确认的是：")
        for reason in MissingAtomReason:
            descriptions = tuple(
                dict.fromkeys(
                    _missing_description(
                        atom.answer_shape,
                        (
                            atom.original_fragment
                            or f"{atom.target}{atom.relation}"
                        ).strip(),
                    )
                    for atom in plan.atoms
                    if missing_atoms.get(atom.atom_id) is reason
                )
            )
            if not descriptions:
                continue
            subject = "；".join(descriptions)
            if reason is MissingAtomReason.SOURCE_MISSING:
                lines.append(f"现有资料中没有找到该部分的明确规定：{subject}。")
            elif reason is MissingAtomReason.EVIDENCE_NOT_DIRECT:
                lines.append(
                    f"已检索到相关资料，但尚未确认{subject}的直接依据。"
                )
            elif reason is MissingAtomReason.STRUCTURE_INCOMPLETE:
                lines.append(f"当前资料只检索到部分条目，尚无法确认{subject}。")
            elif reason is MissingAtomReason.GENERATION_INCOMPLETE:
                lines.append("已检索到相关资料，但本次未能完整组织全部内容。")
            elif reason is MissingAtomReason.CLAIM_REJECTED:
                lines.append(
                    f"检索到了相关资料，但其中部分表述未能通过引用核验：{subject}。"
                )
            else:
                lines.append("当前问题的指代对象尚不明确。")
    return "\n".join(lines)


__all__ = [
    "MissingAtomReason",
    "ValidatedNaturalClaim",
    "render_natural_answer",
]
