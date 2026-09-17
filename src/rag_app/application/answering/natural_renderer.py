"""按已校验事实和逐原子状态组织回答，不改写事实或原文。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupportMatrix,
    QueryPlan,
)
from rag_app.core.models.retrieval import AnswerClaim, EvidenceItem


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


def render_natural_answer(
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    claims: tuple[ValidatedNaturalClaim, ...],
    evidence: tuple[EvidenceItem, ...],
    *,
    missing_atom_ids: frozenset[str],
) -> str | None:
    """以来源顺序发布一次完整答案，缺项只作有限回答。"""
    by_id = {item.support_id: item for item in evidence}
    lines: list[str] = []
    represented_claim_ids: set[str] = set()
    for atom in plan.atoms:
        atom_claims = tuple(
            item
            for item in claims
            if atom.atom_id in item.atom_ids
            and item.claim_id not in represented_claim_ids
        )
        represented_claim_ids.update(item.claim_id for item in atom_claims)
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
    if missing_atom_ids:
        missing = "；".join(
            (atom.original_fragment or f"{atom.target}{atom.relation}").strip()
            for atom in plan.atoms
            if atom.atom_id in missing_atom_ids
        )
        if claims:
            lines.insert(0, "当前资料能够确认的是：")
            lines.append(f"但现有资料没有明确说明：{missing}。")
        elif contradictions:
            lines.append(f"现有资料没有明确说明：{missing}。")
    return "\n".join(lines)


__all__ = ["ValidatedNaturalClaim", "render_natural_answer"]
