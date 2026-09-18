"""把可引用检索结果组成有界生成证据包，保留发布前独立核验。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum

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
from rag_app.core.models import (
    ChannelHit,
    ChunkRole,
    EvidenceItem,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
    SourceSpan,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    AtomStatus,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
)
from rag_app.core.query_text import named_table_label_in_query

GENERATION_EVIDENCE_PACK_REVISION = "wb08r-generation-evidence-v6"
_MAX_RESERVED_PREDECESSOR_CHUNKS = 2
_STRUCTURED_GROUP_TYPES = frozenset(
    {"LIST_GROUP", "PROCEDURE_GROUP", "SECTION_GROUP", "TABLE_ROW_GROUP"}
)
_TABLE_ROW = re.compile(r"^tr:(\d+)$")
_TABLE_NODE_ID = re.compile(r"^node_[0-9a-f]{32}$")
_MIN_TABLE_SUBJECT_CHARS = 3
_MIN_TABLE_ACTION_CHARS = 12
_MIN_QUESTION_SOURCE_RUN = 4
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


def _normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _question_source_run(query: str, source: str) -> bool:
    """要求原问与原文单元格有连续对象锚点，避免泛主题补证。"""
    words = "".join(char for char in query if "\u4e00" <= char <= "\u9fff")
    normalized_source = _normalized(source)
    return any(
        words[index : index + _MIN_QUESTION_SOURCE_RUN]
        in normalized_source
        for index in range(len(words) - _MIN_QUESTION_SOURCE_RUN + 1)
    )


def _identity(item: EvidenceItem) -> tuple[object, ...]:
    """一个来源片段跨 Root/Atom/Group 的稳定去重键。"""
    return (
        item.document_version_id,
        item.chunk_id,
        item.citation_text,
        tuple(
            (span.node_id, span.source_start_char, span.source_end_char)
            for span in item.source_spans
        ),
    )


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
    if not atom.source_qualifier:
        return True
    metadata = dict(item.metadata)
    identity = " ".join(
        (item.display_name or "", str(metadata.get("document_title", "")))
    )
    return _normalized(atom.source_qualifier) in _normalized(identity)


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
    rows = set()
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
        atom.source_qualifier and not _source_matches(atom, item)
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
            previous = alternatives.get(span.node_id)
            if previous is None or len(quote) > len(previous[1]):
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
            for atom in atoms:
                metadata = (
                    atom.get("metadata") if isinstance(atom, dict) else None
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
            column = next(
                (
                    int(part[3:])
                    for part in path
                    if re.fullmatch(r"tc:\d+", part)
                ),
                None,
            )
            if column is None:
                continue
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if not quote:
                continue
            named = column == 0 and named_table_label_in_query(
                query_plan.resolved_root_query, quote
            )
            subject_atoms = tuple(
                atom
                for atom in query_plan.atoms
                if column > 0
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

    def add_ordinary(key: tuple[object, ...]) -> None:
        nonlocal ordinary_tokens
        if key in chosen or len(chosen) >= policy.generation_max_ordinary_items:
            return
        item = candidates[key]
        document_id = item.document_id or ""
        if ordinary_counts[document_id] >= policy.generation_per_document_cap:
            return
        cost = max(1, (len(item.citation_text) + 3) // 4)
        if ordinary_tokens + cost > policy.generation_evidence_token_budget:
            return
        chosen.append(key)
        ordinary_counts[document_id] += 1
        ordinary_tokens += cost

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
    group_keys: list[tuple[object, ...]] = []
    group_tokens = 0
    complete_group_ids: list[str] = []
    # 同一原文节点的后半段优先于无关结构组使用额外预算；普通证据的
    # 每文档配额不应截断已命中段落，但总条数与 token 上限仍生效。
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
    return GenerationEvidencePack(
        original_query=query_plan.original_query,
        resolved_root_query=query_plan.resolved_root_query,
        entries=entries,
        rejected_entries=rejected_entries,
        per_atom_candidate_support_ids=tuple(per_atom),
        complete_group_ids=tuple(complete_group_ids),
        partial_group_ids=partial_group_ids,
        missing_atom_ids=tuple(missing),
    )


__all__ = [
    "GENERATION_EVIDENCE_PACK_REVISION",
    "EvidenceAdmissionReason",
    "EvidenceAdmissionStatus",
    "GenerationEvidenceEntry",
    "GenerationEvidencePack",
    "build_generation_evidence_pack",
]
