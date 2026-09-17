"""把事实原子锁定到 canonical 结构组，不借用相邻组的成员。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.core.models import EvidenceGroupKind, RetrievalPolicy
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
    reason_codes: tuple[str, ...]


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
    "AtomGroupAlignment",
    "align_atom_to_groups",
]
