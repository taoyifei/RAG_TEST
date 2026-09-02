"""把 OOXML 表格映射为物理节点和逻辑网格。"""

from __future__ import annotations

from typing import Protocol

from lxml import etree

from rag_app.adapters.parsers.docx.builder import IrNodeBuilder
from rag_app.adapters.parsers.docx.issues import IssueCollector
from rag_app.adapters.parsers.docx.namespaces import WORD, qn, word_attr
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import CellGrid, NodeKind, SourceAnchor, StoryKind
from rag_app.core.policies import ParsingMode, ParsingPolicy


class _BlockParser(Protocol):
    builder: IrNodeBuilder
    issues: IssueCollector
    policy: ParsingPolicy

    def anchor(  # noqa: PLR0913
        self,
        *,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        table_index: int | None = None,
        row_index: int | None = None,
        cell_index: int | None = None,
    ) -> SourceAnchor:
        """创建来源锚点。

        Args:
            part_uri: 当前 Part URI。
            story_kind: 当前 story 类型。
            structural_path: 稳定结构路径。
            table_index: 可选表格序号。
            row_index: 可选行序号。
            cell_index: 可选物理 cell 序号。

        Returns:
            表格结构来源锚点。

        """
        ...

    def parse_container(  # noqa: PLR0913
        self,
        container: etree._Element,
        *,
        parent_node_id: str | None,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        table_depth: int,
    ) -> None:
        """递归解析一个 block 容器。

        Args:
            container: cell 内 block 容器。
            parent_node_id: cell 或嵌套节点 ID。
            part_uri: 当前 Part URI。
            story_kind: 当前 story 类型。
            structural_path: 稳定结构路径。
            table_depth: 当前嵌套表格深度。

        Returns:
            无返回值。

        """
        ...


def parse_table(  # noqa: PLR0913
    parser: _BlockParser,
    table: etree._Element,
    *,
    parent_node_id: str | None,
    part_uri: str,
    story_kind: StoryKind,
    structural_path: tuple[str, ...],
    table_index: int,
    table_depth: int,
) -> str:
    """解析一个表格并返回 TableNode ID。

    Args:
        parser: 共享 block parser 上下文。
        table: `w:tbl` 元素。
        parent_node_id: 可选父节点。
        part_uri: 当前 story Part URI。
        story_kind: 当前 story 类型。
        structural_path: 表格稳定结构路径。
        table_index: 当前 Part 内表格序号。
        table_depth: 当前嵌套表格深度。

    Returns:
        新建 TableNode ID。

    Raises:
        InvalidDocument: 嵌套深度或严格网格语义无效。

    """
    if table_depth >= parser.policy.max_table_depth:
        raise InvalidDocument(
            "DOCX 嵌套表格深度超过限制。",
            stage="docx-ooxml-v4.resource",
        )
    grid = table.find(qn(WORD, "tblGrid"))
    grid_columns = (
        0 if grid is None else len(grid.findall(qn(WORD, "gridCol")))
    )
    if grid_columns == 0:
        parser.issues.add(
            "DOCX_TABLE_GRID_MISSING",
            action="physical_cells_preserved",
            message="表格缺少 tblGrid，保留物理单元格。",
        )
    table_node = parser.builder.add(
        kind=NodeKind.TABLE,
        parent_node_id=parent_node_id,
        anchor=parser.anchor(
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
            table_index=table_index,
        ),
        metadata={
            "grid_columns": grid_columns,
            "nested_depth": table_depth,
        },
    )
    active_vertical_merges: dict[tuple[int, int], str] = {}
    rows = table.findall(qn(WORD, "tr"))
    for row_index, row in enumerate(rows):
        row_properties = row.find(qn(WORD, "trPr"))
        grid_before = _integer_child(row_properties, "gridBefore") or 0
        grid_after = _integer_child(row_properties, "gridAfter") or 0
        row_path = (*structural_path, f"tr:{row_index}")
        row_node = parser.builder.add(
            kind=NodeKind.TABLE_ROW,
            parent_node_id=table_node.node_id,
            anchor=parser.anchor(
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=row_path,
                table_index=table_index,
                row_index=row_index,
            ),
            metadata={
                "grid_before": grid_before,
                "grid_after": grid_after,
                "repeated_header": _has_child(row_properties, "tblHeader"),
            },
        )
        column_index = grid_before
        continued_keys: set[tuple[int, int]] = set()
        for cell_index, cell in enumerate(row.findall(qn(WORD, "tc"))):
            properties = cell.find(qn(WORD, "tcPr"))
            column_span = _integer_child(properties, "gridSpan") or 1
            if column_span <= 0:
                _grid_failure(parser, "表格 cell gridSpan 必须为正数。")
                column_span = 1
            if grid_columns and column_index + column_span > grid_columns:
                _grid_failure(parser, "表格 cell span 超出 tblGrid。")
            merge_node = (
                None
                if properties is None
                else properties.find(qn(WORD, "vMerge"))
            )
            merge_value = (
                None
                if merge_node is None
                else merge_node.get(word_attr("val")) or "continue"
            )
            cell_path = (*row_path, f"tc:{cell_index}")
            metadata: dict[str, object] = {
                "physical_cell_index": cell_index,
                "vertical_merge": merge_value,
            }
            merge_key = (column_index, column_span)
            if merge_value == "continue":
                anchor_id = active_vertical_merges.get(merge_key)
                if anchor_id is None:
                    _grid_failure(parser, "表格存在悬空 vMerge continuation。")
                else:
                    metadata["vmerge_anchor_node_id"] = anchor_id
                    continued_keys.add(merge_key)
                    anchor = parser.builder.get(anchor_id)
                    if anchor.cell_grid is not None:
                        anchor.cell_grid = anchor.cell_grid.model_copy(
                            update={
                                "row_span": anchor.cell_grid.row_span + 1
                            }
                        )
            cell_node = parser.builder.add(
                kind=NodeKind.TABLE_CELL,
                parent_node_id=row_node.node_id,
                anchor=parser.anchor(
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=cell_path,
                    table_index=table_index,
                    row_index=row_index,
                    cell_index=cell_index,
                ),
                cell_grid=CellGrid(
                    row_index=row_index,
                    column_index=column_index,
                    column_span=column_span,
                ),
                metadata=metadata,
            )
            if merge_value == "restart":
                active_vertical_merges[merge_key] = cell_node.node_id
                continued_keys.add(merge_key)
            parser.parse_container(
                cell,
                parent_node_id=cell_node.node_id,
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=cell_path,
                table_depth=table_depth + 1,
            )
            column_index += column_span
        active_vertical_merges = {
            key: value
            for key, value in active_vertical_merges.items()
            if key in continued_keys
        }
        expected_end = grid_columns - grid_after if grid_columns else None
        if expected_end is not None and column_index != expected_end:
            _grid_failure(parser, "表格行逻辑网格宽度不一致。")
    return table_node.node_id


def _grid_failure(parser: _BlockParser, message: str) -> None:
    if parser.policy.mode is ParsingMode.STRICT:
        raise InvalidDocument(
            message,
            stage="docx-ooxml-v4.table",
        )
    parser.issues.add(
        "DOCX_TABLE_GRID_INCONSISTENT",
        action="physical_cells_preserved",
        message=message,
    )


def _integer_child(
    parent: etree._Element | None,
    local_name: str,
) -> int | None:
    if parent is None:
        return None
    node = parent.find(qn(WORD, local_name))
    if node is None:
        return None
    value = node.get(word_attr("val"))
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _has_child(parent: etree._Element | None, local_name: str) -> bool:
    return (
        parent is not None
        and parent.find(qn(WORD, local_name)) is not None
    )
