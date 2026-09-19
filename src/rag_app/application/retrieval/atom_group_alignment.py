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
_TABLE_CERTIFICATE_SPAN_COUNT = 3
_STRUCTURAL_RELATIONS = {
    AtomAnswerShape.ENUMERATION: re.compile(
        r"包括|包含|分为|分成|列为|组成|如下"
    ),
    AtomAnswerShape.DUTIES: re.compile(r"职责|负责|承担|任务"),
    AtomAnswerShape.PROCEDURE: re.compile(r"流程|步骤|先|再|然后|随后"),
}


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
    structural_relation_proven: bool = False


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


def _certified_table_items(
    item: EvidenceItem, supporting_items: tuple[EvidenceItem, ...]
) -> tuple[EvidenceItem, ...]:
    """只接受同文档、同表且行列交点闭合的三个真实片段。"""
    support = dict(item.metadata).get("answer_support")
    if not isinstance(support, dict) or support.get("support_reason") != (
        "TABLE_INTERSECTION"
    ):
        return ()
    ids = support.get("supporting_span_ids")
    if (
        not isinstance(ids, list)
        or len(ids) != _TABLE_CERTIFICATE_SPAN_COUNT
        or any(not isinstance(node_id, str) or not node_id for node_id in ids)
        or len(set(ids)) != _TABLE_CERTIFICATE_SPAN_COUNT
    ):
        return ()
    by_node: dict[str, tuple[EvidenceItem, tuple[object, int, int]]] = {}
    for proof in supporting_items:
        proof_support = dict(proof.metadata).get("answer_support")
        if (
            proof.document_version_id != item.document_version_id
            or not isinstance(proof_support, dict)
            or proof_support.get("support_reason") != "TABLE_INTERSECTION"
            or proof_support.get("supporting_span_ids") != ids
        ):
            continue
        for span in proof.source_spans:
            path = span.structural_path
            if not span.is_citable or span.source_anchor is None:
                continue
            for index in range(len(path) - 2):
                row = re.fullmatch(r"tr:(\d+)", path[index + 1])
                column = re.fullmatch(r"tc:(\d+)", path[index + 2])
                if (
                    not path[index].startswith("tbl:")
                    or row is None
                    or column is None
                ):
                    continue
                table_identity = (
                    proof.document_version_id,
                    span.source_anchor.part_uri,
                    path[: index + 1],
                )
                if span.node_id is not None and span.node_id in ids:
                    by_node[span.node_id] = (
                        proof,
                        (table_identity, int(row[1]), int(column[1])),
                    )
                break
    if set(ids) != set(by_node):
        return ()
    row_label, header, value = (
        by_node[node] for node in ids if isinstance(node, str)
    )
    label_table, label_row, label_column = row_label[1]
    header_table, header_row, header_column = header[1]
    value_table, value_row, value_column = value[1]
    if (
        label_table != header_table
        or label_table != value_table
        or label_row <= 0
        or label_column != 0
        or header_row != 0
        or header_column <= 0
        or value_row != label_row
        or value_column != header_column
        or item not in (row_label[0], header[0], value[0])
    ):
        return ()
    return row_label[0], header[0], value[0]


def qualify_atom_evidence(  # noqa: PLR0913
    atom: QueryAtom,
    item: EvidenceItem,
    links: tuple[AtomCandidateLink, ...],
    *,
    alignment: AtomGroupAlignment | None,
    resolved_root_query: str,
    context_resolution_confidence: str = "HIGH",
    single_atom_direct: bool = False,
    supporting_items: tuple[EvidenceItem, ...] = (),
) -> AtomEvidenceQualification:
    """对一个真实 SourceSpan 建立逐原子发布资格。

    ROOT 只能在可信上下文和完整目标关系均被来源直接证明时补救。
    结构问题只使用其已锁定组，不能借单个邻居 Chunk 宣称完整。
    """
    metadata = dict(item.metadata)
    group_id = metadata.get("evidence_group_id")
    group_id = group_id if isinstance(group_id, str) else None
    direct_support = metadata.get("answer_support")
    table_items = _certified_table_items(item, supporting_items)
    direct_relation_supported = (
        isinstance(direct_support, dict)
        and direct_support.get("status") == "SUPPORTED"
        and _normalized(str(direct_support.get("query_target") or ""))
        == _normalized(atom.target)
        and _normalized(
            str(direct_support.get("requested_relation_or_attribute") or "")
        )
        == _normalized(atom.relation)
        and (
            direct_support.get("support_reason") != "TABLE_INTERSECTION"
            or bool(table_items)
        )
    )
    table_certificate = bool(table_items and direct_relation_supported)
    proof_items = table_items or (item,)
    own_link = any(
        link.atom_id == atom.atom_id
        and any(proof.chunk_id == link.chunk_id for proof in proof_items)
        for link in links
    ) or (single_atom_direct and not links and direct_relation_supported)
    root_link = any(
        link.atom_id is None
        and any(proof.chunk_id == link.chunk_id for proof in proof_items)
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
    source_scope_supported = not atom.source_qualifier or _normalized(
        atom.source_qualifier
    ) in _normalized(source_text)
    direct_constraints_supported = all(
        any(
            _constraint_supported(
                constraint.kind.value, constraint.value, proof
            )
            for proof in proof_items
        )
        for constraint in atom.constraints
    )
    group_constraints_supported = (
        alignment is None
        or not group_id
        or alignment.group_id != group_id
        or all(passed for _kind, passed in alignment.constraint_checks)
    )
    constraints_supported = group_constraints_supported and (
        direct_constraints_supported
        or bool(
            alignment
            and alignment.structural_relation_proven
            and metadata.get("group_complete") is True
        )
    )
    direct_target = bool(
        _normalized(atom.target)
        and any(
            _normalized(atom.target) in _normalized(proof.citation_text)
            for proof in proof_items
        )
    )
    group_owned = (
        alignment is not None
        and alignment.qualification is not AlignmentQualification.REJECTED
        and alignment.group_id == group_id
    )
    group_gate = group_owned and alignment is not None and alignment.publishable
    relation_supported = (
        direct_relation_supported
        or bool(
            alignment
            and alignment.structural_relation_proven
            and metadata.get("group_complete") is True
        )
    ) and (
        not group_owned
        or table_certificate
        or bool(alignment and alignment.relation_compatible)
    )
    target_owned = direct_target or group_owned or table_certificate
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
        group_gate
        if structural
        else alignment is None
        or group_gate
        or group_id is None
        or table_certificate
    )
    retrieval_relevant = cited and (own_link or root_link or group_owned)
    direct_allowed = (
        atom.answer_shape in _SCALAR_SHAPES
        and direct_target
        and (own_link or root_trusted)
        and structure_safe
    )
    group_allowed = group_gate and structure_safe
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
    if group_owned and not group_gate and not table_certificate:
        reasons.append("ATOM_ALIGNMENT_NOT_PUBLISHABLE")
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
            heading_counts[(group.document_version_id, group.heading_path)] == 1
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
        structural_relation_proven = bool(
            group.complete
            and atom.answer_shape in _STRUCTURAL_RELATIONS
            and len(group.member_chunk_ids) > 1
            and score >= policy.atom_group_strong_anchor_threshold
            and any(
                _STRUCTURAL_RELATIONS[atom.answer_shape].search(
                    _normalized(anchor)
                )
                for anchor in anchor_texts
                if _normalized(atom.target) in _normalized(anchor)
            )
        )
        relation_compatible = structural_relation_proven or bool(
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
                structural_relation_proven=structural_relation_proven,
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
        SequenceMatcher(None, target, anchor)
        .find_longest_match(0, len(target), 0, len(anchor))
        .size
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
