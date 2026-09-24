"""按活动 DocumentIR 从重排命中恢复可核验的来源阅读组。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    Chunk,
    ChunkRole,
    DocumentIR,
    DocumentNode,
    DocumentVersionRef,
    NodeKind,
    RankedChunk,
    RetrievalPolicy,
    SourceSpan,
)
from rag_app.core.ports import EvidenceSourcePort

CONTEXT_READER_REVISION = "wb08r-context-reader-v1"
SOURCE_STRUCTURE_REVISION = "wb08r-source-ir-view-v1"
_MAX_IR_BYTES = 2_000_000
_MAX_SOURCE_NODES = 64
_MAX_SOURCE_CHUNKS = 200
_MAX_READER_GROUPS = 24


@dataclass(frozen=True, slots=True)
class ContextReadPiece:
    """一个仍指向 canonical Chunk 与真实原文跨度的阅读片段。"""

    candidate: RankedChunk
    span: SourceSpan

    @property
    def text(self) -> str:
        """返回原始 Chunk 中该 SourceSpan 的逐字内容。"""
        chunk = self.candidate.hydrated.chunk
        return chunk.citation_text[
            self.span.chunk_start_char : self.span.chunk_end_char
        ]


@dataclass(frozen=True, slots=True)
class ContextReadGroup:
    """来源身份固定、可整体预算选择的结构单元。"""

    group_id: str
    kind: Literal["table_row", "list", "source_node"]
    seed_chunk_ids: tuple[str, ...]
    pieces: tuple[ContextReadPiece, ...]
    required_node_ids: tuple[str, ...]
    missing_node_ids: tuple[str, ...]
    source_complete: bool
    reason_codes: tuple[str, ...] = ()
    table_node_id: str | None = None
    table_row_index: int | None = None

    @property
    def candidates(self) -> tuple[RankedChunk, ...]:
        """按首次出现的顺序返回组内规范 Chunk。"""
        return tuple(
            {
                piece.candidate.hydrated.chunk.chunk_id: piece.candidate
                for piece in self.pieces
            }.values()
        )


@dataclass(frozen=True, slots=True)
class ContextReadResult:
    """同一 ranked seeds 的来源回读结果，不包含模型调用。"""

    groups: tuple[ContextReadGroup, ...]
    skipped_seed_ids: tuple[tuple[str, str], ...] = ()

    @property
    def candidates(self) -> tuple[RankedChunk, ...]:
        """按组顺序展开去重后的 canonical Chunk。"""
        return tuple(
            {
                candidate.hydrated.chunk.chunk_id: candidate
                for group in self.groups
                for candidate in group.candidates
            }.values()
        )


@dataclass(frozen=True, slots=True)
class _ReadTarget:
    """节点到目标逻辑行的映射；物理节点可以来自合并单元格上一行。"""

    kind: Literal["table_row", "list", "source_node"]
    identity: tuple[object, ...]
    node_rows: tuple[tuple[str, int | None], ...]
    table_node_id: str | None = None
    reason_codes: tuple[str, ...] = ()


class ContextReader:
    """以权威 IR 关系回读完整节点，再核对 Chunk 的逐字覆盖。"""

    def __init__(self, source: EvidenceSourcePort) -> None:
        self._source = source

    def read(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        seeds: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
    ) -> ContextReadResult:
        """只读恢复同一活动 revision 的有界来源组。

        Args:
            snapshot: 单请求冻结的活动索引快照。
            seeds: 已完成现有重排的不可变命中序列。
            policy: 当前部署的现有检索及生成预算。

        Returns:
            按 seed 顺序排列的完整或明确部分来源组。

        """
        groups: dict[str, ContextReadGroup] = {}
        skipped: list[tuple[str, str]] = []
        ir_cache: dict[tuple[str, str], DocumentIR | None] = {}
        seed_by_id = {seed.hydrated.chunk.chunk_id: seed for seed in seeds}
        for seed in seeds[: policy.rerank_candidate_limit]:
            chunk = seed.hydrated.chunk
            seed_id = chunk.chunk_id
            if len(groups) >= _MAX_READER_GROUPS:
                skipped.append((seed_id, "GROUP_LIMIT"))
                continue
            if (
                chunk.project_id != snapshot.revision.project_id
                or chunk.knowledge_base_id
                != snapshot.revision.knowledge_base_id
                or chunk.index_revision_id
                != snapshot.revision.index_revision_id
            ):
                skipped.append((seed_id, "SOURCE_SCOPE_MISMATCH"))
                continue
            version = chunk.version
            cache_key = (version.document_id, version.document_version_id)
            if cache_key not in ir_cache:
                ir_cache[cache_key] = self._source.load_document_ir(
                    snapshot, version, max_bytes=_MAX_IR_BYTES
                )
            document_ir = ir_cache[cache_key]
            if document_ir is None:
                skipped.append((seed_id, "DOCUMENT_IR_BUDGET"))
                continue
            nodes = {node.node_id: node for node in document_ir.nodes}
            targets = _read_targets(seed, nodes)
            if not targets:
                skipped.append((seed_id, "SOURCE_TARGET_UNDETERMINED"))
                continue
            for target in targets:
                group_id = canonical_sha256(
                    {
                        "revision": CONTEXT_READER_REVISION,
                        "scope": (
                            chunk.project_id,
                            chunk.knowledge_base_id,
                            chunk.index_revision_id,
                        ),
                        "document": cache_key,
                        "source": target.identity,
                    }
                )
                existing = groups.get(group_id)
                if existing is not None:
                    groups[group_id] = replace(
                        existing,
                        seed_chunk_ids=tuple(
                            dict.fromkeys((*existing.seed_chunk_ids, seed_id))
                        ),
                    )
                    continue
                if len(groups) >= _MAX_READER_GROUPS:
                    skipped.append((seed_id, "GROUP_LIMIT"))
                    break
                groups[group_id] = self._read_group(
                    snapshot=snapshot,
                    version=version,
                    seed=seed,
                    seed_by_id=seed_by_id,
                    nodes=nodes,
                    target=target,
                    group_id=group_id,
                    policy=policy,
                )
        return ContextReadResult(tuple(groups.values()), tuple(skipped))

    def _read_group(  # noqa: PLR0913
        self,
        *,
        snapshot: ActiveRevisionQuerySnapshot,
        version: DocumentVersionRef,
        seed: RankedChunk,
        seed_by_id: dict[str, RankedChunk],
        nodes: dict[str, DocumentNode],
        target: _ReadTarget,
        group_id: str,
        policy: RetrievalPolicy,
    ) -> ContextReadGroup:
        """精确回读所需节点，逐字核对完整性。"""
        node_ids = tuple(dict.fromkeys(node for node, _row in target.node_rows))
        reasons = list(target.reason_codes)
        if not node_ids or len(node_ids) > _MAX_SOURCE_NODES:
            return ContextReadGroup(
                group_id,
                target.kind,
                (seed.hydrated.chunk.chunk_id,),
                (),
                node_ids,
                node_ids,
                False,
                (*reasons, "SOURCE_NODE_LIMIT"),
            )
        ids = self._source.source_node_chunk_ids(
            snapshot,
            version,
            node_ids=node_ids,
            limit=_MAX_SOURCE_CHUNKS,
        )
        if ids is None:
            return ContextReadGroup(
                group_id,
                target.kind,
                (seed.hydrated.chunk.chunk_id,),
                (),
                node_ids,
                node_ids,
                False,
                (*reasons, "SOURCE_CHUNK_LIMIT"),
            )
        hydrated = self._source.hydrate_chunks(snapshot, ids)
        max_group_chunks = min(
            _MAX_SOURCE_CHUNKS,
            max(
                policy.generation_max_group_items,
                policy.group_member_chunk_limit,
            ),
        )
        node_rows = dict(target.node_rows)
        pieces: dict[tuple[object, ...], ContextReadPiece] = {}
        selected_chunks: set[str] = set()
        for item in hydrated:
            chunk = item.chunk
            if chunk.version != version:
                reasons.append("DOCUMENT_VERSION_CONFLICT")
                continue
            for span in chunk.source_spans:
                row = node_rows.get(span.node_id or "")
                node = nodes.get(span.node_id or "")
                if not span.is_citable or node is None:
                    continue
                if target.kind == "table_row" and not _chunk_has_table_row(
                    chunk, target.table_node_id, row, span.node_id
                ):
                    continue
                if span.source_anchor != node.anchor:
                    reasons.append("SOURCE_ANCHOR_CONFLICT")
                    continue
                if len(selected_chunks) >= max_group_chunks and (
                    chunk.chunk_id not in selected_chunks
                ):
                    reasons.append("GROUP_CHUNK_LIMIT")
                    continue
                selected_chunks.add(chunk.chunk_id)
                candidate = seed_by_id.get(chunk.chunk_id)
                if candidate is None:
                    candidate = RankedChunk(
                        hydrated=item,
                        fusion_rank=seed.fusion_rank + len(selected_chunks),
                        expansion_reason="CONTEXT_READER_SOURCE",
                        expansion_seed_ids=(seed.hydrated.chunk.chunk_id,),
                    )
                piece = ContextReadPiece(candidate, span)
                key = (
                    span.node_id,
                    span.source_start_char,
                    span.source_end_char,
                    piece.text,
                )
                pieces.setdefault(key, piece)
        ordered = tuple(
            sorted(
                pieces.values(),
                key=lambda piece: (
                    node_rows.get(piece.span.node_id or "") or 0,
                    piece.span.source_anchor.ordinal
                    if piece.span.source_anchor is not None
                    else 2**31,
                    piece.span.source_start_char or 0,
                    piece.candidate.hydrated.chunk.chunk_id,
                ),
            )
        )
        missing = tuple(
            node_id
            for node_id in node_ids
            if not _covers_exact_node(
                nodes[node_id],
                tuple(
                    piece for piece in ordered if piece.span.node_id == node_id
                ),
            )
        )
        if missing:
            reasons.append("SOURCE_NODE_INCOMPLETE")
        return ContextReadGroup(
            group_id=group_id,
            kind=target.kind,
            seed_chunk_ids=(seed.hydrated.chunk.chunk_id,),
            pieces=ordered,
            required_node_ids=node_ids,
            missing_node_ids=missing,
            source_complete=not reasons and not missing,
            reason_codes=tuple(dict.fromkeys(reasons)),
            table_node_id=target.table_node_id,
            table_row_index=(
                target.identity[2]
                if target.kind == "table_row"
                and type(target.identity[2]) is int
                else None
            ),
        )


def _read_targets(
    seed: RankedChunk, nodes: dict[str, DocumentNode]
) -> tuple[_ReadTarget, ...]:
    """优先使用表格 atom 的逻辑行，再回退到真实节点。"""
    chunk = seed.hydrated.chunk
    targets: list[_ReadTarget] = []
    if chunk.role is ChunkRole.TABLE:
        atoms = dict(chunk.metadata).get("atoms")
        if isinstance(atoms, (list, tuple)):
            for atom in atoms:
                metadata = (
                    atom.get("metadata") if isinstance(atom, dict) else None
                )
                if not isinstance(metadata, dict):
                    continue
                table_id = metadata.get("table_node_id")
                row = metadata.get("row_index")
                if not isinstance(table_id, str) or type(row) is not int:
                    continue
                target = _table_target(table_id, row, metadata, nodes)
                if target is not None:
                    targets.append(target)
        return tuple(dict.fromkeys(targets))
    for span in chunk.source_spans:
        node = nodes.get(span.node_id or "")
        if not span.is_citable or node is None or node.text_payload is None:
            continue
        if node.kind is NodeKind.LIST_ITEM:
            targets.append(_list_target(node, nodes))
        else:
            targets.append(
                _ReadTarget(
                    "source_node",
                    ("node", node.node_id),
                    ((node.node_id, None),),
                )
            )
    return tuple(dict.fromkeys(targets))


def _table_target(
    table_id: str,
    row_index: int,
    atom_metadata: Mapping[str, object],
    nodes: dict[str, DocumentNode],
) -> _ReadTarget | None:
    """从 IR 真实行/合并属主恢复所需节点，不信任展示文本。"""
    table = nodes.get(table_id)
    if table is None or table.kind is not NodeKind.TABLE:
        return None
    rows = tuple(
        nodes[child_id]
        for child_id in table.child_ids
        if child_id in nodes and nodes[child_id].kind is NodeKind.TABLE_ROW
    )
    target_rows = tuple(
        row for row in rows if row.anchor.row_index == row_index
    )
    if len(target_rows) != 1:
        return None
    target_row = target_rows[0]
    target_nodes = _row_source_nodes(target_row, table_id, nodes)
    if target_nodes is None:
        return None
    reasons: list[str] = []
    raw_mapping = atom_metadata.get("cell_source_node_ids")
    if (
        not isinstance(raw_mapping, dict)
        or {
            str(index): list(values)
            for index, values in enumerate(target_nodes)
        }
        != raw_mapping
    ):
        reasons.append("SOURCE_MAPPING_CONFLICT")
    node_rows: list[tuple[str, int | None]] = [
        (node_id, row_index) for values in target_nodes for node_id in values
    ]
    for header in rows:
        header_index = header.anchor.row_index
        if (
            type(header_index) is not int
            or header_index >= row_index
            or not (
                header_index == 0
                or dict(header.metadata).get("repeated_header") is True
            )
        ):
            continue
        header_nodes = _row_source_nodes(header, table_id, nodes)
        if header_nodes is None:
            reasons.append("TABLE_HEADER_STRUCTURE_CONFLICT")
            continue
        node_rows.extend(
            (node_id, header_index)
            for values in header_nodes
            for node_id in values
        )
    return _ReadTarget(
        "table_row",
        ("table", table_id, row_index),
        tuple(dict.fromkeys(node_rows)),
        table_node_id=table_id,
        reason_codes=tuple(reasons),
    )


def _row_source_nodes(
    row: DocumentNode,
    table_id: str,
    nodes: dict[str, DocumentNode],
) -> tuple[tuple[str, ...], ...] | None:
    """只沿同一表的真实单元格及纵向合并属主取文本节点。"""
    cells = tuple(
        nodes[child_id]
        for child_id in row.child_ids
        if child_id in nodes and nodes[child_id].kind is NodeKind.TABLE_CELL
    )
    result: list[tuple[str, ...]] = []
    for cell in cells:
        source = cell
        anchor_id = dict(cell.metadata).get("vmerge_anchor_node_id")
        if isinstance(anchor_id, str):
            owner = nodes.get(anchor_id)
            if (
                owner is None
                or owner.kind is not NodeKind.TABLE_CELL
                or not _belongs_to_table(owner, table_id, nodes)
                or owner.cell_grid is None
                or cell.cell_grid is None
                or owner.cell_grid.column_index != cell.cell_grid.column_index
                or owner.cell_grid.row_index >= cell.cell_grid.row_index
            ):
                return None
            source = owner
        result.append(_descendant_text_nodes(source, nodes))
    return tuple(result)


def _belongs_to_table(
    node: DocumentNode, table_id: str, nodes: dict[str, DocumentNode]
) -> bool:
    parent_id = node.parent_node_id
    while parent_id is not None and parent_id in nodes:
        if parent_id == table_id:
            return True
        parent_id = nodes[parent_id].parent_node_id
    return False


def _descendant_text_nodes(
    node: DocumentNode, nodes: dict[str, DocumentNode]
) -> tuple[str, ...]:
    pending = list(node.child_ids)
    found: list[str] = []
    while pending:
        child = nodes[pending.pop(0)]
        if child.kind is NodeKind.TABLE:
            continue
        if child.text_payload is not None and child.text_payload.exact_text:
            found.append(child.node_id)
        else:
            pending[0:0] = list(child.child_ids)
    return tuple(dict.fromkeys(found))


def _list_target(
    seed: DocumentNode, nodes: dict[str, DocumentNode]
) -> _ReadTarget:
    """从同一父级的连续列表成员建立有界来源组。"""
    parent = nodes.get(seed.parent_node_id or "")
    if parent is None:
        return _ReadTarget(
            "list",
            ("list", seed.node_id),
            ((seed.node_id, None),),
            reason_codes=("LIST_PARENT_MISSING",),
        )
    siblings = tuple(nodes[child_id] for child_id in parent.child_ids)
    position = next(
        index
        for index, node in enumerate(siblings)
        if node.node_id == seed.node_id
    )
    matched = [seed]
    for direction in (-1, 1):
        index = position + direction
        while 0 <= index < len(siblings):
            sibling = siblings[index]
            if (
                sibling.kind is not NodeKind.LIST_ITEM
                or sibling.list_attributes is None
                or seed.list_attributes is None
                or sibling.list_attributes.level != seed.list_attributes.level
                or sibling.list_attributes.restart_group
                != seed.list_attributes.restart_group
            ):
                break
            matched.append(sibling)
            index += direction
    ordered = tuple(sorted(matched, key=lambda node: node.order))
    limited = ordered[:_MAX_SOURCE_NODES]
    reasons = ("SOURCE_NODE_LIMIT",) if len(ordered) > len(limited) else ()
    return _ReadTarget(
        "list",
        (
            "list",
            parent.node_id,
            seed.list_attributes.level if seed.list_attributes else None,
            seed.list_attributes.restart_group
            if seed.list_attributes
            else None,
        ),
        tuple((node.node_id, None) for node in limited),
        reason_codes=reasons,
    )


def _chunk_has_table_row(
    chunk: Chunk,
    table_id: str | None,
    row: int | None,
    node_id: str | None,
) -> bool:
    """重复单元格按逻辑行 atom 映射核验，不用空锚点行号猜测。"""
    if table_id is None or row is None or node_id is None:
        return False
    atoms = dict(chunk.metadata).get("atoms")
    if not isinstance(atoms, (list, tuple)):
        return False
    for atom in atoms:
        values = atom.get("metadata") if isinstance(atom, dict) else None
        if not isinstance(values, dict):
            continue
        mapping = values.get("cell_source_node_ids")
        if (
            values.get("table_node_id") == table_id
            and values.get("row_index") == row
            and isinstance(mapping, dict)
            and any(
                node_id in members
                for members in mapping.values()
                if isinstance(members, (list, tuple))
            )
        ):
            return True
    return False


def _covers_exact_node(
    node: DocumentNode, pieces: tuple[ContextReadPiece, ...]
) -> bool:
    """逐字检查源区间从起点到 IR 终点完整且重叠内容一致。"""
    payload = node.text_payload
    if payload is None or not payload.exact_text or not pieces:
        return False
    expected = payload.exact_text
    ranges: set[tuple[int, int]] = set()
    for piece in pieces:
        span = piece.span
        start, end = span.source_start_char, span.source_end_char
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end > len(expected)
            or end <= start
            or piece.text != expected[start:end]
        ):
            return False
        ranges.add((start, end))
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor:
            return False
        cursor = max(cursor, end)
    return cursor == len(expected)


__all__ = [
    "CONTEXT_READER_REVISION",
    "SOURCE_STRUCTURE_REVISION",
    "ContextReadGroup",
    "ContextReadPiece",
    "ContextReadResult",
    "ContextReader",
]
