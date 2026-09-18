"""把事实原子锁定到 canonical 结构组，不借用相邻组的成员。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.core.models import EvidenceGroupKind, EvidenceItem, RetrievalPolicy
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    QueryAtom,
)

_TABLE_ROW_LABEL = re.compile(r"r(?P<row>\d+):c0$")
_RELATION_ANCHOR_THRESHOLD = 0.35
_MIN_BIGRAM_CHARS = 2
_MIN_FUZZY_TARGET_CHARS = 5


class AlignmentQualification(StrEnum):
    """结构组对一个 Atom 的候选归属强度。"""

    STRONG = "STRONG"
    WEAK = "WEAK"
    REJECTED = "REJECTED"


class EvidenceProvenance(StrEnum):
    """候选最早进入本次检索的来源。"""

    ROOT = "ROOT"
    ATOM = "ATOM"
    CORRECTIVE = "CORRECTIVE"


class EvidenceSupportMode(StrEnum):
    """可引用证据的结构边界。"""

    ALIGNED_COMPLETE_GROUP = "ALIGNED_COMPLETE_GROUP"
    ALIGNED_PARTIAL_GROUP = "ALIGNED_PARTIAL_GROUP"
    DIRECT_ATOM_SPAN = "DIRECT_ATOM_SPAN"
    DIRECT_ROOT_SPAN = "DIRECT_ROOT_SPAN"


@dataclass(frozen=True, slots=True)
class AtomGroupAlignment:
    """保留结构身份、字面锚点、初召回来源和拒绝原因。"""

    atom_id: str
    group_id: str
    document_version_id: str
    provenance_hit: bool
    target_anchor_score: float
    relation_compatible: bool
    constraint_checks: tuple[tuple[str, bool], ...]
    qualification: AlignmentQualification
    publishable: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AtomEvidenceQualification:
    """区分相关候选和可发布支持，不用相似度代替事实证明。"""

    atom_id: str
    chunk_id: str
    group_id: str | None
    provenance: EvidenceProvenance
    retrieval_relevant: bool
    target_owned: bool
    relation_supported: bool
    constraints_supported: bool
    source_scope_supported: bool
    structure_safe: bool
    publishable: bool
    support_mode: EvidenceSupportMode | None
    reason_codes: tuple[str, ...]


_SCALAR_SHAPES = frozenset(
    {
        AtomAnswerShape.FACT,
        AtomAnswerShape.DEFINITION,
        AtomAnswerShape.DURATION,
        AtomAnswerShape.COUNT,
        AtomAnswerShape.RESPONSIBLE_PARTY,
    }
)


def qualify_atom_evidence(  # noqa: PLR0913
    atom: QueryAtom,
    item: EvidenceItem,
    links: tuple[AtomCandidateLink, ...],
    *,
    alignment: AtomGroupAlignment | None,
    resolved_root_query: str,
    context_resolution_confidence: str = "HIGH",
    single_atom_direct: bool = False,
) -> AtomEvidenceQualification:
    """对一个真实 SourceSpan 建立逐原子发布资格。

    ROOT 只能在可信上下文和完整目标关系均被来源直接证明时补救。
    结构问题只使用其已锁定组，不能借单个邻居 Chunk 宣称完整。
    """
    metadata = dict(item.metadata)
    group_id = metadata.get("evidence_group_id")
    group_id = group_id if isinstance(group_id, str) else None
    direct_support = metadata.get("answer_support")
    relation_supported = (
        isinstance(direct_support, dict)
        and direct_support.get("status") == "SUPPORTED"
    )
    own_link = any(
        link.atom_id == atom.atom_id and link.chunk_id == item.chunk_id
        for link in links
    ) or (single_atom_direct and not links and relation_supported)
    root_link = any(
        link.atom_id is None and link.chunk_id == item.chunk_id
        for link in links
    )
    provenance = (
        EvidenceProvenance.CORRECTIVE
        if item.selection_reason == "closed_correction"
        else EvidenceProvenance.ATOM
        if own_link
        else EvidenceProvenance.ROOT
    )
    cited = bool(item.source_spans) and all(
        span.is_citable for span in item.source_spans
    )
    source_text = " ".join(
        (
            item.display_name or "",
            str(metadata.get("document_title", "")),
        )
    )
    source_scope_supported = (
        not atom.source_qualifier
        or _normalized(atom.source_qualifier) in _normalized(source_text)
    )
    constraints_supported = all(
        _constraint_supported(constraint.kind.value, constraint.value, item)
        for constraint in atom.constraints
    )
    direct_target = bool(
        _normalized(atom.target)
        and _normalized(atom.target) in _normalized(item.citation_text)
    )
    group_owned = (
        alignment is not None
        and alignment.qualification is not AlignmentQualification.REJECTED
        and alignment.group_id == group_id
    )
    target_owned = direct_target or group_owned
    root_trusted = (
        context_resolution_confidence in {"HIGH", "MEDIUM"}
        and bool(_normalized(atom.target))
        and bool(_normalized(atom.relation))
        and _normalized(atom.target) in _normalized(resolved_root_query)
        and _normalized(atom.relation) in _normalized(resolved_root_query)
    )
    if provenance is EvidenceProvenance.ROOT and not root_link:
        root_trusted = False
    structural = atom.answer_shape not in _SCALAR_SHAPES
    structure_safe = (
        group_owned
        if structural
        else alignment is None
        or group_owned
        or group_id is None
    )
    retrieval_relevant = cited and (own_link or root_link or group_owned)
    direct_allowed = (
        atom.answer_shape in _SCALAR_SHAPES
        and direct_target
        and (own_link or root_trusted)
        and structure_safe
    )
    group_allowed = group_owned and structure_safe
    support_mode = (
        EvidenceSupportMode.ALIGNED_COMPLETE_GROUP
        if group_allowed and metadata.get("group_complete") is True
        else EvidenceSupportMode.ALIGNED_PARTIAL_GROUP
        if group_allowed
        else EvidenceSupportMode.DIRECT_ATOM_SPAN
        if direct_allowed and own_link
        else EvidenceSupportMode.DIRECT_ROOT_SPAN
        if direct_allowed and root_trusted
        else None
    )
    publishable = (
        item.publishable
        and cited
        and target_owned
        and relation_supported
        and constraints_supported
        and source_scope_supported
        and structure_safe
        and support_mode is not None
        and (provenance is not EvidenceProvenance.ROOT or root_trusted)
    )
    reasons: list[str] = []
    for valid, code in (
        (retrieval_relevant, "NOT_RETRIEVAL_RELEVANT"),
        (target_owned, "ATOM_TARGET_NOT_OWNED"),
        (relation_supported, "ATOM_RELATION_UNSUPPORTED"),
        (constraints_supported, "ATOM_CONSTRAINT_UNVERIFIED"),
        (source_scope_supported, "SOURCE_QUALIFIER_MISMATCH"),
        (structure_safe, "ATOM_STRUCTURE_UNSAFE"),
        (cited, "SOURCE_SPAN_NOT_CITABLE"),
    ):
        if not valid:
            reasons.append(code)
    if provenance is EvidenceProvenance.ROOT and not root_trusted:
        reasons.append("ROOT_RESCUE_UNTRUSTED")
    if support_mode is None:
        reasons.append("NO_PUBLISHABLE_SUPPORT_MODE")
    return AtomEvidenceQualification(
        atom_id=atom.atom_id,
        chunk_id=item.chunk_id,
        group_id=group_id,
        provenance=provenance,
        retrieval_relevant=retrieval_relevant,
        target_owned=target_owned,
        relation_supported=relation_supported,
        constraints_supported=constraints_supported,
        source_scope_supported=source_scope_supported,
        structure_safe=structure_safe,
        publishable=publishable,
        support_mode=support_mode,
        reason_codes=tuple(reasons),
    )


def _constraint_supported(kind: str, value: str, item: EvidenceItem) -> bool:
    """硬限制逐字保真；来源限制只检查文档身份。"""
    metadata = dict(item.metadata)
    checked = (
        " ".join(
            (
                item.display_name or "",
                str(metadata.get("document_title", "")),
            )
        )
        if kind == "SOURCE"
        else item.citation_text
    )
    expected = "".join(unicodedata.normalize("NFKC", value).casefold().split())
    actual = "".join(unicodedata.normalize("NFKC", checked).casefold().split())
    return bool(expected) and expected in actual


def align_atom_to_groups(
    atom: QueryAtom,
    groups: tuple[GroupCandidate, ...],
    links: tuple[AtomCandidateLink, ...],
    policy: RetrievalPolicy,
) -> tuple[AtomGroupAlignment, ...]:
    """按组的专属导语、行标签或正文锚定目标，保留 ROOT 的强命中。"""
    own_hits = {link.chunk_id for link in links if link.atom_id == atom.atom_id}
    heading_counts = Counter(
        (candidate.group.document_version_id, candidate.group.heading_path)
        for candidate in groups
        if candidate.group.heading_path
    )
    results: list[AtomGroupAlignment] = []
    for candidate in groups:
        group = candidate.group
        member_ids = set(group.member_chunk_ids)
        provenance = bool(member_ids & own_hits)
        reasons: list[str] = []
        valid_members = len(candidate.members) == len(
            group.member_chunk_ids
        ) and all(
            member.hydrated.chunk.chunk_id == expected_id
            and member.hydrated.chunk.version.document_version_id
            == group.document_version_id
            and member.hydrated.chunk.section_id == group.section_id
            for member, expected_id in zip(
                candidate.members, group.member_chunk_ids, strict=True
            )
        )
        if not valid_members:
            reasons.append("GROUP_MEMBER_IDENTITY_MISMATCH")
        compatible = _shape_compatible(
            atom.answer_shape, group.kind, group.complete
        )
        if not compatible:
            reasons.append("GROUP_SHAPE_INCOMPATIBLE")
        heading_is_unique = (
            heading_counts[(group.document_version_id, group.heading_path)]
            == 1
        )
        anchor_texts = _primary_anchors(
            candidate, include_heading=heading_is_unique
        )
        target = _normalized(atom.target)
        score = max(
            (_anchor_score(target, _normalized(text)) for text in anchor_texts),
            default=0.0,
        )
        if score < policy.atom_group_weak_anchor_threshold:
            reasons.append("TARGET_ANCHOR_MISSING")
        source_text = _normalized(group.display_name)
        source_ok = (
            not atom.source_qualifier
            or _normalized(atom.source_qualifier) in source_text
        )
        if not source_ok:
            reasons.append("SOURCE_QUALIFIER_MISMATCH")
        group_text = _normalized(group.group_text_for_model)
        checks = tuple(
            (
                constraint.kind.value,
                _normalized(constraint.value)
                in (
                    source_text
                    if constraint.kind.value == "SOURCE"
                    else group_text
                ),
            )
            for constraint in atom.constraints
        )
        if any(not passed for _kind, passed in checks):
            reasons.append("ATOM_CONSTRAINT_UNVERIFIED")
        relation = _normalized(atom.relation)
        relation_compatible = bool(
            relation
            and _anchor_score(relation, group_text)
            >= _RELATION_ANCHOR_THRESHOLD
        )
        if not valid_members or not compatible or not source_ok:
            qualification = AlignmentQualification.REJECTED
        elif score >= policy.atom_group_strong_anchor_threshold:
            qualification = AlignmentQualification.STRONG
        elif provenance and score >= policy.atom_group_weak_anchor_threshold:
            qualification = AlignmentQualification.WEAK
        else:
            qualification = AlignmentQualification.REJECTED
            if score >= policy.atom_group_weak_anchor_threshold:
                reasons.append("WEAK_ANCHOR_WITHOUT_ATOM_SEED")
        if not relation_compatible:
            reasons.append("ATOM_RELATION_UNVERIFIED")
        publishable = (
            qualification is not AlignmentQualification.REJECTED
            and relation_compatible
            and all(passed for _kind, passed in checks)
        )
        results.append(
            AtomGroupAlignment(
                atom_id=atom.atom_id,
                group_id=group.group_id,
                document_version_id=group.document_version_id,
                provenance_hit=provenance,
                target_anchor_score=score,
                relation_compatible=relation_compatible,
                constraint_checks=checks,
                qualification=qualification,
                publishable=publishable,
                reason_codes=tuple(reasons),
            )
        )
    return tuple(results)


def _shape_compatible(
    shape: AtomAnswerShape, kind: EvidenceGroupKind, complete: bool
) -> bool:
    if kind is EvidenceGroupKind.CATALOG_ENTRY:
        return shape is AtomAnswerShape.CATALOG_REFERENCE
    if shape is AtomAnswerShape.CATALOG_REFERENCE:
        return False
    if shape is AtomAnswerShape.ENUMERATION:
        return kind in {
            EvidenceGroupKind.LIST_GROUP,
            EvidenceGroupKind.TABLE_ROW_GROUP,
        } or (kind is EvidenceGroupKind.SECTION_GROUP and complete)
    if shape is AtomAnswerShape.DUTIES:
        return kind in {
            EvidenceGroupKind.LIST_GROUP,
            EvidenceGroupKind.TABLE_ROW_GROUP,
            EvidenceGroupKind.SECTION_GROUP,
        }
    if shape is AtomAnswerShape.PROCEDURE:
        return kind in {
            EvidenceGroupKind.PROCEDURE_GROUP,
            EvidenceGroupKind.LIST_GROUP,
        }
    return True


def _primary_anchors(
    candidate: GroupCandidate, *, include_heading: bool
) -> tuple[str, ...]:
    group = candidate.group
    heading = group.heading_path[-1:] if include_heading else ()
    if group.kind is EvidenceGroupKind.TABLE_ROW_GROUP:
        row_labels: list[tuple[int, str]] = []
        for source in group.member_source_maps:
            row_labels.extend(
                (int(match["row"]), source.citation_text)
                for coordinate in source.structural_coordinates
                if (match := _TABLE_ROW_LABEL.fullmatch(coordinate))
            )
        if row_labels:
            data_row = max(row for row, _text in row_labels)
            return tuple(text for row, text in row_labels if row == data_row)
        return ()
    if group.kind in {
        EvidenceGroupKind.LIST_GROUP,
        EvidenceGroupKind.PROCEDURE_GROUP,
    }:
        first = group.member_source_maps[:1]
        return (
            *(source.citation_text for source in first),
            *heading,
        )
    return (
        *(source.citation_text for source in group.member_source_maps),
        *heading,
    )


def _normalized(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in folded
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _anchor_score(target: str, anchor: str) -> float:
    if not target or not anchor:
        return 0.0
    if target in anchor:
        return 1.0
    if len(anchor) >= _MIN_BIGRAM_CHARS and anchor in target:
        return min(0.9, len(anchor) / len(target) + 0.1)
    if min(len(target), len(anchor)) < _MIN_BIGRAM_CHARS:
        return 0.0
    left = {target[index : index + 2] for index in range(len(target) - 1)}
    right = {anchor[index : index + 2] for index in range(len(anchor) - 1)}
    dice = 2 * len(left & right) / (len(left) + len(right))
    overlap = (
        SequenceMatcher(None, target, anchor).find_longest_match(
            0, len(target), 0, len(anchor)
        ).size
        / len(target)
        if len(target) >= _MIN_FUZZY_TARGET_CHARS
        else 0.0
    )
    return max(
        dice,
        overlap,
        SequenceMatcher(None, target, anchor).ratio() * 0.8,
    )


__all__ = [
    "AlignmentQualification",
    "AtomEvidenceQualification",
    "AtomGroupAlignment",
    "EvidenceProvenance",
    "EvidenceSupportMode",
    "align_atom_to_groups",
    "qualify_atom_evidence",
]
