"""来源结构相容的纯合同；相容只准许联合核验，不证明事实支持。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from rag_app.core.models import EvidenceItem, SourceSpan, SourceSpanKind
from rag_app.core.models.evidence_group import EvidenceGroup, EvidenceGroupKind

_TABLE_INTERSECTION_MIN_MEMBERS = 3


@dataclass(frozen=True, slots=True)
class SourceCompatibility:
    """保留结构判定及可审计的来源证明，不包含语义支持结论。

    Attributes:
        compatible: 真实来源身份和结构是否允许联合核验。
        reason: 稳定的相容或拒绝原因，不含正文。
        proof_key: 可复核的来源定位及成员证明键。
        ordered_support_ids: 按真实位置排列的当前发送别名。

    """

    compatible: bool
    reason: str
    proof_key: tuple[object, ...] = ()
    ordered_support_ids: tuple[str, ...] = ()


def _identity(
    item: EvidenceItem, span: SourceSpan
) -> tuple[object, ...] | None:
    """全空字段、失配路径和缺节点不能成为相同来源的证明。"""
    anchor = span.source_anchor
    if (
        not item.document_id
        or not item.document_version_id
        or not span.node_id
        or anchor is None
        or not anchor.part_uri
        or not anchor.story_kind
        or not span.structural_path
        or span.structural_path != anchor.structural_path
    ):
        return None
    return (
        item.document_id,
        item.document_version_id,
        anchor.part_uri,
        anchor.story_kind,
    )


def _span_cell(
    item: EvidenceItem, span: SourceSpan
) -> tuple[tuple[object, ...], int, int] | None:
    """优先使用最内层逻辑表路径；数字坐标必须是真正整数。"""
    identity = _identity(item, span)
    anchor = span.source_anchor
    if identity is None or anchor is None:
        return None
    path = span.structural_path
    for index in range(len(path) - 3, -1, -1):
        if not path[index].startswith("tbl:"):
            continue
        row = re.fullmatch(r"tr:(\d+)", path[index + 1])
        column = re.fullmatch(r"tc:(\d+)", path[index + 2])
        if row is not None and column is not None:
            return (
                (*identity, item.table_locator, path[: index + 1]),
                int(row[1]),
                int(column[1]),
            )
    if all(
        type(value) is int and value >= 0
        for value in (anchor.table_index, anchor.row_index, anchor.cell_index)
    ):
        assert anchor.row_index is not None  # noqa: S101
        assert anchor.cell_index is not None  # noqa: S101
        return (
            (
                *identity,
                item.table_locator,
                ("table-index", anchor.table_index),
            ),
            anchor.row_index,
            anchor.cell_index,
        )
    return None


def table_cell_coordinate(
    item: EvidenceItem,
) -> tuple[tuple[object, ...], int, int] | None:
    """返回全部真实跨度一致的唯一逻辑表、行、列。

    Args:
        item: 已授权且携真实来源跨度的证据单元。

    Returns:
        逻辑表身份、行号与列号；缺坐标或跨度冲突时为 None。

    """
    cells = {_span_cell(item, span) for span in item.source_spans}
    if not cells or None in cells or len(cells) != 1:
        return None
    return next(iter(cells))


def source_group_keys(item: EvidenceItem) -> set[tuple[object, ...]]:
    """仅从真实坐标提取分组候选，组证书另由完整成员映射核对。

    Args:
        item: 需要提取物理来源身份的证据单元。

    Returns:
        真实节点或表格行的身份集合；身份不完整时为空集。

    """
    groups: set[tuple[object, ...]] = set()
    for span in item.source_spans:
        identity = _identity(item, span)
        if identity is None:
            return set()
        cell = _span_cell(item, span)
        if cell is not None:
            table, row, _column = cell
            groups.add(("table-row", *table, row))
        elif item.table_context or item.table_locator is not None:
            return set()
        else:
            groups.add(("node", *identity, span.node_id))
    return groups


def _source_range(span: SourceSpan) -> tuple[int, int] | None:
    """再次核对内部 model_copy 等入口可能绕过的严格整数边界。"""
    start, end = span.source_start_char, span.source_end_char
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
    ):
        return None
    return start, end


def _continuous_node(  # noqa: PLR0911
    units: tuple[EvidenceItem, ...],
) -> SourceCompatibility:
    """允许来源顺序重排，重叠部分必须有完全相同的逐字证明。"""
    records: list[tuple[int, int, str, str]] = []
    nodes: set[tuple[object, ...]] = set()
    for item in units:
        if len(item.source_spans) != 1:
            return SourceCompatibility(False, "NODE_MAPPING_NOT_UNIQUE")
        span = item.source_spans[0]
        identity = _identity(item, span)
        source_range = _source_range(span)
        if identity is None or source_range is None:
            return SourceCompatibility(False, "SOURCE_RANGE_NOT_PROVED")
        start, end = source_range
        if end - start != len(item.citation_text):
            return SourceCompatibility(False, "SOURCE_RANGE_TEXT_MISMATCH")
        nodes.add((*identity, span.node_id, _span_cell(item, span)))
        records.append((start, end, item.citation_text, item.support_id))
    if len(nodes) != 1:
        return SourceCompatibility(False, "DIFFERENT_SOURCE_NODE")
    records.sort(key=lambda value: (value[0], value[1], value[3]))
    start, end, joined, _support_id = records[0]
    for next_start, next_end, text, _support_id in records[1:]:
        if next_start > end:
            return SourceCompatibility(False, "SOURCE_RANGE_GAP")
        overlap = min(end, next_end) - next_start
        offset = next_start - start
        if overlap and joined[offset : offset + overlap] != text[:overlap]:
            return SourceCompatibility(False, "SOURCE_OVERLAP_MISMATCH")
        if next_end > end:
            joined += text[overlap:]
            end = next_end
    return SourceCompatibility(
        True,
        "CONTIGUOUS_NODE",
        ("node", *next(iter(nodes)), start, end),
        tuple(record[3] for record in records),
    )


def reconstruct_complete_node_text(  # noqa: PLR0911
    units: tuple[EvidenceItem, ...],
) -> str | None:
    """仅从完整且逐字一致的同一原始节点恢复文本。"""
    if not units or not source_compatibility(units).compatible:
        return None
    records: list[tuple[int, int, str]] = []
    origins: set[tuple[object, ...]] = set()
    for item in units:
        if len(item.source_spans) != 1:
            return None
        span = item.source_spans[0]
        anchor = span.source_anchor
        position = _source_range(span)
        identity = _identity(item, span)
        if (
            anchor is None
            or position is None
            or identity is None
            or not span.node_id
            or type(anchor.source_start_char) is not int
            or type(anchor.source_end_char) is not int
            or position[1] - position[0] != len(item.citation_text)
        ):
            return None
        origins.add(
            (
                *identity,
                span.node_id,
                anchor.structural_path,
                anchor.source_start_char,
                anchor.source_end_char,
            )
        )
        records.append((*position, item.citation_text))
    if len(origins) != 1:
        return None
    origin = next(iter(origins))
    start, end = origin[-2:]
    if type(start) is not int or type(end) is not int or end <= start:
        return None
    records.sort(key=lambda row: (row[0], row[1], row[2]))
    cursor = start
    recovered = ""
    for low, high, text in records:
        if low > cursor or low < start or high > end:
            return None
        overlap = min(cursor, high) - low
        offset = low - start
        if overlap and recovered[offset : offset + overlap] != text[:overlap]:
            return None
        if high > cursor:
            recovered += text[overlap:]
            cursor = high
    return recovered if cursor == end else None


def _certificate_nodes(item: EvidenceItem) -> tuple[str, ...]:
    """只读取认证所指的节点，仍须用真实表格关系进行核对。"""
    certificate = dict(item.metadata).get("answer_support")
    if not isinstance(certificate, dict):
        return ()
    values = certificate.get("supporting_span_ids")
    if not isinstance(values, list) or not values:
        return ()
    if not all(isinstance(value, str) and value for value in values):
        return ()
    return tuple(value for value in values if isinstance(value, str))


def _table_relation(  # noqa: PLR0911
    units: tuple[EvidenceItem, ...],
    cells: tuple[tuple[tuple[object, ...], int, int], ...],
) -> SourceCompatibility:
    """跨行只准许认证表头与唯一目标数据行形成真实行列映射。"""
    certificates = tuple(
        dict(item.metadata).get("answer_support") for item in units
    )
    first = certificates[0]
    if (
        not isinstance(first, dict)
        or not all(certificate == first for certificate in certificates)
        or first.get("status") != "SUPPORTED"
        or first.get("support_reason")
        not in {"TABLE_INTERSECTION", "TABLE_ROW_CONTENT"}
    ):
        return SourceCompatibility(False, "SIBLING_TABLE_ROWS")
    required = _certificate_nodes(units[0])
    selected = {span.node_id for item in units for span in item.source_spans}
    if not required or not selected <= set(required):
        return SourceCompatibility(False, "TABLE_CERTIFICATE_MEMBER_MISMATCH")
    reason = first["support_reason"]
    if reason == "TABLE_INTERSECTION" and (
        len(required) < _TABLE_INTERSECTION_MIN_MEMBERS
        or len(required) != len(set(required))
        or set(required) != selected
    ):
        return SourceCompatibility(False, "TABLE_INTERSECTION_INCOMPLETE")
    target = first.get("query_target")
    labels = [
        (row, column)
        for item, (_table, row, column) in zip(units, cells, strict=True)
        if isinstance(target, str)
        and item.citation_text.strip() == target.strip()
    ]
    if len(set(labels)) != 1:
        return SourceCompatibility(False, "TABLE_TARGET_ROW_NOT_PROVED")
    target_row, label_column = labels[0]
    values = {
        column
        for _table, row, column in cells
        if row == target_row and column != label_column
    }
    headers = {column for _table, row, column in cells if row < target_row}
    if (
        not values
        or not values <= headers
        or any(
            row > target_row or (row < target_row and column == label_column)
            for _table, row, column in cells
        )
    ):
        return SourceCompatibility(False, "TABLE_HEADER_VALUE_NOT_PROVED")
    if reason == "TABLE_INTERSECTION" and len(values) != 1:
        return SourceCompatibility(False, "TABLE_INTERSECTION_NOT_UNIQUE")
    return SourceCompatibility(
        True,
        str(reason),
        (
            "table-intersection"
            if reason == "TABLE_INTERSECTION"
            else "table-row-content",
            *cells[0][0],
            target_row,
            tuple(required),
        ),
        tuple(
            item.support_id
            for item, _cell in sorted(
                zip(units, cells, strict=True),
                key=lambda value: (
                    value[1][1],
                    value[1][2],
                    value[0].support_id,
                ),
            )
        ),
    )


def source_group_contains(item: EvidenceItem, group: EvidenceGroup) -> bool:
    """证据必须属于真实组成员，且每段原文在原始来源映射内。

    Args:
        item: 待核验的逐字证据，不信任其复制来的组 metadata。
        group: 服务端独立物化的来源组与完整成员映射。

    Returns:
        文档版本、成员身份及全部逐字跨度均匹配时为 True。

    """
    if (
        item.document_id != group.document_id
        or item.document_version_id != group.document_version_id
        or item.chunk_id not in group.member_chunk_ids
    ):
        return False
    maps = tuple(
        member
        for member in group.member_source_maps
        if member.chunk_id == item.chunk_id
    )
    for span in item.source_spans:
        current_range = _source_range(span)
        if span.source_anchor is None:
            return False
        matched = False
        for member in maps:
            for mapped in member.source_spans:
                if _same_derived_member(
                    item, span, mapped, member.citation_text
                ):
                    matched = True
                    continue
                mapped_range = _source_range(mapped)
                if (
                    current_range is None
                    or mapped_range is None
                    or mapped.source_anchor is None
                ):
                    continue
                start, end = current_range
                low, high = mapped_range
                if (
                    mapped.node_id == span.node_id
                    and mapped.source_anchor.part_uri
                    == span.source_anchor.part_uri
                    and mapped.source_anchor.story_kind
                    == span.source_anchor.story_kind
                    and mapped.structural_path == span.structural_path
                    and low <= start < end <= high
                ):
                    offset = mapped.chunk_start_char + start - low
                    text = member.citation_text[offset : offset + end - start]
                    if text == item.citation_text:
                        matched = True
        if not matched:
            return False
    return bool(item.source_spans)


def _same_derived_member(
    item: EvidenceItem, span: SourceSpan, mapped: SourceSpan, source: str
) -> bool:
    """派生编号只可精确核对独立成员映射，绝不据此推断节点连续。"""
    return (
        span.span_type is SourceSpanKind.DERIVED_NUMBERING
        and mapped.span_type is SourceSpanKind.DERIVED_NUMBERING
        and span.source_start_char is None
        and span.source_end_char is None
        and mapped.source_start_char is None
        and mapped.source_end_char is None
        and span.node_id == mapped.node_id
        and span.source_anchor is not None
        and span.source_anchor == mapped.source_anchor
        and span.structural_path == mapped.structural_path
        and item.citation_text
        == source[mapped.chunk_start_char : mapped.chunk_end_char]
    )


def source_group_covered(
    evidence: tuple[EvidenceItem, ...], group: EvidenceGroup
) -> bool:
    """逐真实节点区间核对来源组物理覆盖，不推断所问答案完整。

    Args:
        evidence: 预算选择后实际保留的证据单元。
        group: 服务端认证的来源组及原始成员映射。

    Returns:
        全部真实成员与可引用跨度完整覆盖时为 True。

    """
    if not group.complete:
        return False
    members = tuple(
        item for item in evidence if source_group_contains(item, group)
    )
    if {item.chunk_id for item in members} != set(group.member_chunk_ids):
        return False
    for member in group.member_source_maps:
        for mapped in member.source_spans:
            if not mapped.is_citable:
                continue
            if mapped.span_type is SourceSpanKind.DERIVED_NUMBERING:
                if not any(
                    _same_derived_member(
                        item, span, mapped, member.citation_text
                    )
                    for item in members
                    if item.chunk_id == member.chunk_id
                    for span in item.source_spans
                ):
                    return False
                continue
            required_range = _source_range(mapped)
            if required_range is None or mapped.source_anchor is None:
                return False
            required_start, required_end = required_range
            ranges = sorted(
                source_range
                for item in members
                if item.chunk_id == member.chunk_id
                for span in item.source_spans
                if span.node_id == mapped.node_id
                and span.source_anchor is not None
                and span.source_anchor.part_uri == mapped.source_anchor.part_uri
                and span.source_anchor.story_kind
                == mapped.source_anchor.story_kind
                and (source_range := _source_range(span)) is not None
            )
            end = required_start
            for low, high in ranges:
                if low > end:
                    break
                end = max(end, high)
            if end < required_end:
                return False
    return True


def certified_source_group(
    units: tuple[EvidenceItem, ...], trusted_groups: tuple[EvidenceGroup, ...]
) -> EvidenceGroup | None:
    """用独立服务端成员映射认证列表/流程，忽略复制的组名与完整标志。

    Args:
        units: 一条事实拟联合引用的证据单元。
        trusted_groups: 服务端独立物化的真实来源组。

    Returns:
        包含全部所选来源的列表或流程组；无法证明时为 None。

    """
    for group in trusted_groups:
        if group.kind in {
            EvidenceGroupKind.LIST_GROUP,
            EvidenceGroupKind.PROCEDURE_GROUP,
        } and all(source_group_contains(item, group) for item in units):
            return group
    return None


def source_compatibility(  # noqa: PLR0911, PLR0912
    units: tuple[EvidenceItem, ...],
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
) -> SourceCompatibility:
    """统一核对节点连续、同行单元格、表格交点或真实列表成员身份。

    Args:
        units: 一条事实拟联合引用的真实证据单元。
        trusted_groups: 可选的独立服务端列表或流程成员映射。

    Returns:
        类型化的结构判定、原因及定位证明；不代表事实已获支持。

    """
    if not units or any(not item.source_spans for item in units):
        return SourceCompatibility(False, "SOURCE_IDENTITY_MISSING")
    identities = {
        _identity(item, span) for item in units for span in item.source_spans
    }
    if None in identities or len(identities) != 1:
        return SourceCompatibility(False, "SOURCE_IDENTITY_MISMATCH")
    identity = next(identity for identity in identities if identity is not None)
    cells = tuple(table_cell_coordinate(item) for item in units)
    if any(cell is not None for cell in cells):
        if any(cell is None for cell in cells):
            return SourceCompatibility(False, "TABLE_COORDINATE_MISSING")
        actual_cells = tuple(cell for cell in cells if cell is not None)
        if len({cell[0] for cell in actual_cells}) != 1:
            return SourceCompatibility(False, "DIFFERENT_LOGICAL_TABLE")
        if len({cell[1] for cell in actual_cells}) != 1:
            return _table_relation(units, actual_cells)
        if len({cell[2] for cell in actual_cells}) > 1:
            for column in {cell[2] for cell in actual_cells}:
                members = tuple(
                    item
                    for item, cell in zip(units, actual_cells, strict=True)
                    if cell[2] == column
                )
                if (
                    len(members) > 1
                    and not _continuous_node(members).compatible
                ):
                    return SourceCompatibility(
                        False, "TABLE_CELL_NOT_CONTIGUOUS"
                    )
            table, row, _column = actual_cells[0]
            return SourceCompatibility(
                True,
                "SAME_TABLE_ROW",
                ("table-row", *table, row),
                tuple(item.support_id for item in units),
            )
    elif any(
        item.table_context or item.table_locator is not None for item in units
    ):
        return SourceCompatibility(False, "TABLE_COORDINATE_MISSING")
    if len(units) == 1:
        keys = source_group_keys(units[0])
        if len(keys) == 1:
            return SourceCompatibility(
                True, "SINGLE_SOURCE", next(iter(keys)), (units[0].support_id,)
            )
    continuous = _continuous_node(units)
    if continuous.compatible:
        return continuous
    if cells and cells[0] is not None:
        return continuous
    group = certified_source_group(units, trusted_groups)
    if group is not None:
        digest = hashlib.sha256(
            "\n".join(group.member_chunk_ids).encode("utf-8")
        ).hexdigest()
        return SourceCompatibility(
            True,
            "CERTIFIED_SOURCE_GROUP",
            (
                "complete-structured-group",
                *identity,
                group.group_id,
                digest,
            ),
            tuple(item.support_id for item in units),
        )
    return continuous


def compatible_partitions(
    units: tuple[EvidenceItem, ...],
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
) -> tuple[tuple[SourceCompatibility, tuple[EvidenceItem, ...]], ...]:
    """为独立分句核验分区，不用组名把有缺口或跨行的来源拼合。

    Args:
        units: 需要按真实来源拆分的有限证据集合。
        trusted_groups: 可选的独立服务端列表或流程成员映射。

    Returns:
        稳定顺序的分区及各自结构判定，供恢复后逐条重新核验。

    """
    whole = source_compatibility(units, trusted_groups=trusted_groups)
    if whole.compatible:
        return ((whole, units),)
    partitions: list[tuple[EvidenceItem, ...]] = []
    for item in units:
        for index, partition in enumerate(partitions):
            candidate = (*partition, item)
            if source_compatibility(
                candidate, trusted_groups=trusted_groups
            ).compatible:
                partitions[index] = candidate
                break
        else:
            partitions.append((item,))
    return tuple(
        (
            source_compatibility(partition, trusted_groups=trusted_groups),
            partition,
        )
        for partition in partitions
    )
