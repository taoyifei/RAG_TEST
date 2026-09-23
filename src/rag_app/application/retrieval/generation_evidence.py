"""把可引用检索结果组成有界生成证据包，保留发布前独立核验。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from rag_app.application.retrieval.atom_group_alignment import (
    AlignmentQualification,
    align_atom_to_groups,
)
from rag_app.application.retrieval.evidence import (
    _evidence_item,
    group_source_maps_covered,
)
from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.application.retrieval.filters import apply_candidate_filters
from rag_app.application.retrieval.source_scope import evidence_allowed_for_atom
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AtomFactBinding,
    ChannelHit,
    ChunkRole,
    EvidenceGroup,
    EvidenceItem,
    EvidenceReadUnit,
    FieldCandidate,
    FieldResolution,
    PhysicalTableFact,
    PhysicalTableHeader,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
    SourceSpan,
)
from rag_app.core.models.common import JsonObject, freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    AtomStatus,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
    SourceResolution,
)
from rag_app.core.query_text import (
    literal_relation_modifiers_supported,
    named_table_label_in_query,
    query_without_source_qualifier,
    table_axis_label_in_query,
)
from rag_app.core.source_compatibility import (
    source_compatibility,
    table_cell_coordinate,
)

GENERATION_EVIDENCE_PACK_REVISION = "wb08r-generation-evidence-v18"
_MIN_TABLE_FACT_COLUMNS = 2
_TABLE_ROW_LABEL_COLUMN = 0
_MAX_RESERVED_PREDECESSOR_CHUNKS = 2
_STRUCTURED_GROUP_TYPES = frozenset(
    {"LIST_GROUP", "PROCEDURE_GROUP", "SECTION_GROUP", "TABLE_ROW_GROUP"}
)
_TABLE_ROW = re.compile(r"^tr:(\d+)$")
_TABLE_NODE_ID = re.compile(r"^node_[0-9a-f]{32}$")
_MIN_TABLE_SUBJECT_CHARS = 3
_MIN_TABLE_ACTION_CHARS = 12
_MIN_QUESTION_SOURCE_RUN = 4
_MIN_READING_LABEL_CHARS = 2
_MAX_READING_LABEL_CHARS = 64
_MIN_READING_LABEL_COVERAGE = 0.5
_MIN_READING_RELATION_CELLS = 2
_MAX_INFERRED_HEADER_CELL_CHARS = 16
_TEMPLATE_BODY = re.compile(
    r"正文|具体内容|具体字段|怎么填|如何填写|填写方法|占位|示例|正式要求"
)
_LITERAL_QUANTITY = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*"
    r"(毫秒|分钟|小时|秒|天|日|周|个月|月|年|%|％|元|人|次|个|件|项)"
)


class EvidenceAdmissionStatus(StrEnum):
    """区分可阅读证据、结构部分证据和硬性排除。"""

    ADMITTED = "ADMITTED"
    ADMITTED_STRUCTURED_PARTIAL = "ADMITTED_STRUCTURED_PARTIAL"
    REJECTED_HARD = "REJECTED_HARD"


class EvidenceAdmissionReason(StrEnum):
    """可审计的准入依据与硬拒原因。"""

    ACTIVE_CITABLE = "ACTIVE_CITABLE"
    ROOT_RETRIEVAL = "ROOT_RETRIEVAL"
    ATOM_RETRIEVAL = "ATOM_RETRIEVAL"
    RERANK_TOP = "RERANK_TOP"
    COMPLETE_GROUP = "COMPLETE_GROUP"
    PARTIAL_GROUP = "PARTIAL_GROUP"
    TARGET_SOFT_MISMATCH = "TARGET_SOFT_MISMATCH"
    RELATION_SOFT_MISMATCH = "RELATION_SOFT_MISMATCH"
    NON_CITABLE = "NON_CITABLE"
    INACTIVE_VERSION = "INACTIVE_VERSION"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    EXPLICIT_SOURCE_MISMATCH = "EXPLICIT_SOURCE_MISMATCH"
    TEMPLATE_BODY_UNAVAILABLE = "TEMPLATE_BODY_UNAVAILABLE"
    TABLE_ROW_CONFLICT = "TABLE_ROW_CONFLICT"
    STRUCTURAL_SIBLING_CONFLICT = "STRUCTURAL_SIBLING_CONFLICT"
    HARD_LITERAL_CONTRADICTION = "HARD_LITERAL_CONTRADICTION"


@dataclass(frozen=True, slots=True)
class GenerationEvidenceEntry:
    """一个 SourceSpan 的准入结果，不表示其能证明任何具体 Claim。"""

    support_id: str
    evidence_item: EvidenceItem
    source_group_id: str | None
    linked_atom_ids: tuple[str, ...]
    admission_status: EvidenceAdmissionStatus
    hard_reject_reasons: tuple[EvidenceAdmissionReason, ...]
    soft_signals: tuple[EvidenceAdmissionReason, ...]
    rerank_rank: int | None
    source_order: int | None
    table_node_id: str | None = None
    table_group_id: str | None = None
    table_row_index: int | None = None


@dataclass(frozen=True, slots=True)
class GenerationEvidencePack:
    """只把 admitted entries 交给模型；拒绝项供 Trace 诊断。"""

    original_query: str
    resolved_root_query: str
    entries: tuple[GenerationEvidenceEntry, ...]
    rejected_entries: tuple[GenerationEvidenceEntry, ...]
    per_atom_candidate_support_ids: tuple[tuple[str, tuple[str, ...]], ...]
    complete_group_ids: tuple[str, ...]
    partial_group_ids: tuple[str, ...]
    missing_atom_ids: tuple[str, ...]
    pack_revision: str = GENERATION_EVIDENCE_PACK_REVISION
    trusted_source_groups: tuple[EvidenceGroup, ...] = ()
    per_atom_source_certificates: tuple[tuple[str, str, JsonObject], ...] = ()
    priority_source_units: tuple[tuple[str, tuple[str, ...]], ...] = ()
    reading_unit_reason_codes: tuple[str, ...] = ()
    physical_table_facts: tuple[PhysicalTableFact, ...] = ()
    atom_fact_bindings: tuple[AtomFactBinding, ...] = ()
    field_candidates: tuple[FieldCandidate, ...] = ()
    field_resolutions: tuple[FieldResolution, ...] = ()
    field_resolution_active: bool = False
    field_resolution_execution_state: str = "NOT_NEEDED"
    field_resolution_failure_reason: str | None = None
    field_resolution_pending_atom_ids: tuple[str, ...] = ()
    field_resolution_contract_sha256: str | None = None
    field_resolution_capability_profile_sha256: str | None = None

    @property
    def evidence(self) -> tuple[EvidenceItem, ...]:
        """返回按包内稳定 Support ID 编号的可生成证据。"""
        return tuple(entry.evidence_item for entry in self.entries)

    def pre_generation_availability(
        self, matrix: AtomSupportMatrix
    ) -> dict[str, str]:
        """按每个 Atom 的包内候选和结构闭合状态给出生成前可用性。"""
        by_support_id = {entry.support_id: entry for entry in self.entries}
        available: dict[str, str] = {}
        for atom_id, support_ids in self.per_atom_candidate_support_ids:
            if matrix.for_atom(atom_id).status is AtomStatus.CONTRADICTORY:
                available[atom_id] = "HARD_CONFLICT"
                continue
            entries = tuple(
                by_support_id[support_id]
                for support_id in support_ids
                if support_id in by_support_id
            )
            if not entries:
                available[atom_id] = "NO_CANDIDATE"
            elif any(
                entry.source_group_id in self.complete_group_ids
                for entry in entries
            ):
                available[atom_id] = "STRUCTURED_COMPLETE"
            elif any(
                entry.source_group_id in self.partial_group_ids
                or entry.evidence_item.table_context
                for entry in entries
            ):
                available[atom_id] = "STRUCTURED_PARTIAL"
            else:
                available[atom_id] = "EVIDENCE_AVAILABLE"
        return available

    @property
    def hard_rejected_sources(self) -> tuple[dict[str, object], ...]:
        """仅给评测和 Trace 提供来源身份及硬拒原因。"""
        return tuple(
            {
                "document_version_id": entry.evidence_item.document_version_id,
                "chunk_id": entry.evidence_item.chunk_id,
                "node_ids": tuple(
                    span.node_id
                    for span in entry.evidence_item.source_spans
                    if span.node_id is not None
                ),
                "reasons": tuple(
                    reason.value for reason in entry.hard_reject_reasons
                ),
            }
            for entry in self.rejected_entries
        )

    def structural_sibling_observation(
        self, groups: tuple[GroupCandidate, ...]
    ) -> dict[str, object]:
        """核对结构归属与表格行；无法辨别同表兄弟行时保留 PARTIAL。"""
        members_by_group = {
            group.group_id: frozenset(group.group.member_chunk_ids)
            for group in groups
        }
        unknown_structural_count = 0
        unknown_table_coordinate_count = 0
        rows_by_table: dict[tuple[str | None, str], set[int]] = defaultdict(set)
        for entry in self.entries:
            group_type = dict(entry.evidence_item.metadata).get(
                "evidence_group_type"
            )
            structured = (
                entry.evidence_item.table_context
                or entry.table_row_index is not None
                or group_type in _STRUCTURED_GROUP_TYPES
                or entry.source_group_id is not None
            )
            table_row_located = (
                entry.evidence_item.table_context
                and entry.source_group_id is None
                and entry.table_node_id is not None
                and entry.table_row_index is not None
            )
            if (
                structured
                and not table_row_located
                and (
                    entry.source_group_id is None
                    or entry.source_group_id not in members_by_group
                )
            ):
                unknown_structural_count += 1
            if entry.evidence_item.table_context:
                if entry.table_node_id is None or entry.table_row_index is None:
                    unknown_table_coordinate_count += 1
                else:
                    rows_by_table[
                        (
                            entry.evidence_item.document_version_id,
                            entry.table_node_id,
                        )
                    ].add(entry.table_row_index)
        unknown_group_count = sum(
            1
            for entry in self.entries
            if entry.source_group_id is not None
            and entry.source_group_id not in members_by_group
        )
        conflict_count = sum(
            1
            for entry in self.entries
            if entry.source_group_id in members_by_group
            and entry.evidence_item.chunk_id
            not in members_by_group[entry.source_group_id]
        )
        multi_row_table_count = sum(
            1 for rows in rows_by_table.values() if len(rows) > 1
        )
        return {
            "structural_sibling_pollution_count": conflict_count,
            "structural_sibling_observation_status": (
                "PARTIAL"
                if (unknown_structural_count or unknown_table_coordinate_count)
                else "COMPLETE"
            ),
            "structural_sibling_unknown_group_count": unknown_group_count,
            "structural_sibling_unknown_structural_count": (
                unknown_structural_count
            ),
            "structural_sibling_unknown_table_coordinate_count": (
                unknown_table_coordinate_count
            ),
            # 多行候选均有独立的表节点与行坐标，不等于无法辨别兄弟行。
            "structural_sibling_multi_row_table_count": multi_row_table_count,
            "structural_sibling_observation_scope": (
                "ADMITTED_STRUCTURAL_GROUP_AND_TABLE_ROW"
            ),
        }


def project_evidence_read_units(
    evidence: tuple[EvidenceItem, ...],
    physical_table_facts: tuple[PhysicalTableFact, ...] = (),
) -> tuple[EvidenceReadUnit, ...]:
    """把已准入来源投影成模型可读、服务端可恢复的短编号单元。

    表格事实只向模型展示一次可读的行、列和值；完整的真实跨度仍由
    ``support_ids`` 与 ``fact_id`` 留在服务端。未形成物理事实的表格原文只作为
    字面片段发送，不补行名、列名或隐藏单元格关系。普通来源保持原始
    完整引用，不在这里判断其是否回答了用户问题。

    Args:
        evidence: 当前生成尝试获准读取的真实来源。
        physical_table_facts: 已按物理坐标闭合的表格事实。

    Returns:
        按当前请求编号的阅读单元，编号只在对应发送包内有效。

    """
    by_id = {item.support_id: item for item in evidence}
    if len(by_id) != len(evidence):
        raise ValueError("READ_UNIT_DUPLICATE_SUPPORT")
    complete_facts = tuple(
        fact
        for fact in physical_table_facts
        if set(fact.all_support_ids) <= by_id.keys()
    )
    physical_support_ids = {
        support_id
        for fact in complete_facts
        for support_id in fact.all_support_ids
    }
    units: list[EvidenceReadUnit] = []

    def source_complete(items: tuple[EvidenceItem, ...]) -> bool:
        return bool(items) and all(
            item.publishable
            and item.source_spans
            and all(span.is_citable for span in item.source_spans)
            for item in items
        )

    def is_table_fragment(item: EvidenceItem) -> bool:
        return item.table_locator is not None or any(
            part.startswith("tbl:")
            for span in item.source_spans
            for part in span.structural_path
        )

    grouped_items: list[list[EvidenceItem]] = []
    table_group_indexes: dict[tuple[object, ...], int] = {}
    for item in evidence:
        if item.support_id in physical_support_ids:
            continue
        cell = table_cell_coordinate(item)
        if is_table_fragment(item) and cell is not None:
            table_group_key = (
                item.source_identity_scope,
                item.document_version_id,
                item.chunk_id,
                item.table_locator,
                cell[0],
                cell[1],
            )
            group_index = table_group_indexes.get(table_group_key)
            if group_index is not None:
                grouped_items[group_index].append(item)
                continue
            table_group_indexes[table_group_key] = len(grouped_items)
        grouped_items.append([item])

    for raw_items in grouped_items:
        items = tuple(raw_items)
        item = items[0]
        table_fragment = is_table_fragment(item)
        metadata = dict(item.metadata)
        group_type = metadata.get("evidence_group_type")
        kind: Literal["paragraph", "list_item", "table_fact", "catalog_entry"]
        if group_type == "CATALOG_ENTRY":
            kind = "catalog_entry"
        elif group_type in {"LIST_GROUP", "PROCEDURE_GROUP"}:
            kind = "list_item"
        else:
            kind = "paragraph"
        context = {
            "source_label": item.source_label,
            "heading_path": list(item.heading_path),
            "table_locator": item.table_locator,
            # 该单元只证明它自身的逐字内容。只有下方带
            # fact_id 的 table_fact 才允许使用行、列、值关系。
            "structure_scope": (
                "literal_table_fragment" if table_fragment else None
            ),
            "table_relation_complete": False if table_fragment else None,
        }
        units.append(
            EvidenceReadUnit(
                unit_id=f"E{len(units) + 1}",
                kind=kind,
                text="\n".join(
                    dict.fromkeys(
                        source.citation_text.strip() for source in items
                    )
                ),
                source_context=freeze_json_object(
                    {
                        key: value
                        for key, value in context.items()
                        if value not in (None, "", ())
                    }
                ),
                support_ids=tuple(source.support_id for source in items),
                source_complete=source_complete(items),
            )
        )
    for fact in complete_facts:
        fact_items = tuple(by_id[item] for item in fact.all_support_ids)
        row_labels = tuple(
            dict.fromkeys(
                by_id[support_id].citation_text.strip()
                for support_id in fact.row_label_support_ids
            )
        )
        headers = tuple(
            "".join(
                by_id[support_id].citation_text
                for support_id in header.support_ids
            ).strip()
            for header in fact.headers
        )
        values = tuple(
            dict.fromkeys(
                by_id[support_id].citation_text.strip()
                for support_id in fact.value_support_ids
            )
        )
        anchor = by_id[fact.value_support_ids[0]]
        units.append(
            EvidenceReadUnit(
                unit_id=f"E{len(units) + 1}",
                kind="table_fact",
                text=(
                    f"行：{' / '.join(row_labels)}；"
                    f"列：{' / '.join(headers)}；"
                    f"值：{' / '.join(values)}"
                ),
                source_context=freeze_json_object(
                    {
                        "source_label": anchor.source_label,
                        "heading_path": list(anchor.heading_path),
                        "table_locator": anchor.table_locator,
                    }
                ),
                support_ids=fact.all_support_ids,
                fact_id=fact.fact_id,
                source_complete=source_complete(fact_items),
            )
        )
    return tuple(units)


def _normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _question_source_run(query: str, source: str) -> bool:
    """要求原问与原文单元格有连续对象锚点，避免泛主题补证。"""
    words = "".join(char for char in query if "\u4e00" <= char <= "\u9fff")
    normalized_source = _normalized(source)
    return any(
        words[index : index + _MIN_QUESTION_SOURCE_RUN] in normalized_source
        for index in range(len(words) - _MIN_QUESTION_SOURCE_RUN + 1)
    )


def _identity(item: EvidenceItem) -> tuple[object, ...]:
    """一个来源片段跨 Root/Atom/Group 的稳定去重键。"""
    return (stable_support_key(item),)


def _rank(item: EvidenceItem) -> tuple[int, int, int, str]:
    """统一 Rerank 优先，扩展项保留原融合顺序。"""
    return (
        0 if item.rerank_rank is not None else 1,
        item.rerank_rank or 2**31 - 1,
        item.fusion_rank or 2**31 - 1,
        item.chunk_id,
    )


def _source_order(item: EvidenceItem) -> int | None:
    ordinals = (
        span.source_anchor.ordinal
        for span in item.source_spans
        if span.source_anchor is not None
    )
    return min(ordinals, default=None)


def _source_matches(atom: QueryAtom, item: EvidenceItem) -> bool:
    if atom.source_scope is not None:
        return evidence_allowed_for_atom(atom.source_scope, item)
    if not atom.source_qualifier:
        return True
    metadata = dict(item.metadata)
    identity = " ".join(
        (item.display_name or "", str(metadata.get("document_title", "")))
    )
    return _normalized(atom.source_qualifier) in _normalized(identity)


def _source_restricted(atom: QueryAtom) -> bool:
    """显式来源合同不能因 Planner 漏填 qualifier 而失效。"""
    return bool(
        atom.source_qualifier
        or (
            atom.source_scope is not None
            and atom.source_scope.resolution is not SourceResolution.OPEN
        )
    )


def _candidate_visible(
    candidate: RankedChunk,
    request: SearchRequest,
    active_revision_id: str,
    excluded_document_ids: frozenset[str],
) -> bool:
    chunk = candidate.hydrated.chunk
    if (
        chunk.project_id != request.scope.project_id
        or chunk.knowledge_base_id != request.scope.knowledge_base_id
        or chunk.index_revision_id != active_revision_id
        or chunk.version.document_id in excluded_document_ids
    ):
        return False
    hit = ChannelHit(
        revision_id=active_revision_id,
        chunk_id=chunk.chunk_id,
        document_id=chunk.version.document_id,
        document_version_id=chunk.version.document_version_id,
        role=chunk.role.value,
        section_id=chunk.section_id,
        content_sha256=chunk.content_sha256,
        channel="generation-admission",
        rank=1,
        raw_score=0.0,
    )
    return bool(apply_candidate_filters((hit,), request))


def _table_rows(item: EvidenceItem) -> frozenset[str]:
    rows: set[str] = set()
    for span in item.source_spans:
        rows.update(
            part for part in span.structural_path if _TABLE_ROW.fullmatch(part)
        )
    return frozenset(rows)


def _table_row_index(item: EvidenceItem) -> int | None:
    """从 SourceSpan 读取唯一表格行，冲突或缺失时不猜测。"""
    rows: set[int] = set()
    for span in item.source_spans:
        if not span.is_citable:
            continue
        if span.source_anchor is not None:
            anchor_row = span.source_anchor.row_index
            if anchor_row is not None:
                rows.add(anchor_row)
        rows.update(
            int(match[1])
            for part in span.structural_path
            if (match := _TABLE_ROW.fullmatch(part)) is not None
        )
    return next(iter(rows)) if len(rows) == 1 else None


def _table_node_id(
    candidate: RankedChunk | None, row_index: int | None
) -> str | None:
    """用 canonical Chunk atom 的行身份定位原表节点。"""
    if candidate is None or row_index is None:
        return None
    atoms = dict(candidate.hydrated.chunk.metadata).get("atoms")
    if not isinstance(atoms, (list, tuple)):
        return None
    node_ids: set[str] = set()
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        metadata = atom.get("metadata")
        if not isinstance(metadata, dict):
            continue
        node_id = metadata.get("table_node_id")
        row = metadata.get("row_index")
        if (
            isinstance(node_id, str)
            and _TABLE_NODE_ID.fullmatch(node_id)
            and isinstance(row, int)
            and not isinstance(row, bool)
            and row == row_index
        ):
            node_ids.add(node_id)
    return next(iter(node_ids)) if len(node_ids) == 1 else None


def _hard_literal_conflict(
    atom: QueryAtom, item: EvidenceItem, certificate: object
) -> bool:
    """仅在同一目标关系已被来源认证时识别单值、同单位的直接矛盾。"""
    if not isinstance(certificate, dict) or (
        certificate.get("status") != "SUPPORTED"
        or _normalized(str(certificate.get("query_target") or ""))
        != _normalized(atom.target)
        or _normalized(
            str(certificate.get("requested_relation_or_attribute") or "")
        )
        != _normalized(atom.relation)
    ):
        return False
    source_values = _LITERAL_QUANTITY.findall(
        unicodedata.normalize("NFKC", item.citation_text)
    )
    if len(source_values) != 1:
        return False
    for constraint in atom.constraints:
        if constraint.kind.value not in {"NUMBER", "DURATION"}:
            continue
        requested = _LITERAL_QUANTITY.findall(
            unicodedata.normalize("NFKC", constraint.value)
        )
        if (
            len(requested) == 1
            and requested[0][1] == source_values[0][1]
            and requested[0][0] != source_values[0][0]
        ):
            return True
    return False


def _hard_reasons(  # noqa: PLR0913
    item: EvidenceItem,
    *,
    candidate: RankedChunk | None,
    groups_by_id: dict[str, GroupCandidate],
    possible_atoms: tuple[QueryAtom, ...],
    request: SearchRequest,
    active_revision_id: str,
    excluded_document_ids: frozenset[str],
) -> tuple[EvidenceAdmissionReason, ...]:
    """只检查来源身份、显式范围和明确结构冲突。"""
    reasons: list[EvidenceAdmissionReason] = []
    if (
        not item.publishable
        or not item.source_spans
        or any(not span.is_citable for span in item.source_spans)
    ):
        reasons.append(EvidenceAdmissionReason.NON_CITABLE)
    if candidate is None or not _candidate_visible(
        candidate, request, active_revision_id, excluded_document_ids
    ):
        reasons.append(EvidenceAdmissionReason.SCOPE_MISMATCH)
    elif (item.document_id, item.document_version_id) != (
        candidate.hydrated.chunk.version.document_id,
        candidate.hydrated.chunk.version.document_version_id,
    ):
        reasons.append(EvidenceAdmissionReason.INACTIVE_VERSION)
    if possible_atoms and all(
        _source_restricted(atom) and not _source_matches(atom, item)
        for atom in possible_atoms
    ):
        reasons.append(EvidenceAdmissionReason.EXPLICIT_SOURCE_MISMATCH)
    metadata = dict(item.metadata)
    if any(
        _hard_literal_conflict(atom, item, metadata.get("answer_support"))
        for atom in possible_atoms
    ):
        reasons.append(EvidenceAdmissionReason.HARD_LITERAL_CONTRADICTION)
    group_id = metadata.get("evidence_group_id")
    group = groups_by_id.get(group_id) if isinstance(group_id, str) else None
    if group is not None and item.chunk_id not in group.group.member_chunk_ids:
        reasons.append(EvidenceAdmissionReason.STRUCTURAL_SIBLING_CONFLICT)
    if item.table_context and len(_table_rows(item)) > 1:
        reasons.append(EvidenceAdmissionReason.TABLE_ROW_CONFLICT)
    certificate = metadata.get("answer_support")
    catalog_title_only = (
        metadata.get("evidence_group_type") == "CATALOG_ENTRY"
        or (
            isinstance(certificate, dict)
            and certificate.get("support_reason") == "CATALOG_TITLE_EXISTS"
        )
        or item.citation_text.startswith("模板目录项：")
    )
    if catalog_title_only and _TEMPLATE_BODY.search(request.text):
        reasons.append(EvidenceAdmissionReason.TEMPLATE_BODY_UNAVAILABLE)
    return tuple(dict.fromkeys(reasons))


def _group_items(group: GroupCandidate) -> tuple[EvidenceItem, ...]:
    """只从已检索的真实组成员 SourceSpan 物化引用，不拼接原文。"""
    items: list[EvidenceItem] = []
    for index, member in enumerate(group.members, 1):
        chunk = member.hydrated.chunk
        for span in chunk.source_spans:
            if not span.is_citable:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if not quote:
                continue
            item = _evidence_item(member, span, quote, "S0")
            items.append(
                item.model_copy(
                    update={
                        "metadata": freeze_json_object(
                            {
                                **dict(item.metadata),
                                "evidence_group_id": group.group_id,
                                "evidence_group_type": group.group.kind.value,
                                "group_member_index": index,
                                "group_member_count": len(group.members),
                                "group_complete": group.complete,
                            }
                        )
                    }
                )
            )
    return tuple(items)


def _physical_source(item: EvidenceItem) -> bool:
    """仅将有完整字符位置的独立原文用作阅读单元锚点。

    Args:
        item: 保留真实 SourceSpan 的阅读候选。

    Returns:
        是否能核对一个明确的原文区间；不表示支持任何事实。

    """
    if len(item.source_spans) != 1:
        return False
    span = item.source_spans[0]
    start, end = span.source_start_char, span.source_end_char
    return (
        type(start) is int
        and type(end) is int
        and start >= 0
        and end - start == len(item.citation_text)
        and bool(item.citation_text)
        and source_compatibility((item,)).compatible
    )


def _reading_label_score(query: str, label: str) -> tuple[float, int]:
    """仅给真实行标签的原问字面重合排序，不建立名称等价关系。

    Args:
        query: 原问或 Atom 的合法检索文本。
        label: 已由原表第一列证明的行标签。

    Returns:
        最长连续重合占标签比例和长度；短零散重合返回零。

    """
    query, label = _normalized(query), _normalized(label)
    if not _MIN_READING_LABEL_CHARS <= len(label) <= _MAX_READING_LABEL_CHARS:
        return (0.0, 0)
    longest = max(
        (
            end - start
            for start in range(len(label))
            for end in range(start + _MIN_READING_LABEL_CHARS, len(label) + 1)
            if label[start:end] in query
        ),
        default=0,
    )
    score = longest / len(label)
    return (
        (score, longest) if score >= _MIN_READING_LABEL_COVERAGE else (0.0, 0)
    )


def _reading_table_identity(
    item: EvidenceItem,
) -> tuple[tuple[object, ...], int, int] | None:
    """联合规范节点映射与统一坐标合同认证逻辑表和真实单元格。

    Args:
        item: 由 canonical Chunk 的 SourceSpan 物化的证据。

    Returns:
        含授权范围、版本、part/story 和表节点的身份及行列。

    """
    cell = table_cell_coordinate(item)
    metadata = dict(item.metadata)
    node = metadata.get("table_logical_node_id")
    row = metadata.get("table_logical_row_index")
    if (
        cell is None
        or not isinstance(node, str)
        or not _TABLE_NODE_ID.fullmatch(node)
        or type(row) is not int
        or row != cell[1]
        or not _physical_source(item)
    ):
        return None
    table = cell[0]
    return (
        (*item.source_identity_scope, *table[:4], node, table[-1]),
        row,
        cell[2],
    )


def _inferred_header_tables(
    candidate_by_id: dict[str, RankedChunk],
) -> frozenset[tuple[object, ...]]:
    """仅从同表完整首行的短列标签推断未标记表头。"""
    first_rows: dict[
        tuple[object, ...], dict[int, list[str]]
    ] = defaultdict(lambda: defaultdict(list))
    data_tables: set[tuple[object, ...]] = set()
    marked_tables: set[tuple[object, ...]] = set()
    for candidate in candidate_by_id.values():
        chunk = candidate.hydrated.chunk
        if chunk.role is not ChunkRole.TABLE:
            continue
        for span in chunk.source_spans:
            if not span.is_citable or span.is_repeated:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if not quote:
                continue
            item = _evidence_item(candidate, span, quote, "S0")
            cell = _reading_table_identity(item)
            if cell is None:
                continue
            table, row, column = cell
            if _reading_header(item, candidate):
                marked_tables.add(table)
            elif row == 0:
                first_rows[table][column].append(quote)
            else:
                data_tables.add(table)
    return frozenset(
        table
        for table, columns in first_rows.items()
        if table in data_tables
        and table not in marked_tables
        and _TABLE_ROW_LABEL_COLUMN in columns
        and len(columns) >= _MIN_TABLE_FACT_COLUMNS
        and all(
            len(" ".join(values)) <= _MAX_INFERRED_HEADER_CELL_CHARS
            for values in columns.values()
        )
    )


def _reading_header(
    item: EvidenceItem,
    candidate: RankedChunk,
    inferred_tables: frozenset[tuple[object, ...]] = frozenset(),
) -> bool:
    """只使用解析器明确认证且实际映射到该节点的表头。

    Args:
        item: 表头候选原文。
        candidate: 保留 canonical atom 映射的合法候选。
        inferred_tables: 已按同一表首行短标签确认的未标记表头。

    Returns:
        是否具备源表头标记，不把任意第一行当作表头。

    """
    cell = _reading_table_identity(item)
    if cell is not None and cell[0] in inferred_tables and cell[1] == 0:
        return True
    atoms = dict(candidate.hydrated.chunk.metadata).get("atoms")
    if not isinstance(atoms, (list, tuple)):
        return False
    nodes = {span.node_id for span in item.source_spans}
    for atom in atoms:
        metadata = atom.get("metadata") if isinstance(atom, dict) else None
        if not isinstance(metadata, dict):
            continue
        mapping = metadata.get("cell_source_node_ids")
        if metadata.get("header_strategy") != "tblHeader" or not isinstance(
            mapping, dict
        ):
            continue
        mapped = {
            node
            for values in mapping.values()
            if isinstance(values, (list, tuple))
            for node in values
            if isinstance(node, str)
        }
        if nodes <= mapped:
            return True
    return False


def _reading_seed_rank(
    item: EvidenceItem, candidates: dict[str, RankedChunk]
) -> int | None:
    """同一规范行的命中值可选出行名，不给闭合来源伪造 rerank 分数。"""
    if item.rerank_rank is not None:
        return item.rerank_rank
    candidate = candidates[item.chunk_id]
    if candidate.expansion_reason != "TABLE_CANONICAL_RELATION_UNIT":
        return None
    cell = _reading_table_identity(item)
    if cell is None:
        return None
    node = dict(item.metadata).get("table_logical_node_id")
    ranks = [
        seed.rerank_rank
        for key in candidate.expansion_seed_ids
        if (seed := candidates.get(key)) is not None
        and seed.rerank_rank is not None
        and _table_node_id(seed, cell[1]) == node
        and seed.hydrated.chunk.version.document_version_id
        == item.document_version_id
    ]
    return min(ranks, default=None)


def _reading_header_columns(
    item: EvidenceItem,
    candidate: RankedChunk,
    inferred_tables: frozenset[tuple[object, ...]] = frozenset(),
) -> frozenset[int]:
    """按规范单元格 grid span 展开祖先列头，不靠相邻列猜合并关系。"""
    cell = _reading_table_identity(item)
    if cell is None or not _reading_header(
        item, candidate, inferred_tables
    ):
        return frozenset()
    _table, row, column = cell
    columns = {column}
    atoms = dict(candidate.hydrated.chunk.metadata).get("atoms", [])
    if not isinstance(atoms, (list, tuple)):
        return frozenset(columns)
    for atom in atoms:
        metadata = atom.get("metadata") if isinstance(atom, dict) else None
        if not isinstance(metadata, dict) or metadata.get("row_index") != row:
            continue
        coordinates = metadata.get("cell_coordinates", [])
        if not isinstance(coordinates, (list, tuple)):
            continue
        for coordinate in coordinates:
            match = re.fullmatch(
                r"r(\d+):c(\d+):rs(\d+):cs(\d+)", str(coordinate)
            )
            if match and (int(match[1]), int(match[2])) == (row, column):
                columns.update(range(column, column + int(match[4])))
    return frozenset(columns)


def _ordered_cell_members(
    items: tuple[EvidenceItem, ...],
) -> tuple[EvidenceItem, ...]:
    """按原始节点和字符位置稳定排列同一物理单元格的多个片段。"""
    return tuple(
        sorted(
            items,
            key=lambda item: (
                min(
                    (
                        span.source_anchor.ordinal
                        for span in item.source_spans
                        if span.source_anchor is not None
                    ),
                    default=2**31,
                ),
                min(
                    (
                        span.source_start_char
                        for span in item.source_spans
                        if span.source_start_char is not None
                    ),
                    default=2**31,
                ),
                item.support_id,
            ),
        )
    )


def _physical_table_facts(
    items: tuple[EvidenceItem, ...],
    candidate_by_id: dict[str, RankedChunk],
) -> tuple[PhysicalTableFact, ...]:
    """先按真实坐标建立事实，再由问句单独选择可用事实。"""
    rows: dict[
        tuple[tuple[object, ...], int], dict[int, list[EvidenceItem]]
    ] = defaultdict(lambda: defaultdict(list))
    headers: dict[
        tuple[object, ...],
        dict[tuple[int, int, tuple[int, ...]], list[EvidenceItem]],
    ] = defaultdict(lambda: defaultdict(list))
    inferred_tables = _inferred_header_tables(candidate_by_id)
    for item in items:
        cell = _reading_table_identity(item)
        candidate = candidate_by_id.get(item.chunk_id)
        if cell is None or candidate is None:
            continue
        table, row, column = cell
        if _reading_header(item, candidate, inferred_tables):
            covered = tuple(
                sorted(
                    _reading_header_columns(
                        item, candidate, inferred_tables
                    )
                )
            )
            if covered:
                headers[table][row, column, covered].append(item)
            continue
        rows[table, row][column].append(item)

    facts: list[PhysicalTableFact] = []
    for (table, row), columns in sorted(rows.items(), key=repr):
        if (
            len(columns) < _MIN_TABLE_FACT_COLUMNS
            or _TABLE_ROW_LABEL_COLUMN not in columns
        ):
            continue
        label_column = _TABLE_ROW_LABEL_COLUMN
        labels = _ordered_cell_members(tuple(columns[label_column]))
        first = labels[0]
        node = dict(first.metadata).get("table_logical_node_id")
        if (
            not first.document_id
            or not first.document_version_id
            or not isinstance(node, str)
        ):
            continue
        table_key = canonical_sha256(
            {"revision": "wb08r-physical-table-v1", "identity": table}
        )
        for value_column, raw_values in sorted(columns.items()):
            if value_column == label_column:
                continue
            header_groups = tuple(
                PhysicalTableHeader(
                    row_index=header_row,
                    column_indexes=covered,
                    support_ids=tuple(
                        item.support_id
                        for item in _ordered_cell_members(tuple(raw_headers))
                    ),
                )
                for (
                    header_row,
                    _header_column,
                    covered,
                ), raw_headers in sorted(headers.get(table, {}).items())
                if value_column in covered
            )
            if not header_groups:
                continue
            facts.append(
                PhysicalTableFact(
                    fact_id=canonical_sha256(
                        {
                            "revision": "wb08r-physical-table-fact-v1",
                            "table_key": table_key,
                            "row": row,
                            "value_column": value_column,
                        }
                    ),
                    table_key=table_key,
                    document_id=first.document_id,
                    document_version_id=first.document_version_id,
                    table_node_id=node,
                    row_index=row,
                    row_label_column_index=label_column,
                    value_column_index=value_column,
                    row_label_support_ids=tuple(
                        item.support_id for item in labels
                    ),
                    value_support_ids=tuple(
                        item.support_id
                        for item in _ordered_cell_members(tuple(raw_values))
                    ),
                    headers=header_groups,
                )
            )
    return tuple(facts)


def _atom_fact_bindings(
    query_plan: QueryPlan,
    facts: tuple[PhysicalTableFact, ...],
    evidence: tuple[EvidenceItem, ...],
    per_atom: tuple[tuple[str, tuple[str, ...]], ...],
    priority_units: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[AtomFactBinding, ...]:
    """绑定 Atom 与事实候选，但不把候选关系升级成语义证明。"""
    by_id = {item.support_id: item for item in evidence}
    allowed = {atom_id: set(ids) for atom_id, ids in per_atom}
    priority = {
        owner: set(keys) for owner, keys in priority_units if owner != "ROOT"
    }
    bindings: list[AtomFactBinding] = []
    for atom in query_plan.atoms:
        focus = " ".join(
            (
                query_plan.original_query,
                atom.search_text,
                atom.target,
                atom.relation,
            )
        )
        for fact in facts:
            fact_ids = set(fact.all_support_ids)
            if not fact_ids <= allowed.get(atom.atom_id, set()):
                continue
            fact_keys = {
                stable_support_key(by_id[support_id])
                for support_id in fact.all_support_ids
            }
            label = "".join(
                by_id[support_id].citation_text
                for support_id in fact.row_label_support_ids
            )
            if not (
                fact_keys <= priority.get(atom.atom_id, set())
                or _reading_label_score(focus, label)[0]
            ):
                continue
            headers = " ".join(
                by_id[support_id].citation_text
                for support_id in fact.header_support_ids
            )
            values = " ".join(
                by_id[support_id].citation_text
                for support_id in fact.value_support_ids
            )
            target = _normalized(atom.target)
            relation = _normalized(atom.relation)
            normalized_label = _normalized(label)
            normalized_headers = _normalized(headers)
            original_fragment = query_without_source_qualifier(
                atom.original_fragment or "", atom.source_qualifier
            )
            explicit_axis_relation = bool(
                table_axis_label_in_query(original_fragment, label)
                and any(
                    table_axis_label_in_query(
                        original_fragment,
                        by_id[support_id].citation_text,
                    )
                    for support_id in fact.header_support_ids
                )
            )
            constraint_values = tuple(
                _normalized(constraint.value) for constraint in atom.constraints
            )
            physical_text = _normalized(" ".join((label, headers, values)))
            relation_supported = bool(
                (target or explicit_axis_relation)
                and (relation or explicit_axis_relation)
                and (
                    target in normalized_label
                    or normalized_label in target
                    or explicit_axis_relation
                )
                and (
                    relation in normalized_headers
                    or any(
                        header and header in relation
                        for header in (
                            _normalized(by_id[support_id].citation_text)
                            for support_id in fact.header_support_ids
                        )
                    )
                    or explicit_axis_relation
                )
                and literal_relation_modifiers_supported(
                    original_fragment, headers
                )
                and all(value in physical_text for value in constraint_values)
            )
            bindings.append(
                AtomFactBinding(
                    atom_id=atom.atom_id,
                    fact_id=fact.fact_id,
                    relation_status=(
                        "SUPPORTED" if relation_supported else "UNDETERMINED"
                    ),
                    requested_target=atom.target,
                    requested_relation=atom.relation,
                    requested_stage_labels=tuple(
                        constraint.value
                        for constraint in atom.constraints
                        if constraint.kind.value == "STAGE"
                    ),
                    requested_conditions=tuple(
                        constraint.value
                        for constraint in atom.constraints
                        if constraint.kind.value == "CONDITION"
                    ),
                )
            )
    return tuple(bindings)


def _priority_reading_units(  # noqa: PLR0912, PLR0913
    *,
    query_plan: QueryPlan,
    items: tuple[EvidenceItem, ...],
    candidate_by_id: dict[str, RankedChunk],
    root_evidence: tuple[EvidenceItem, ...],
    atom_candidates_by_atom: tuple[tuple[str, tuple[EvidenceItem, ...]], ...],
    policy: RetrievalPolicy,
    diagnostics: list[str] | None = None,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    """在应用层选每个 Atom 的一个有界阅读单元，不认证回答语义。

    Args:
        query_plan: 当前请求的 Root 与 Atom。
        items: 全部通过硬边界的有限来源。
        candidate_by_id: 当前合法池内的原始 Chunk 映射。
        root_evidence: 原检索器选中的 Root 阅读来源。
        atom_candidates_by_atom: 保留各 Atom 自身关系上下文的来源。
        policy: 已冻结的数量与预算约束。
        diagnostics: 可选的缺失表头等结构诊断，仅供内部 SAFE Trace。

    Returns:
        owner 与必须一起阅读的稳定来源身份；表头保持独立引用。

    """
    inferred_tables = _inferred_header_tables(candidate_by_id)
    rows: dict[tuple[tuple[object, ...], int], list[EvidenceItem]] = (
        defaultdict(list)
    )
    headers: dict[tuple[object, ...], list[EvidenceItem]] = defaultdict(list)
    for item in items:
        cell = _reading_table_identity(item)
        if cell is None:
            continue
        table, row, _column = cell
        candidate = candidate_by_id[item.chunk_id]
        if _reading_header(item, candidate, inferred_tables):
            headers[table].append(item)
        else:
            rows[(table, row)].append(item)
    units: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
    members_by_atom = dict(atom_candidates_by_atom)
    readable = {_identity(item): item for item in items}
    for atom in query_plan.atoms:
        focused: list[
            tuple[tuple[float, int, int], tuple[EvidenceItem, ...]]
        ] = []
        query = f"{query_plan.original_query} {atom.search_text}"
        for (table, _row), members in rows.items():
            labels = [
                item
                for item in members
                if (cell := _reading_table_identity(item)) is not None
                and cell[2] == 0
                and _source_matches(atom, item)
                and _reading_seed_rank(item, candidate_by_id) is not None
            ]
            for label in labels:
                score = _reading_label_score(query, label.citation_text)
                if not score[0]:
                    continue
                # 有精确列名时只保留所问列；口语关系未消歧时保留这一
                # 目标行的有限阅读上下文，不宣称整行等于所问事实。
                column_headers = headers.get(table, [])
                requested_columns = {
                    column
                    for item in column_headers
                    if _normalized(item.citation_text) in _normalized(query)
                    for column in _reading_header_columns(
                        item, candidate_by_id[item.chunk_id], inferred_tables
                    )
                    if column != _TABLE_ROW_LABEL_COLUMN
                }
                columns = (
                    requested_columns | {0} if requested_columns else set()
                )
                value_columns = {
                    cell[2]
                    for item in members
                    if (cell := _reading_table_identity(item)) is not None
                    and cell[2] > 0
                    and (not columns or cell[2] in columns)
                }
                header_columns = {
                    column
                    for item in column_headers
                    for column in _reading_header_columns(
                        item, candidate_by_id[item.chunk_id], inferred_tables
                    )
                }
                if not value_columns or not value_columns <= header_columns:
                    if diagnostics is not None:
                        diagnostics.append("TABLE_HEADER_MISSING")
                    continue
                related = tuple(
                    item
                    for item in (*members, *column_headers)
                    if not columns
                    or (_reading_table_identity(item) or ((), -1, -1))[2]
                    in columns
                    or bool(
                        columns
                    & _reading_header_columns(
                        item, candidate_by_id[item.chunk_id], inferred_tables
                    )
                    )
                )
                if (
                    len({item.chunk_id for item in related})
                    > policy.group_member_chunk_limit
                ):
                    continue
                focused.append(
                    (
                        (
                            *score,
                            -(
                                _reading_seed_rank(label, candidate_by_id)
                                or 2**31
                            ),
                        ),
                        related,
                    )
                )
        if focused:
            selected = max(focused, key=lambda value: value[0])[1]
        else:
            anchors = members_by_atom.get(atom.atom_id) or root_evidence
            anchor = next(
                (
                    readable[_identity(item)]
                    for item in anchors
                    if _identity(item) in readable
                    and _physical_source(item)
                    and _source_matches(atom, item)
                ),
                None,
            )
            if anchor is None:
                continue
            selected = (anchor,)
            for item in sorted(
                items,
                key=lambda value: (
                    _source_order(value) or 0,
                    value.source_spans[0].source_start_char or 0,
                ),
            ):
                if item == anchor or not _physical_source(item):
                    continue
                if len(selected) >= policy.group_member_chunk_limit:
                    break
                if {span.node_id for span in item.source_spans} == {
                    span.node_id for span in anchor.source_spans
                } and source_compatibility((*selected, item)).compatible:
                    selected = (*selected, item)
        units.append(
            (
                atom.atom_id,
                tuple(dict.fromkeys(_identity(item) for item in selected)),
            )
        )
    return tuple(units)


def build_generation_evidence_pack(  # noqa: PLR0912, PLR0913, PLR0915
    *,
    query_plan: QueryPlan,
    root_evidence: tuple[EvidenceItem, ...],
    atom_evidence: tuple[EvidenceItem, ...],
    atom_candidates_by_atom: tuple[
        tuple[str, tuple[EvidenceItem, ...]], ...
    ] = (),
    ranked_candidates: tuple[RankedChunk, ...],
    groups: tuple[GroupCandidate, ...],
    links: tuple[AtomCandidateLink, ...],
    request: SearchRequest,
    active_revision_id: str,
    excluded_document_ids: tuple[str, ...],
    policy: RetrievalPolicy,
) -> GenerationEvidencePack:
    """先取 Rerank Top，再公平补 Atom 与完整结构组。"""
    candidate_by_id = {
        item.hydrated.chunk.chunk_id: item for item in ranked_candidates
    }
    for group in groups:
        for member in group.members:
            # 结构闭合可能再次装入同一 Chunk；保留原扩展来源和排名，
            # 避免闭合成员覆盖 SECTION_PREDECESSOR 身份。
            candidate_by_id.setdefault(member.hydrated.chunk.chunk_id, member)
    groups_by_id = {group.group_id: group for group in groups}
    root_keys = {_identity(item) for item in root_evidence}
    atom_keys = {_identity(item) for item in atom_evidence}
    member_keys_by_atom = {
        atom_id: {_identity(item) for item in items}
        for atom_id, items in atom_candidates_by_atom
    }
    candidates: dict[tuple[object, ...], EvidenceItem] = {}
    sibling_keys_by_parent: dict[
        tuple[object, ...], set[tuple[object, ...]]
    ] = defaultdict(set)
    node_sibling_keys_by_parent: dict[
        tuple[object, ...], set[tuple[object, ...]]
    ] = defaultdict(set)
    for item in (*root_evidence, *atom_evidence):
        key = _identity(item)
        previous = candidates.get(key)
        if previous is None or (
            not dict(previous.metadata).get("evidence_group_id")
            and dict(item.metadata).get("evidence_group_id")
        ):
            candidates[key] = item
    # 文本或列表 Chunk 可能把两个相邻段落装在一起。原有 span 选择受每 Chunk
    # 配额约束；给已入选的段落补一个最近的独立来源节点，避免漏掉同块的时限。
    for item in (*root_evidence, *atom_evidence):
        item_key = _identity(item)
        candidate = candidate_by_id.get(item.chunk_id)
        if candidate is None or candidate.hydrated.chunk.role not in {
            ChunkRole.TEXT,
            ChunkRole.LIST,
        }:
            continue
        chunk = candidate.hydrated.chunk
        owned_nodes = {span.node_id for span in item.source_spans}
        alternatives: dict[str, tuple[SourceSpan, str]] = {}
        for span in chunk.source_spans:
            if (
                not span.is_citable
                or not span.node_id
                or span.node_id in owned_nodes
            ):
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ]
            if not quote.strip():
                continue
            previous_span = alternatives.get(span.node_id)
            if previous_span is None or len(quote) > len(previous_span[1]):
                alternatives[span.node_id] = (span, quote)
        if not alternatives:
            continue
        source_order = _source_order(item)
        sibling_span, sibling_quote = min(
            alternatives.values(),
            key=lambda pair: (
                abs(pair[0].source_anchor.ordinal - source_order)
                if (
                    pair[0].source_anchor is not None
                    and source_order is not None
                )
                else 2**31 - 1,
                -len(pair[1]),
                pair[0].chunk_start_char,
            ),
        )
        sibling = _evidence_item(
            candidate, sibling_span, sibling_quote, item.support_id
        )
        sibling_key = _identity(sibling)
        candidates.setdefault(sibling_key, sibling)
        sibling_keys_by_parent[item_key].add(sibling_key)
        if item_key in root_keys:
            root_keys.add(sibling_key)
        if item_key in atom_keys:
            atom_keys.add(sibling_key)
        for keys in member_keys_by_atom.values():
            if item_key in keys:
                keys.add(sibling_key)
    # 同一原文段落可能跨多个 canonical Chunk。沿 node_id 补齐有界片段，
    # 让原句的后半段进入生成包，同时保持每段独立的 SourceSpan 和引用。
    node_spans: dict[
        tuple[str, str], list[tuple[RankedChunk, SourceSpan, str]]
    ] = defaultdict(list)
    table_spans_by_node: dict[
        tuple[str, str], list[tuple[RankedChunk, SourceSpan, str]]
    ] = defaultdict(list)
    table_spans_by_chunk: dict[
        str, list[tuple[RankedChunk, SourceSpan, str]]
    ] = defaultdict(list)
    for candidate in candidate_by_id.values():
        chunk = candidate.hydrated.chunk
        if chunk.role is ChunkRole.TABLE:
            atoms = dict(chunk.metadata).get("atoms")
            if not isinstance(atoms, (list, tuple)) or not atoms:
                continue
            rows: set[int] = set()
            allowed_nodes: set[str] = set()
            valid_mapping = True
            for raw_atom in atoms:
                metadata = (
                    raw_atom.get("metadata")
                    if isinstance(raw_atom, dict)
                    else None
                )
                if not isinstance(metadata, dict):
                    valid_mapping = False
                    break
                row = metadata.get("row_index")
                mapping = metadata.get("cell_source_node_ids")
                if (
                    not isinstance(row, int)
                    or isinstance(row, bool)
                    or not isinstance(mapping, dict)
                ):
                    valid_mapping = False
                    break
                rows.add(row)
                for values in mapping.values():
                    if not isinstance(values, (list, tuple)):
                        valid_mapping = False
                        break
                    allowed_nodes.update(
                        node_id
                        for node_id in values
                        if isinstance(node_id, str)
                    )
                if not valid_mapping:
                    break
            # 只补同一个逻辑表格行中、由单元格节点映射明确列出的原文。
            if not valid_mapping or len(rows) != 1 or not allowed_nodes:
                continue
            for span in chunk.source_spans:
                if (
                    not span.is_citable
                    or span.is_repeated
                    or span.node_id not in allowed_nodes
                ):
                    continue
                quote = chunk.citation_text[
                    span.chunk_start_char : span.chunk_end_char
                ]
                if quote.strip():
                    table_spans_by_node[
                        (chunk.version.document_version_id, span.node_id)
                    ].append((candidate, span, quote))
                    table_spans_by_chunk[chunk.chunk_id].append(
                        (candidate, span, quote)
                    )
            continue
        if chunk.role not in {ChunkRole.TEXT, ChunkRole.LIST}:
            continue
        for span in chunk.source_spans:
            if not span.is_citable or span.is_repeated or not span.node_id:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ]
            if quote.strip():
                node_key = (chunk.version.document_version_id, span.node_id)
                node_spans[node_key].append((candidate, span, quote))
    for item in (*root_evidence, *atom_evidence):
        node_ids = {span.node_id for span in item.source_spans}
        if len(node_ids) != 1 or None in node_ids:
            continue
        item_key = _identity(item)
        node_id = next(iter(node_ids))
        if node_id is None:
            continue
        same_node = (
            (
                *table_spans_by_chunk.get(item.chunk_id, ()),
                *table_spans_by_node.get(
                    (item.document_version_id or "", node_id), ()
                ),
            )
            if item.table_context
            else node_spans.get((item.document_version_id or "", node_id), ())
        )
        for candidate, span, quote in sorted(
            same_node,
            key=lambda row: (
                row[1].source_anchor.ordinal
                if row[1].source_anchor is not None
                else 2**31,
                row[1].source_start_char or 0,
                row[0].fusion_rank,
            ),
        )[: policy.group_member_chunk_limit]:
            sibling = _evidence_item(candidate, span, quote, item.support_id)
            sibling_key = _identity(sibling)
            if sibling_key == item_key:
                continue
            candidates.setdefault(sibling_key, sibling)
            sibling_keys_by_parent[item_key].add(sibling_key)
            node_sibling_keys_by_parent[item_key].add(sibling_key)
            if item_key in root_keys:
                root_keys.add(sibling_key)
            if item_key in atom_keys:
                atom_keys.add(sibling_key)
            for keys in member_keys_by_atom.values():
                if item_key in keys:
                    keys.add(sibling_key)
    # 已获准文档中的目标行一旦由规范行名确认，直接从同一有界 canonical
    # 表结构补齐该行全部竞争字段和真实表头。字段选择稍后由 schema-aware
    # resolver 完成；这里不读取旧 relation_status，也不按最高分预选一列。
    inferred_tables = _inferred_header_tables(candidate_by_id)
    schema_rows: dict[
        tuple[tuple[object, ...], int],
        list[tuple[RankedChunk, EvidenceItem, int]],
    ] = defaultdict(list)
    schema_headers: dict[
        tuple[object, ...], list[tuple[RankedChunk, EvidenceItem]]
    ] = defaultdict(list)
    for candidate in candidate_by_id.values():
        chunk = candidate.hydrated.chunk
        if chunk.role is not ChunkRole.TABLE:
            continue
        for span in chunk.source_spans:
            if not span.is_citable or span.is_repeated:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if not quote:
                continue
            item = _evidence_item(candidate, span, quote, "S0")
            cell = _reading_table_identity(item)
            if cell is None:
                continue
            table, row, column = cell
            if _reading_header(item, candidate, inferred_tables):
                schema_headers[table].append((candidate, item))
            else:
                schema_rows[table, row].append((candidate, item, column))
    for (table, _row), row_items in sorted(schema_rows.items(), key=repr):
        labels = tuple(
            item
            for _candidate, item, column in row_items
            if column == _TABLE_ROW_LABEL_COLUMN
        )
        if not labels:
            continue
        label = " ".join(item.citation_text for item in labels)
        matched_atoms = tuple(
            atom
            for atom in query_plan.atoms
            if _reading_label_score(atom.target, label)[0]
        )
        if not matched_atoms and not named_table_label_in_query(
            query_plan.resolved_root_query, label
        ):
            continue
        value_columns = {
            column
            for _candidate, _item, column in row_items
            if column != _TABLE_ROW_LABEL_COLUMN
        }
        selected_schema_items = [
            (candidate, item) for candidate, item, _column in row_items
        ]
        selected_schema_items.extend(
            (candidate, item)
            for candidate, item in schema_headers.get(table, ())
            if _reading_header_columns(
                item, candidate, inferred_tables
            ) & value_columns
        )
        for _candidate, item in selected_schema_items:
            key = _identity(item)
            candidates.setdefault(key, item)
            root_keys.add(key)
            for atom in matched_atoms:
                atom_keys.add(key)
                member_keys_by_atom.setdefault(atom.atom_id, set()).add(key)

    # 表格行名和职责主体可能已进入 Rerank 池，却被旧证据装配配额丢弃。
    # 只从同一个有界池恢复逐字命中的原文单元格，再走统一硬边界与预算。
    exact_table_keys: list[tuple[object, ...]] = []
    # 流程问句中的零散字词不能当作表格职责主体；流程仍可经行名、
    # Root/Atom 命中及结构组进入证据包。
    duty_shapes = {
        AtomAnswerShape.DUTIES,
        AtomAnswerShape.ENUMERATION,
    }
    for ranked in ranked_candidates:
        chunk = ranked.hydrated.chunk
        if chunk.role is not ChunkRole.TABLE:
            continue
        for span in chunk.source_spans:
            if not span.is_citable or span.is_repeated:
                continue
            path = span.structural_path
            if not any(part.startswith("tbl:") for part in path):
                continue
            span_column = next(
                (
                    int(part[3:])
                    for part in path
                    if re.fullmatch(r"tc:\d+", part)
                ),
                None,
            )
            if span_column is None:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if not quote:
                continue
            named = span_column == 0 and named_table_label_in_query(
                query_plan.resolved_root_query, quote
            )
            subject_atoms = tuple(
                atom
                for atom in query_plan.atoms
                if span_column > 0
                and atom.answer_shape in duty_shapes
                and (
                    (
                        len(_normalized(atom.target))
                        >= _MIN_TABLE_SUBJECT_CHARS
                        and _normalized(atom.target) in _normalized(quote)
                    )
                    or _question_source_run(query_plan.original_query, quote)
                )
                and len(quote) >= _MIN_TABLE_ACTION_CHARS
            )
            if not named and not subject_atoms:
                continue
            item = _evidence_item(ranked, span, quote, "S0")
            key = _identity(item)
            candidates.setdefault(key, item)
            exact_table_keys.append(key)
            if named:
                root_keys.add(key)
            for atom in subject_atoms:
                atom_keys.add(key)
                member_keys_by_atom.setdefault(atom.atom_id, set()).add(key)
    # 新补入的表格原句也可能在 canonical Chunk 边界处截断；沿同一
    # SourceSpan 节点补后半段，不把同表相邻行当作续句。
    for parent_key in exact_table_keys:
        parent = candidates[parent_key]
        node_ids = {span.node_id for span in parent.source_spans}
        if len(node_ids) != 1 or None in node_ids:
            continue
        node_id = next(iter(node_ids))
        if node_id is None:
            continue
        continuation = sorted(
            table_spans_by_node.get(
                (parent.document_version_id or "", node_id), ()
            ),
            key=lambda row: (
                row[1].source_start_char or 0,
                row[0].fusion_rank,
            ),
        )[: policy.group_member_chunk_limit]
        for candidate, span, quote in continuation:
            sibling = _evidence_item(candidate, span, quote, "S0")
            sibling_key = _identity(sibling)
            if sibling_key == parent_key:
                continue
            candidates.setdefault(sibling_key, sibling)
            sibling_keys_by_parent[parent_key].add(sibling_key)
            node_sibling_keys_by_parent[parent_key].add(sibling_key)
            if parent_key in root_keys:
                root_keys.add(sibling_key)
            if parent_key in atom_keys:
                atom_keys.add(sibling_key)
            for keys in member_keys_by_atom.values():
                if parent_key in keys:
                    keys.add(sibling_key)
    # 旧证据装配器可能因软语义判断丢掉同文档的高排名正文。
    # 多子问题保留少量真实 Rerank 正文候选，仍由下面的硬边界和预算把关。
    supplemental_keys: list[tuple[object, ...]] = []
    if len(query_plan.atoms) > 1:
        ranked_top = sorted(
            (
                item
                for item in ranked_candidates
                if item.rerank_rank is not None
            ),
            key=lambda item: item.rerank_rank or 2**31,
        )[: policy.generation_max_ordinary_items]
        if ranked_top:
            primary_version = ranked_top[0].hydrated.chunk.version
            primary_source = (
                primary_version.document_id,
                primary_version.document_version_id,
            )
            anchored = any(
                (item.document_id, item.document_version_id) == primary_source
                for item in (*root_evidence, *atom_evidence)
            )
            if anchored:
                for candidate in ranked_top:
                    chunk = candidate.hydrated.chunk
                    if (
                        chunk.role is not ChunkRole.TEXT
                        or chunk.version != primary_version
                    ):
                        continue
                    quoted_spans = (
                        (
                            span,
                            chunk.citation_text[
                                span.chunk_start_char : span.chunk_end_char
                            ].strip(),
                        )
                        for span in chunk.source_spans
                        if span.is_citable and not span.is_repeated
                    )
                    best = max(
                        (
                            (span, quote)
                            for span, quote in quoted_spans
                            if quote
                        ),
                        key=lambda pair: len(pair[1]),
                        default=None,
                    )
                    if best is None:
                        continue
                    item = _evidence_item(candidate, best[0], best[1], "S0")
                    key = _identity(item)
                    candidates.setdefault(key, item)
                    root_keys.add(key)
                    supplemental_keys.append(key)
    linked_ids_by_chunk: dict[str, set[str]] = defaultdict(set)
    root_chunk_ids: set[str] = set()
    for link in links:
        if link.atom_id is None:
            root_chunk_ids.add(link.chunk_id)
        else:
            linked_ids_by_chunk[link.chunk_id].add(link.atom_id)
    # 最终有界池中的真实 SourceSpan 具有阅读资格；旧证书缺失只影响
    # 证明与完整性。后面的 ACL、活动版本、显式来源和结构冲突仍硬过滤。
    for candidate in ranked_candidates:
        if candidate.rerank_rank is None and candidate.expansion_reason is None:
            continue
        chunk = candidate.hydrated.chunk
        for span in chunk.source_spans:
            if not span.is_citable or span.is_repeated:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if not quote:
                continue
            item = _evidence_item(candidate, span, quote, "S0")
            key = _identity(item)
            candidates.setdefault(key, item)
            linked_atoms = linked_ids_by_chunk.get(chunk.chunk_id, set())
            if chunk.chunk_id in root_chunk_ids or not linked_atoms:
                root_keys.add(key)
            for atom_id in linked_atoms:
                atom_keys.add(key)
                member_keys_by_atom.setdefault(atom_id, set()).add(key)
    atoms_by_id = {atom.atom_id: atom for atom in query_plan.atoms}
    excluded = frozenset(excluded_document_ids)

    def reasons_for(item: EvidenceItem) -> tuple[EvidenceAdmissionReason, ...]:
        linked = linked_ids_by_chunk.get(item.chunk_id, set())
        possible = tuple(
            atoms_by_id[atom_id]
            for atom_id in sorted(linked)
            if atom_id in atoms_by_id
        )
        if _identity(item) in root_keys or not possible:
            possible = query_plan.atoms
        return _hard_reasons(
            item,
            candidate=candidate_by_id.get(item.chunk_id),
            groups_by_id=groups_by_id,
            possible_atoms=possible,
            request=request,
            active_revision_id=active_revision_id,
            excluded_document_ids=excluded,
        )

    ordered = sorted(candidates, key=lambda key: _rank(candidates[key]))
    admitted = [key for key in ordered if not reasons_for(candidates[key])]
    chosen: list[tuple[object, ...]] = []
    ordinary_counts: Counter[str] = Counter()
    ordinary_tokens = 0
    priority_keys: list[tuple[object, ...]] = []
    priority_tokens = 0
    reading_unit_reasons: list[str] = []
    selected_priority_units: list[
        tuple[str, tuple[tuple[object, ...], ...]]
    ] = []

    def add_ordinary(key: tuple[object, ...]) -> None:
        nonlocal ordinary_tokens
        if (
            key in chosen
            or key in priority_keys
            or len(chosen) >= policy.generation_max_ordinary_items
        ):
            return
        item = candidates[key]
        document_id = item.document_id or ""
        if ordinary_counts[document_id] >= policy.generation_per_document_cap:
            return
        cost = max(1, (len(item.citation_text) + 3) // 4)
        if (
            ordinary_tokens + priority_tokens + cost
            > policy.generation_evidence_token_budget
        ):
            return
        chosen.append(key)
        ordinary_counts[document_id] += 1
        ordinary_tokens += cost

    for owner, unit in _priority_reading_units(
        query_plan=query_plan,
        items=tuple(candidates[key] for key in admitted),
        candidate_by_id=candidate_by_id,
        root_evidence=root_evidence,
        atom_candidates_by_atom=atom_candidates_by_atom,
        policy=policy,
        diagnostics=reading_unit_reasons,
    ):
        cells = {
            cell
            for key in unit
            if (cell := _reading_table_identity(candidates[key])) is not None
        }
        if len(cells) < _MIN_READING_RELATION_CELLS or not any(
            cell[2] == 0 for cell in cells
        ):
            # 普通正文和同节点续片沿用既有配额顺序；这里只固定最终
            # 全部入选时的发送保护，不抢占已认证列表组的保留名额。
            selected_priority_units.append((owner, unit))
            continue
        new_keys = tuple(
            key
            for key in unit
            if key not in chosen and key not in priority_keys
        )
        cost = sum(
            max(1, (len(candidates[key].citation_text) + 3) // 4)
            for key in new_keys
        )
        if (
            len(priority_keys) + max(0, len(new_keys) - 1)
            > policy.generation_max_group_items
            or ordinary_tokens + priority_tokens + cost
            > policy.generation_evidence_token_budget
        ):
            reading_unit_reasons.append("TABLE_RELATION_UNIT_BUDGET_EXCEEDED")
            continue
        if new_keys:
            add_ordinary(new_keys[0])
            if new_keys[0] not in chosen:
                continue
            for key in new_keys[1:]:
                priority_keys.append(key)
                priority_tokens += max(
                    1, (len(candidates[key].citation_text) + 3) // 4
                )
        selected_priority_units.append((owner, unit))

    for key in dict.fromkeys(exact_table_keys):
        if key in admitted:
            add_ordinary(key)

    if len(query_plan.atoms) > 1:
        first_seed = min(
            (
                candidate
                for candidate in ranked_candidates
                if candidate.rerank_rank is not None
            ),
            key=lambda candidate: candidate.rerank_rank or 2**31,
            default=None,
        )
        if first_seed is not None:
            primary_document_id = first_seed.hydrated.chunk.version.document_id
            predecessor_top = (
                key
                for key in admitted
                if key in root_keys
                and candidates[key].document_id == primary_document_id
                and (candidate := candidate_by_id.get(candidates[key].chunk_id))
                is not None
                and candidate.expansion_reason == "SECTION_PREDECESSOR"
            )
            # 多子问题保留同一来源中紧邻的前序阶段，后续仍受每文档、
            # 总条数和 token 上限约束；只有已经过硬边界检查的来源可入包。
            reserved_chunks: set[str] = set()
            for key in predecessor_top:
                chunk_id = candidates[key].chunk_id
                if chunk_id in reserved_chunks:
                    continue
                add_ordinary(key)
                if key in chosen:
                    reserved_chunks.add(chunk_id)
                if len(reserved_chunks) == _MAX_RESERVED_PREDECESSOR_CHUNKS:
                    break
    # 前序阶段保留之后，给同一活动文档的其他高排名正文最多一半名额；
    # 另一半仍留给原有 Root/Atom，防止某一个来源挤掉其余问答证据。
    if supplemental_keys:
        primary_document_id = candidates[supplemental_keys[0]].document_id or ""
        reserve_limit = min(
            policy.generation_per_document_cap // 2,
            max(
                0,
                policy.generation_per_document_cap
                - ordinary_counts[primary_document_id]
                - 2,
            ),
        )
        reserved = 0
        for key in supplemental_keys:
            if reserved >= reserve_limit:
                break
            if key not in admitted:
                continue
            add_ordinary(key)
            if key in chosen:
                reserved += 1
    root_top = [key for key in admitted if key in root_keys]
    for key in root_top[: policy.generation_root_top_k]:
        add_ordinary(key)
        if key in chosen:
            for sibling_key in sorted(
                sibling_keys_by_parent.get(key, ()),
                key=lambda candidate_key: _rank(candidates[candidate_key]),
            ):
                if sibling_key in admitted:
                    add_ordinary(sibling_key)
    for atom in query_plan.atoms:
        member_keys = member_keys_by_atom.get(atom.atom_id, set())
        linked = [
            key
            for key in admitted
            if key in atom_keys
            and (
                key in member_keys
                or atom.atom_id
                in linked_ids_by_chunk.get(candidates[key].chunk_id, set())
            )
            and _source_matches(atom, candidates[key])
        ]
        for key in linked[: policy.generation_atom_top_k]:
            add_ordinary(key)
            if key in chosen:
                for sibling_key in sorted(
                    sibling_keys_by_parent.get(key, ()),
                    key=lambda candidate_key: _rank(candidates[candidate_key]),
                ):
                    if sibling_key in admitted:
                        add_ordinary(sibling_key)
    for key in admitted:
        add_ordinary(key)

    # 已选中的完整结构组按 SourceSpan 补齐；组预算与总预算保持有界。
    selected_chunk_ids = {candidates[key].chunk_id for key in chosen}
    group_keys: list[tuple[object, ...]] = list(priority_keys)
    group_tokens = priority_tokens
    complete_group_ids: list[str] = []
    for group in groups:
        if not group.complete or not selected_chunk_ids.intersection(
            group.group.member_chunk_ids
        ):
            continue
        items = _group_items(group)
        if not items:
            continue
        for item in items:
            key = _identity(item)
            if key not in candidates:
                candidates[key] = item
                ordered.append(key)
        if any(reasons_for(item) for item in items):
            continue
        new_items = tuple(
            item
            for item in items
            if _identity(item) not in chosen
            and _identity(item) not in group_keys
        )
        added_tokens = sum(
            max(1, (len(item.citation_text) + 3) // 4) for item in new_items
        )
        if (
            len(group_keys) + len(new_items) > policy.generation_max_group_items
            or ordinary_tokens + group_tokens + added_tokens
            > policy.generation_evidence_token_budget
        ):
            continue
        for item in items:
            key = _identity(item)
            if key not in candidates or not dict(candidates[key].metadata).get(
                "evidence_group_id"
            ):
                candidates[key] = item
            if key not in chosen and key not in group_keys:
                group_keys.append(key)
        group_tokens += added_tokens
        covered = tuple(candidates[key] for key in (*chosen, *group_keys))
        if group_source_maps_covered(group, covered):
            complete_group_ids.append(group.group_id)
    # 完整结构组先占有界预算，再补同一原文节点的后半段。
    # 否则无关的续片会挤掉已经命中的表格行或列表成员。
    for parent_key in chosen:
        for key in sorted(
            node_sibling_keys_by_parent.get(parent_key, ()),
            key=lambda sibling_key: _rank(candidates[sibling_key]),
        ):
            if key in chosen or key in group_keys:
                continue
            item = candidates[key]
            cost = max(1, (len(item.citation_text) + 3) // 4)
            if (
                reasons_for(item)
                or len(group_keys) >= policy.generation_max_group_items
                or ordinary_tokens + group_tokens + cost
                > policy.generation_evidence_token_budget
            ):
                continue
            group_keys.append(key)
            group_tokens += cost
    selected = tuple(dict.fromkeys((*chosen, *group_keys)))
    complete = frozenset(complete_group_ids)
    selected_groups = {
        group_id
        for key in selected
        if isinstance(
            group_id := dict(candidates[key].metadata).get("evidence_group_id"),
            str,
        )
    }
    partial_group_ids = tuple(sorted(selected_groups - complete))

    def entry(
        key: tuple[object, ...], support_id: str, *, rejected: bool = False
    ) -> GenerationEvidenceEntry:
        item = candidates[key].model_copy(update={"evidence_id": support_id})
        metadata = dict(item.metadata)
        group_id = metadata.get("evidence_group_id")
        group_id = group_id if isinstance(group_id, str) else None
        linked = tuple(sorted(linked_ids_by_chunk.get(item.chunk_id, set())))
        soft = [EvidenceAdmissionReason.ACTIVE_CITABLE]
        if key in root_keys or item.chunk_id in root_chunk_ids:
            soft.append(EvidenceAdmissionReason.ROOT_RETRIEVAL)
        if key in atom_keys or linked:
            soft.append(EvidenceAdmissionReason.ATOM_RETRIEVAL)
        if item.rerank_rank is not None:
            soft.append(EvidenceAdmissionReason.RERANK_TOP)
        if group_id is not None:
            soft.append(
                EvidenceAdmissionReason.COMPLETE_GROUP
                if group_id in complete
                else EvidenceAdmissionReason.PARTIAL_GROUP
            )
        certificate = metadata.get("answer_support")
        if isinstance(certificate, dict):
            if certificate.get("status") != "SUPPORTED":
                soft.append(EvidenceAdmissionReason.RELATION_SOFT_MISMATCH)
            if not any(
                _normalized(atom.target) in _normalized(item.citation_text)
                for atom in query_plan.atoms
            ):
                soft.append(EvidenceAdmissionReason.TARGET_SOFT_MISMATCH)
        table_row_index = _table_row_index(item)
        return GenerationEvidenceEntry(
            support_id=support_id,
            evidence_item=item,
            source_group_id=group_id,
            linked_atom_ids=linked,
            admission_status=(
                EvidenceAdmissionStatus.REJECTED_HARD
                if rejected
                else EvidenceAdmissionStatus.ADMITTED_STRUCTURED_PARTIAL
                if group_id in partial_group_ids
                and metadata.get("evidence_group_type")
                in _STRUCTURED_GROUP_TYPES
                else EvidenceAdmissionStatus.ADMITTED
            ),
            hard_reject_reasons=reasons_for(item) if rejected else (),
            soft_signals=tuple(dict.fromkeys(soft)),
            rerank_rank=item.rerank_rank,
            source_order=_source_order(item),
            table_node_id=_table_node_id(
                candidate_by_id.get(item.chunk_id), table_row_index
            ),
            table_group_id=item.table_locator,
            table_row_index=table_row_index,
        )

    entries = tuple(
        entry(key, f"S{index}") for index, key in enumerate(selected, 1)
    )
    rejected_entries = tuple(
        entry(key, f"R{index}", rejected=True)
        for index, key in enumerate(ordered, 1)
        if reasons_for(candidates[key])
    )
    root_ids = {
        item.support_id
        for item in entries
        if _identity(item.evidence_item) in root_keys
    }
    root_group_ids = {
        group.group_id
        for group in groups
        if any(
            candidates[key].chunk_id in group.group.member_chunk_ids
            for key in selected
            if key in root_keys
        )
    }
    per_atom: list[tuple[str, tuple[str, ...]]] = []
    missing: list[str] = []
    atom_group_ids: dict[str, set[str]] = defaultdict(set)
    for atom_id, items in atom_candidates_by_atom:
        atom_group_ids[atom_id].update(
            group_id
            for item in items
            if isinstance(
                group_id := dict(item.metadata).get("evidence_group_id"),
                str,
            )
        )
    for item in atom_evidence:
        group_id = dict(item.metadata).get("evidence_group_id")
        if isinstance(group_id, str):
            for atom_id in linked_ids_by_chunk.get(item.chunk_id, set()):
                atom_group_ids[atom_id].add(group_id)
    for atom in query_plan.atoms:
        atom_group_ids[atom.atom_id].update(
            alignment.group_id
            for alignment in align_atom_to_groups(atom, groups, links, policy)
            if alignment.qualification is not AlignmentQualification.REJECTED
        )
    for atom in query_plan.atoms:
        member_keys = member_keys_by_atom.get(atom.atom_id, set())
        # Root Top 可供任一 Atom 选择；有明确来源限定时仍须满足该来源。
        ids = tuple(
            item.support_id
            for item in entries
            if _source_matches(atom, item.evidence_item)
            and (
                item.support_id in root_ids
                or item.source_group_id in root_group_ids
                or _identity(item.evidence_item) in member_keys
                or atom.atom_id in item.linked_atom_ids
                or (
                    item.source_group_id is not None
                    and item.source_group_id in atom_group_ids[atom.atom_id]
                )
            )
        )
        per_atom.append((atom.atom_id, ids))
        if not ids:
            missing.append(atom.atom_id)
    priority_source_units = tuple(
        (owner, tuple(stable_support_key(candidates[key]) for key in unit))
        for owner, unit in selected_priority_units
        if set(unit) <= set(selected)
    )
    physical_table_facts = _physical_table_facts(
        tuple(item.evidence_item for item in entries), candidate_by_id
    )
    atom_fact_bindings = _atom_fact_bindings(
        query_plan,
        physical_table_facts,
        tuple(item.evidence_item for item in entries),
        tuple(per_atom),
        priority_source_units,
    )
    return GenerationEvidencePack(
        original_query=query_plan.original_query,
        resolved_root_query=query_plan.resolved_root_query,
        entries=entries,
        rejected_entries=rejected_entries,
        per_atom_candidate_support_ids=tuple(per_atom),
        complete_group_ids=tuple(complete_group_ids),
        partial_group_ids=partial_group_ids,
        missing_atom_ids=tuple(missing),
        trusted_source_groups=tuple(
            group.group for group in groups if group.group_id in selected_groups
        ),
        per_atom_source_certificates=tuple(
            (atom_id, stable_support_key(item), freeze_json_object(certificate))
            for atom_id, items in atom_candidates_by_atom
            for item in items
            if _identity(item) in selected
            and isinstance(
                certificate := dict(item.metadata).get("answer_support"), dict
            )
        ),
        priority_source_units=priority_source_units,
        reading_unit_reason_codes=tuple(dict.fromkeys(reading_unit_reasons)),
        physical_table_facts=physical_table_facts,
        atom_fact_bindings=atom_fact_bindings,
    )


__all__ = [
    "GENERATION_EVIDENCE_PACK_REVISION",
    "EvidenceAdmissionReason",
    "EvidenceAdmissionStatus",
    "GenerationEvidenceEntry",
    "GenerationEvidencePack",
    "build_generation_evidence_pack",
]
