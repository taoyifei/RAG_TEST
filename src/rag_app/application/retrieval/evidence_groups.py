"""从 canonical Chunk 构造有界结构组并按组原子装包。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from rag_app.core.identifiers import canonical_json
from rag_app.core.models import (
    ChunkRole,
    EvidenceGroup,
    EvidenceGroupKind,
    GroupSourceMap,
    RankedChunk,
    SourceSpan,
)
from rag_app.core.models.common import freeze_json_object

if TYPE_CHECKING:
    from rag_app.core.ports.evidence_source import CatalogDocument

_PROCEDURE_HEADING = re.compile(r"流程|步骤|程序|办理|操作")
_TABLE_ROW = re.compile(r"^tr:(\d+)$")
_TABLE_COLUMN = re.compile(r"^tc:(\d+)$")
_INFERRED_HEADER_CELL_CHAR_LIMIT = 16
_MIN_HEADER_COLUMNS = 2
_SECTION_MEMBER_CHUNK_LIMIT = 3
_GROUP_SCHEMA_REVISION = "wb08r-evidence-group-v1"


@dataclass(frozen=True, slots=True)
class GroupCandidate:
    """应用层组候选；保留原 RankedChunk 供现有 Evidence 链使用。"""

    group: EvidenceGroup
    members: tuple[RankedChunk, ...]
    rerank_text: str

    @property
    def group_id(self) -> str:
        """返回稳定组身份。

        Args:
            无参数；读取当前候选组。

        Returns:
            当前组的稳定身份。

        """
        return self.group.group_id

    @property
    def complete(self) -> bool:
        """返回结构闭合状态。

        Args:
            无参数；读取当前候选组。

        Returns:
            当前组是否闭合。

        """
        return self.group.complete

    @property
    def token_cost(self) -> int:
        """返回保守的组装包开销。

        Args:
            无参数；读取当前候选组。

        Returns:
            当前组的估算 token 开销。

        """
        return self.group.token_cost


@dataclass(frozen=True, slots=True)
class GroupPackingOutcome:
    """原子装包结果与未选组的确定性原因。"""

    selected: tuple[GroupCandidate, ...]
    rejected: tuple[tuple[str, str], ...]
    incomplete: tuple[GroupCandidate, ...] = ()


def build_evidence_groups(
    candidates: tuple[RankedChunk, ...],
    *,
    max_groups: int,
    max_member_chunks: int,
    rerank_text_char_limit: int,
) -> tuple[GroupCandidate, ...]:
    """按版本、章节、结构组和来源顺序构建通用证据组。

    Args:
        candidates: 有界 hydration 和邻居扩展后的 canonical 候选。
        max_groups: 本轮最多构造的组数。
        max_member_chunks: 单组最多保留的 Chunk 数。
        rerank_text_char_limit: 组重排文本字符上限。

    Returns:
        保留原文映射、结构坐标与缺失原因的候选组。

    Raises:
        ValueError: 预算无效或同一 Chunk ID 映射不一致。

    """
    if min(max_groups, max_member_chunks, rerank_text_char_limit) <= 0:
        raise ValueError("EvidenceGroup 预算必须为正数。")
    by_id: dict[str, RankedChunk] = {}
    for candidate in candidates:
        chunk_id = candidate.hydrated.chunk.chunk_id
        prior = by_id.get(chunk_id)
        if prior is not None and prior.hydrated != candidate.hydrated:
            raise ValueError("同一 Chunk ID 对应不同 canonical 来源。")
        by_id.setdefault(chunk_id, candidate)
    bounded = tuple(by_id.values())
    # 输入本身仍有硬上限；丢弃候选时不得宣称组结构完整。
    input_cap = max_groups * max_member_chunks * 2
    input_truncated = len(bounded) > input_cap
    bounded = bounded[:input_cap]
    partitions: dict[tuple[str, str, str, str, str], list[RankedChunk]] = (
        defaultdict(list)
    )
    for candidate in bounded:
        chunk = candidate.hydrated.chunk
        key = (
            chunk.version.document_id,
            chunk.version.document_version_id,
            chunk.section_id,
            chunk.neighbor_group_id,
            chunk.role.value,
        )
        partitions[key].append(candidate)
    ordered = tuple(sorted(bounded, key=_source_order))
    groups: list[GroupCandidate] = []
    for partition in partitions.values():
        first = partition[0].hydrated.chunk
        if first.role is ChunkRole.TABLE:
            proposed = _table_groups(
                partition,
                max_member_chunks=max_member_chunks,
                rerank_text_char_limit=rerank_text_char_limit,
                input_truncated=input_truncated,
            )
        elif first.role is ChunkRole.LIST:
            proposed = (
                _list_group(
                    partition,
                    ordered,
                    max_member_chunks=max_member_chunks,
                    rerank_text_char_limit=rerank_text_char_limit,
                    input_truncated=input_truncated,
                ),
            )
        else:
            sorted_members = tuple(sorted(partition, key=_source_order))
            section_complete = (
                first.role is ChunkRole.TEXT
                and len(sorted_members) > 1
                and len(sorted_members)
                <= min(max_member_chunks, _SECTION_MEMBER_CHUNK_LIMIT)
                and not _chain_reasons(sorted_members)
            )
            if section_complete:
                proposed = (
                    _group_candidate(
                        EvidenceGroupKind.SECTION_GROUP,
                        sorted_members,
                        group_key=(first.neighbor_group_id,),
                        reasons=(),
                        rerank_text_char_limit=rerank_text_char_limit,
                    ),
                )
            else:
                # 不完整章节仍可提供独立段落，不能标成完整章节证据。
                proposed = tuple(
                    _group_candidate(
                        EvidenceGroupKind.PARAGRAPH_GROUP,
                        (member,),
                        group_key=(
                            first.neighbor_group_id,
                            member.hydrated.chunk.chunk_id,
                        ),
                        reasons=(),
                        rerank_text_char_limit=rerank_text_char_limit,
                    )
                    for member in sorted_members
                )
        groups.extend(proposed)
    if len(groups) <= max_groups:
        return tuple(groups)
    # 单个表格可拆出许多行组；先构建所有有界输入的组，再按成员排名
    # 裁剪，避免表格占满构建名额而吞掉后面的同章节列表阶段。
    return rank_evidence_groups(tuple(groups))[:max_groups]


def pack_evidence_groups(
    groups: tuple[GroupCandidate, ...],
    *,
    token_budget: int,
    max_groups: int,
    max_chunks: int,
) -> tuple[GroupCandidate, ...]:
    """按给定重排顺序原子选择完整组。

    Args:
        groups: 已按成员重排信号排序的候选组。
        token_budget: 本轮组证据 token 上限。
        max_groups: 最多选择的组数。
        max_chunks: 最多选择的 Chunk 数。

    Returns:
        在三个预算内完整装入的组。

    """
    return pack_evidence_groups_with_diagnostics(
        groups,
        token_budget=token_budget,
        max_groups=max_groups,
        max_chunks=max_chunks,
    ).selected


def rank_evidence_groups(
    groups: tuple[GroupCandidate, ...],
) -> tuple[GroupCandidate, ...]:
    """沿用成员的一次 Chunk 重排结果，稳定排列完整结构组。

    Args:
        groups: 原始候选组。

    Returns:
        优先完整组且按成员信号稳定排序的候选组。

    """
    return tuple(
        sorted(
            groups,
            key=lambda candidate: (
                not candidate.complete,
                min(
                    member.rerank_rank or member.fusion_rank
                    for member in candidate.members
                )
                if candidate.members
                else 2**31,
                -max(
                    member.rerank_score
                    if member.rerank_score is not None
                    else float("-inf")
                    for member in candidate.members
                )
                if candidate.members
                else float("inf"),
                -len(candidate.members),
                candidate.group_id,
            ),
        )
    )


def pack_evidence_groups_with_diagnostics(  # noqa: PLR0913
    groups: tuple[GroupCandidate, ...],
    *,
    token_budget: int,
    max_groups: int,
    max_chunks: int,
    per_document_cap: int | None = None,
    per_section_cap: int | None = None,
) -> GroupPackingOutcome:
    """记录结构缺失或预算不足，绝不打散组成员。

    Args:
        groups: 已排序的候选组。
        token_budget: 本轮组证据 token 上限。
        max_groups: 最多选择的组数。
        max_chunks: 最多选择的 Chunk 数。
        per_document_cap: 单文档最多选择的组数；省略时不设额外上限。
        per_section_cap: 单章节最多选择的组数；省略时不设额外上限。

    Returns:
        完整选中组及未选组的确定性原因。

    Raises:
        ValueError: 任一预算不是正数。

    """
    limits = (token_budget, max_groups, max_chunks)
    if per_document_cap is not None:
        limits = (*limits, per_document_cap)
    if per_section_cap is not None:
        limits = (*limits, per_section_cap)
    if min(limits) <= 0:
        raise ValueError("EvidenceGroup 打包预算必须为正数。")
    selected: list[GroupCandidate] = []
    rejected: list[tuple[str, str]] = []
    incomplete: list[GroupCandidate] = []
    seen_chunks: set[str] = set()
    documents: Counter[str] = Counter()
    sections: Counter[tuple[str, str]] = Counter()
    remaining = token_budget
    for candidate in groups:
        group = candidate.group
        member_ids = set(group.member_chunk_ids)
        if not group.complete:
            rejected.append((group.group_id, "INCOMPLETE_STRUCTURE"))
            incomplete.append(candidate)
            continue
        if len(selected) >= max_groups:
            rejected.append((group.group_id, "GROUP_LIMIT"))
            continue
        if member_ids <= seen_chunks:
            rejected.append((group.group_id, "GROUP_DUPLICATE"))
            continue
        if (
            per_document_cap is not None
            and documents[group.document_id] >= per_document_cap
        ):
            rejected.append((group.group_id, "GROUP_DOCUMENT_CAP"))
            continue
        section_key = (group.document_id, group.section_id)
        if (
            per_section_cap is not None
            and sections[section_key] >= per_section_cap
        ):
            rejected.append((group.group_id, "GROUP_SECTION_CAP"))
            continue
        if len(seen_chunks | member_ids) > max_chunks:
            rejected.append((group.group_id, "CHUNK_LIMIT"))
            continue
        if group.token_cost > remaining:
            rejected.append((group.group_id, "GROUP_EXCEEDS_BUDGET"))
            incomplete.append(
                GroupCandidate(
                    group=group.model_copy(
                        update={
                            "complete": False,
                            "incomplete_reasons": ("GROUP_EXCEEDS_BUDGET",),
                        }
                    ),
                    members=candidate.members,
                    rerank_text=candidate.rerank_text,
                )
            )
            continue
        selected.append(candidate)
        seen_chunks.update(member_ids)
        documents[group.document_id] += 1
        sections[section_key] += 1
        remaining -= group.token_cost
    return GroupPackingOutcome(
        tuple(selected), tuple(rejected), tuple(incomplete)
    )


def build_catalog_evidence_group(
    document: CatalogDocument, *, index_revision_id: str
) -> GroupCandidate:
    """目录组仅表示版本中存在的标题、分类和可参考对象。

    Args:
        document: 已通过权限过滤的可信目录项。
        index_revision_id: 当前活动索引版本身份。

    Returns:
        不包含文档正文或模板字段的目录组。

    """
    metadata = dict(document.metadata)
    raw_category = metadata.get("category_path")
    category = (
        tuple(part for part in raw_category if isinstance(part, str))
        if isinstance(raw_category, (list, tuple))
        else ()
    )
    rerank_text = "\n".join(
        (
            f"目录标题：{document.title}",
            f"分类：{' > '.join(category)}" if category else "分类：未标注",
            f"可参考对象：{document.title}",
        )
    )
    group = EvidenceGroup(
        group_id=_group_id(
            document.document_version_id,
            index_revision_id,
            "catalog",
            document.chunk_id,
        ),
        kind=EvidenceGroupKind.CATALOG_ENTRY,
        document_id=document.document_id,
        document_version_id=document.document_version_id,
        index_revision_id=index_revision_id,
        section_id="catalog",
        display_name=document.title,
        group_text_for_model=rerank_text,
        group_text_for_rerank=rerank_text,
        complete=True,
        token_cost=max(1, len(rerank_text)),
        catalog_title=document.title,
        category_path=category,
        reference_object=document.title,
        metadata=freeze_json_object(metadata),
    )
    return GroupCandidate(group, (), rerank_text)


def _list_group(
    partition: list[RankedChunk],
    candidates: tuple[RankedChunk, ...],
    *,
    max_member_chunks: int,
    rerank_text_char_limit: int,
    input_truncated: bool,
) -> GroupCandidate:
    ordered = tuple(sorted(partition, key=_source_order))
    first = ordered[0].hydrated.chunk
    first_ordinal = _first_ordinal(ordered[0])
    intros = tuple(
        item
        for item in candidates
        if item.hydrated.chunk.role is ChunkRole.TEXT
        and item.hydrated.chunk.version == first.version
        and item.hydrated.chunk.section_id == first.section_id
        and first_ordinal is not None
        and _last_ordinal(item) == first_ordinal - 1
    )
    intro = intros[0] if len(intros) == 1 else None
    full_members = ((intro,) if intro is not None else ()) + ordered
    members = full_members[:max_member_chunks]
    reasons = list(_chain_reasons(ordered))
    # 章节标题是 canonical 结构中的真实导语；没有独立导语段落时，
    # 仅在列表链确已到达章节起点后使用标题，不补写任何正文。
    heading_intro = bool(first.heading_path) and first.previous_chunk_id is None
    if intro is None and not heading_intro:
        reasons.append("MISSING_LIST_INTRO")
    if len(full_members) > max_member_chunks:
        reasons.append("MEMBER_LIMIT")
    if input_truncated:
        reasons.append("INPUT_LIMIT")
    kind = (
        EvidenceGroupKind.PROCEDURE_GROUP
        if any(_PROCEDURE_HEADING.search(part) for part in first.heading_path)
        else EvidenceGroupKind.LIST_GROUP
    )
    return _group_candidate(
        kind,
        members,
        group_key=(first.neighbor_group_id,),
        reasons=tuple(dict.fromkeys(reasons)),
        rerank_text_char_limit=rerank_text_char_limit,
    )


def _table_groups(
    partition: list[RankedChunk],
    *,
    max_member_chunks: int,
    rerank_text_char_limit: int,
    input_truncated: bool,
) -> tuple[GroupCandidate, ...]:
    rows: dict[int, list[RankedChunk]] = defaultdict(list)
    header_rows: set[int] = set()
    for member in partition:
        for row in _row_indexes(member):
            rows[row].append(member)
        header_rows.update(_table_header_rows(member))
    if not rows:
        first = partition[0].hydrated.chunk
        return (
            _group_candidate(
                EvidenceGroupKind.TABLE_ROW_GROUP,
                tuple(sorted(partition, key=_source_order))[:max_member_chunks],
                group_key=(first.neighbor_group_id, "unknown-row"),
                reasons=("MISSING_TABLE_COORDINATES",),
                rerank_text_char_limit=rerank_text_char_limit,
            ),
        )
    if not header_rows and _has_short_column_headers(rows.get(0, ())):
        header_rows.add(0)
    headers = tuple(
        item for row in sorted(header_rows) for item in rows.get(row, ())
    )
    data_rows = tuple(
        row for row in sorted(rows) if row not in header_rows
    ) or (min(rows),)
    result: list[GroupCandidate] = []
    for row in data_rows:
        row_members = tuple(sorted(rows[row], key=_source_order))
        full_members = _unique_members((*headers, *row_members))
        members = full_members[:max_member_chunks]
        reasons: list[str] = []
        if not headers:
            reasons.append("MISSING_TABLE_HEADER")
        row_columns = {
            column
            for item in row_members
            for member_row, column in _coordinates(item)
            if member_row == row
        }
        if 0 not in row_columns:
            reasons.append("MISSING_ROW_LABEL")
        if len(row_members) > 1 and not _connected(row_members):
            reasons.append("MISSING_ROW_CONTINUATION")
        if len(full_members) > max_member_chunks:
            reasons.append("MEMBER_LIMIT")
        if input_truncated:
            reasons.append("INPUT_LIMIT")
        first = partition[0].hydrated.chunk
        if not first.heading_path:
            reasons.append("MISSING_TABLE_TITLE")
        result.append(
            _group_candidate(
                EvidenceGroupKind.TABLE_ROW_GROUP,
                members,
                group_key=(first.neighbor_group_id, f"row:{row}"),
                reasons=tuple(dict.fromkeys(reasons)),
                rerank_text_char_limit=rerank_text_char_limit,
            )
        )
    return tuple(result)


def _group_candidate(
    kind: EvidenceGroupKind,
    members: tuple[RankedChunk, ...],
    *,
    group_key: tuple[str, ...],
    reasons: tuple[str, ...],
    rerank_text_char_limit: int,
) -> GroupCandidate:
    first = members[0].hydrated.chunk
    coordinates = tuple(
        dict.fromkeys(
            coordinate
            for member in members
            for coordinate in _coordinate_labels(member)
        )
    )
    metadata = dict(first.metadata)
    title = metadata.get("document_title")
    if not isinstance(title, str) or not title:
        title = members[0].hydrated.display_name
    lines = [
        f"文档：{title[:96]}",
        f"章节：{(' > '.join(first.heading_path) or first.section_id)[:128]}",
        f"结构：{kind.value}",
    ]
    department = metadata.get("department_name")
    if isinstance(department, str) and department:
        lines.append(f"部门：{department[:64]}")
    category = metadata.get("category_path")
    if isinstance(category, (list, tuple)):
        parts = tuple(item for item in category if isinstance(item, str))
        if parts:
            lines.append(f"分类：{' > '.join(parts)[:128]}")
    if coordinates:
        lines.append(f"坐标：{' '.join(coordinates)[:128]}")
    # 成员自己的 embedding_text 已包含同类前缀；组内只放一次结构前缀，
    # 原文按成员顺序保留，避免重复元数据挤掉表格行或流程末尾条件。
    lines.extend(member.hydrated.chunk.citation_text for member in members)
    full_text = "\n".join(lines)
    rerank_text = _rerank_preview(full_text, rerank_text_char_limit)
    source_maps = tuple(
        GroupSourceMap(
            chunk_id=member.hydrated.chunk.chunk_id,
            citation_text=member.hydrated.chunk.citation_text,
            source_spans=member.hydrated.chunk.source_spans,
            structural_coordinates=_coordinate_labels(member),
        )
        for member in members
    )
    group = EvidenceGroup(
        group_id=_group_id(
            first.version.document_version_id,
            first.index_revision_id,
            first.section_id,
            kind.value,
            group_key,
            tuple(item.chunk_id for item in source_maps),
            tuple(
                (
                    item.structural_coordinates,
                    tuple(span.node_id for span in item.source_spans),
                )
                for item in source_maps
            ),
        ),
        kind=kind,
        document_id=first.version.document_id,
        document_version_id=first.version.document_version_id,
        index_revision_id=first.index_revision_id,
        section_id=first.section_id,
        display_name=members[0].hydrated.display_name,
        heading_path=first.heading_path,
        member_chunk_ids=tuple(item.chunk_id for item in source_maps),
        member_source_maps=source_maps,
        member_ranks=tuple(
            item.rerank_rank or item.fusion_rank for item in members
        ),
        structural_coordinates=coordinates,
        group_text_for_model="\n".join(
            item.hydrated.chunk.citation_text for item in members
        ),
        group_text_for_rerank=rerank_text,
        complete=not reasons,
        incomplete_reasons=tuple(dict.fromkeys(reasons)),
        # 组文本只包含一次结构前缀和成员引用原文；Chunk 的 token_count
        # 还计入每块重复的检索前缀，用它会把完整表格行错误地挤出组预算。
        token_cost=max(1, len(full_text)),
        metadata=freeze_json_object(
            {
                "department_name": department,
                "category_path": category,
            }
        ),
    )
    return GroupCandidate(group, members, rerank_text)


def _chain_reasons(members: tuple[RankedChunk, ...]) -> tuple[str, ...]:
    first = members[0].hydrated.chunk
    last = members[-1].hydrated.chunk
    reasons: list[str] = []
    if first.previous_chunk_id is not None:
        reasons.append("MISSING_PREVIOUS_NEIGHBOR")
    if last.next_chunk_id is not None:
        reasons.append("MISSING_NEXT_NEIGHBOR")
    if not _connected(members):
        reasons.append("BROKEN_NEIGHBOR_CHAIN")
    return tuple(reasons)


def _connected(members: tuple[RankedChunk, ...]) -> bool:
    return all(
        left.hydrated.chunk.next_chunk_id == right.hydrated.chunk.chunk_id
        and right.hydrated.chunk.previous_chunk_id
        == left.hydrated.chunk.chunk_id
        for left, right in pairwise(members)
    )


def _source_order(member: RankedChunk) -> tuple[int, int, int, str]:
    """同一来源节点被切块时，按原文偏移而非检索名次排列。"""
    positions = (
        (
            span.source_anchor.ordinal,
            span.source_start_char
            if span.source_start_char is not None
            else 0,
        )
        for span in member.hydrated.chunk.source_spans
        if span.source_anchor is not None and span.is_citable
    )
    position = min(positions, default=(2**31, 2**31))
    return (
        *position,
        member.fusion_rank,
        member.hydrated.chunk.chunk_id,
    )


def _first_ordinal(member: RankedChunk) -> int | None:
    ordinals = (
        span.source_anchor.ordinal
        for span in member.hydrated.chunk.source_spans
        if span.source_anchor is not None and span.is_citable
    )
    return min(ordinals, default=None)


def _last_ordinal(member: RankedChunk) -> int | None:
    ordinals = (
        span.source_anchor.ordinal
        for span in member.hydrated.chunk.source_spans
        if span.source_anchor is not None and span.is_citable
    )
    return max(ordinals, default=None)


def _coordinates(member: RankedChunk) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for span in member.hydrated.chunk.source_spans:
        coordinate = _span_coordinate(span)
        if coordinate is not None:
            result.append(coordinate)
    return tuple(dict.fromkeys(result))


def _span_coordinate(span: SourceSpan) -> tuple[int, int] | None:
    """从可引用的 canonical 来源跨度提取行列坐标。"""
    if not span.is_citable or span.source_anchor is None:
        return None
    row = span.source_anchor.row_index
    column = span.source_anchor.cell_index
    for part in span.structural_path:
        row_match = _TABLE_ROW.fullmatch(part)
        column_match = _TABLE_COLUMN.fullmatch(part)
        if row_match is not None:
            row = int(row_match[1])
        if column_match is not None:
            column = int(column_match[1])
    if row is None or column is None:
        return None
    return row, column


def _has_short_column_headers(members: list[RankedChunk]) -> bool:
    """仅在首行多列均为短标签时推断未标记的 DOCX 表头。"""
    columns: dict[int, list[str]] = defaultdict(list)
    for member in members:
        chunk = member.hydrated.chunk
        for span in chunk.source_spans:
            coordinate = _span_coordinate(span)
            if coordinate is None or coordinate[0] != 0:
                continue
            value = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ].strip()
            if value:
                columns[coordinate[1]].append(value)
    if 0 not in columns or len(columns) < _MIN_HEADER_COLUMNS:
        return False
    return all(
        len(" ".join(values)) <= _INFERRED_HEADER_CELL_CHAR_LIMIT
        for values in columns.values()
    )


def _coordinate_labels(member: RankedChunk) -> tuple[str, ...]:
    return tuple(f"r{row}:c{column}" for row, column in _coordinates(member))


def _row_indexes(member: RankedChunk) -> tuple[int, ...]:
    return tuple(dict.fromkeys(row for row, _column in _coordinates(member)))


def _table_header_rows(member: RankedChunk) -> tuple[int, ...]:
    atoms = dict(member.hydrated.chunk.metadata).get("atoms")
    if not isinstance(atoms, (list, tuple)):
        return ()
    rows: list[int] = []
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        metadata = atom.get("metadata")
        if not isinstance(metadata, dict):
            continue
        row = metadata.get("row_index")
        if metadata.get("header_strategy") == "tblHeader" and isinstance(
            row, int
        ):
            rows.append(row)
    return tuple(dict.fromkeys(rows))


def _unique_members(
    members: tuple[RankedChunk, ...],
) -> tuple[RankedChunk, ...]:
    by_id: dict[str, RankedChunk] = {}
    for member in members:
        by_id.setdefault(member.hydrated.chunk.chunk_id, member)
    return tuple(by_id.values())


def _group_id(*parts: object) -> str:
    digest = hashlib.sha256(
        canonical_json((_GROUP_SCHEMA_REVISION, *parts)).encode("utf-8")
    ).hexdigest()
    return f"egrp_{digest[:32]}"


def _rerank_preview(text: str, char_limit: int) -> str:
    if len(text) <= char_limit:
        return text
    marker = "\n…\n"
    if char_limit <= len(marker) + 2:
        return text[:char_limit]
    head = (char_limit - len(marker)) // 2
    tail = char_limit - len(marker) - head
    return f"{text[:head]}{marker}{text[-tail:]}"
