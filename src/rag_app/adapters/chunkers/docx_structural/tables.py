"""复杂表格的 row 原子、merge 来源和 nested group 规划。"""

from __future__ import annotations

import hashlib

from rag_app.adapters.chunkers.docx_structural.atoms import (
    AtomicUnit,
    RunPlan,
    SourceFragment,
    node_text_fragments,
)
from rag_app.adapters.chunkers.docx_structural.rendering import (
    separator_fragment,
)
from rag_app.core.models import (
    ChunkRole,
    DocumentIR,
    DocumentNode,
    NodeKind,
    SourceSpanKind,
)
from rag_app.core.models.common import freeze_json_object


def table_group_id(document_version_id: str, table_node_id: str) -> str:
    """生成不含机器路径的稳定表格 neighbor group。

    Args:
        document_version_id: 当前不可变文档版本。
        table_node_id: TABLE 节点 ID。

    Returns:
        `group_` 前缀的稳定身份。

    """
    payload = f"{document_version_id}\x1f{table_node_id}\x1ftable-v3"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"group_{digest[:32]}"


def build_table_run(
    document_ir: DocumentIR,
    table: DocumentNode,
    *,
    section_id: str,
    heading_path: tuple[str, ...],
) -> RunPlan | None:
    """把一张表规划为独立 group 和完整 row 原子。

    Args:
        document_ir: 已校验的 v4 IR。
        table: 当前 TABLE 节点。
        section_id: 表格所在 section。
        heading_path: 表格所在标题路径。

    Returns:
        至少含一个非空 row 时返回 RunPlan，否则返回 None。

    """
    nodes = {node.node_id: node for node in document_ir.nodes}
    rows = [
        nodes[child_id]
        for child_id in table.child_ids
        if nodes[child_id].kind is NodeKind.TABLE_ROW
    ]
    group_id = table_group_id(
        document_ir.version.document_version_id,
        table.node_id,
    )
    header_fragments: tuple[SourceFragment, ...] = ()
    atoms: list[AtomicUnit] = []
    for row in rows:
        fragments, child_groups, cell_coordinates = _row_fragments(
            document_ir,
            row,
            nodes,
        )
        if not fragments:
            continue
        row_metadata = dict(row.metadata)
        is_header = row_metadata.get("repeated_header") is True
        atom = AtomicUnit(
            unit_id=row.node_id,
            role=ChunkRole.TABLE,
            parent_node_id=table.node_id,
            section_id=section_id,
            neighbor_group_id=group_id,
            heading_path=heading_path,
            fragments=fragments,
            metadata=freeze_json_object(
                {
                    "table_node_id": table.node_id,
                    "row_index": row.anchor.row_index,
                    "cell_coordinates": list(cell_coordinates),
                    "header_strategy": "tblHeader" if is_header else "none",
                    "header_confidence": 1.0 if is_header else 0.0,
                    "contains_nested_table": bool(child_groups),
                }
            ),
            child_group_ids=child_groups,
            table_header_fragments=header_fragments,
        )
        atoms.append(atom)
        if is_header:
            header_fragments = fragments
    if not atoms:
        return None
    return RunPlan(
        run_id=f"run_{group_id.removeprefix('group_')}",
        role=ChunkRole.TABLE,
        section_id=section_id,
        neighbor_group_id=group_id,
        heading_path=heading_path,
        atoms=tuple(atoms),
    )


def _row_fragments(
    document_ir: DocumentIR,
    row: DocumentNode,
    nodes: dict[str, DocumentNode],
) -> tuple[tuple[SourceFragment, ...], tuple[str, ...], tuple[str, ...]]:
    fragments: list[SourceFragment] = []
    child_groups: list[str] = []
    coordinates: list[str] = []
    cells = [
        nodes[child_id]
        for child_id in row.child_ids
        if nodes[child_id].kind is NodeKind.TABLE_CELL
    ]
    for cell_index, cell in enumerate(cells):
        cell_fragments = _cell_fragments(cell, nodes)
        nested_tables = _nested_tables(cell, nodes)
        child_groups.extend(
            table_group_id(
                document_ir.version.document_version_id,
                nested.node_id,
            )
            for nested in nested_tables
        )
        grid = cell.cell_grid
        if grid is not None:
            coordinates.append(
                f"r{grid.row_index}:c{grid.column_index}:"
                f"rs{grid.row_span}:cs{grid.column_span}"
            )
        if not cell_fragments:
            continue
        if fragments:
            fragments.append(separator_fragment(" | "))
        fragments.extend(cell_fragments)
        if cell_index + 1 < len(cells) and not cell_fragments:
            continue
    return (
        tuple(fragments),
        tuple(dict.fromkeys(child_groups)),
        tuple(coordinates),
    )


def _cell_fragments(
    cell: DocumentNode,
    nodes: dict[str, DocumentNode],
) -> tuple[SourceFragment, ...]:
    metadata = dict(cell.metadata)
    anchor_id = metadata.get("vmerge_anchor_node_id")
    source_cell = (
        nodes[anchor_id]
        if isinstance(anchor_id, str) and anchor_id in nodes
        else cell
    )
    repeated = source_cell is not cell
    fragments: list[SourceFragment] = []
    for child_id in source_cell.child_ids:
        child = nodes[child_id]
        if child.kind is NodeKind.TABLE:
            continue
        for fragment in _visible_descendant_fragments(child, nodes):
            if fragments:
                fragments.append(separator_fragment("\n"))
            resolved_fragment = fragment
            if repeated and fragment.node_id is not None:
                resolved_fragment = SourceFragment(
                    text=fragment.text,
                    span_type=SourceSpanKind.REPEATED_CONTEXT,
                    node_id=fragment.node_id,
                    source_anchor=fragment.source_anchor,
                    source_start_char=fragment.source_start_char,
                    source_end_char=fragment.source_end_char,
                    metadata=fragment.metadata,
                    is_repeated=True,
                )
            fragments.append(resolved_fragment)
    return tuple(fragments)


def _visible_descendant_fragments(
    node: DocumentNode,
    nodes: dict[str, DocumentNode],
) -> tuple[SourceFragment, ...]:
    if node.kind in {NodeKind.PARAGRAPH, NodeKind.LIST_ITEM}:
        return node_text_fragments(node)
    fragments: list[SourceFragment] = []
    for child_id in node.child_ids:
        child = nodes[child_id]
        if child.kind is NodeKind.TABLE:
            continue
        descendants = _visible_descendant_fragments(child, nodes)
        if fragments and descendants:
            fragments.append(separator_fragment("\n"))
        fragments.extend(descendants)
    return tuple(fragments)


def _nested_tables(
    cell: DocumentNode,
    nodes: dict[str, DocumentNode],
) -> tuple[DocumentNode, ...]:
    pending = list(cell.child_ids)
    tables: list[DocumentNode] = []
    while pending:
        child = nodes[pending.pop(0)]
        if child.kind is NodeKind.TABLE:
            tables.append(child)
            continue
        pending[0:0] = list(child.child_ids)
    return tuple(tables)
