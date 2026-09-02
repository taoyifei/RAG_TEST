"""从最终 chunks 聚合非敏感结构报告。"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence

from rag_app.core.models import (
    Chunk,
    ChunkingPolicy,
    ChunkingReport,
    DocumentIR,
    DocumentNode,
    NodeKind,
    SourceSpanKind,
)


def build_chunking_report(
    chunks: Sequence[Chunk],
    document_ir: DocumentIR,
    policy: ChunkingPolicy,
    *,
    elapsed_seconds: float,
) -> ChunkingReport:
    """聚合覆盖、token、表格、列表、orphan 和 slot 上限。

    Args:
        chunks: 已通过最终 validator 的 chunks。
        document_ir: 当前来源 IR。
        policy: required slots 与 provisional 参数。
        elapsed_seconds: 不进入确定性快照的耗时。

    Returns:
        不含正文或 secret 的 ChunkingReport。

    """
    role_counts = Counter(chunk.role.value for chunk in chunks)
    token_counts = sorted(chunk.token_count for chunk in chunks)
    represented_nodes = {
        span.node_id
        for chunk in chunks
        for span in chunk.source_spans
        if span.node_id is not None
    }
    table_rows = [
        node for node in document_ir.nodes if node.kind is NodeKind.TABLE_ROW
    ]
    table_cells = [
        node for node in document_ir.nodes if node.kind is NodeKind.TABLE_CELL
    ]
    list_nodes = [
        node for node in document_ir.nodes if node.kind is NodeKind.LIST_ITEM
    ]
    represented_rows = _represented_ancestors(
        represented_nodes,
        document_ir,
        NodeKind.TABLE_ROW,
    )
    represented_cells = {
        cell.node_id
        for cell in table_cells
        if cell.parent_node_id in represented_rows
    }
    repeated_chars = sum(
        span.chunk_end_char - span.chunk_start_char
        for chunk in chunks
        for span in chunk.source_spans
        if span.span_type is SourceSpanKind.REPEATED_CONTEXT
    )
    represented_labels = sum(
        1
        for chunk in chunks
        for span in chunk.source_spans
        if span.span_type is SourceSpanKind.DERIVED_NUMBERING
    )
    slot_limits = dict(policy.max_embedding_tokens_by_slot)
    over_by_slot = tuple(
        (
            slot_id,
            sum(chunk.token_count > limit for chunk in chunks),
        )
        for slot_id, limit in policy.max_embedding_tokens_by_slot
    )
    warnings = _warnings(document_ir, policy)
    coverage = _source_coverage(chunks, document_ir, policy)
    cross_sections = sum(
        _crosses_sections(chunk, document_ir) for chunk in chunks
    )
    cross_groups = sum(_crosses_groups(chunk, document_ir) for chunk in chunks)
    group_ids = {chunk.neighbor_group_id for chunk in chunks}
    node_ids = {node.node_id for node in document_ir.nodes}
    missing_child_groups = sum(
        group_id not in group_ids
        for chunk in chunks
        for group_id in chunk.child_group_ids
    )
    missing_note_refs = sum(
        note_id not in node_ids
        for chunk in chunks
        for note_id in chunk.note_refs
    )
    orphan_relations = sum(
        relationship.source_node_id not in represented_nodes
        or relationship.target_node_id not in represented_nodes
        for relationship in document_ir.relationships
    )
    return ChunkingReport(
        node_count=len(document_ir.nodes),
        represented_node_count=len(represented_nodes),
        chunk_count=len(chunks),
        chunk_count_by_role=tuple(sorted(role_counts.items())),
        token_p50=_percentile(token_counts, 0.50),
        token_p95=_percentile(token_counts, 0.95),
        token_max=max(token_counts, default=0),
        exact_token_count=sum(
            not chunk.token_count_is_estimate for chunk in chunks
        ),
        estimated_token_count=sum(
            chunk.token_count_is_estimate for chunk in chunks
        ),
        oversize_violations=sum(
            chunk.token_count
            > min(policy.hard_max_tokens, policy.effective_embedding_max)
            for chunk in chunks
        ),
        cross_boundary_violations=cross_sections + cross_groups,
        cross_section_violations=cross_sections,
        cross_group_violations=cross_groups,
        total_citable_source_chars=coverage[0],
        unique_covered_source_chars=coverage[1],
        missing_source_chars=coverage[2],
        source_span_coverage=coverage[3],
        duplicated_citable_chars=coverage[4],
        repeated_context_chars=repeated_chars,
        table_row_count=len(table_rows),
        represented_table_row_count=len(represented_rows),
        table_cell_count=len(table_cells),
        represented_table_cell_count=len(represented_cells),
        list_label_count=sum(
            node.list_attributes is not None
            and bool(node.list_attributes.marker)
            for node in list_nodes
        ),
        represented_list_label_count=represented_labels,
        orphan_note_count=sum(
            dict(chunk.metadata).get("orphan") is True
            for chunk in chunks
            if chunk.role.value == "note"
        ),
        orphan_image_count=sum(
            node.kind is NodeKind.IMAGE
            and node.node_id not in represented_nodes
            for node in document_ir.nodes
        ),
        orphan_relation_count=orphan_relations,
        missing_child_group_count=missing_child_groups,
        missing_note_ref_count=missing_note_refs,
        stable_id_duplicate_count=len(chunks)
        - len({chunk.chunk_id for chunk in chunks}),
        required_embedding_slots=policy.required_embedding_slots,
        max_embedding_tokens_by_slot=tuple(slot_limits.items()),
        chunks_over_limit_by_slot=over_by_slot,
        warnings=warnings,
        elapsed_seconds=elapsed_seconds,
    )


def _source_coverage(
    chunks: Sequence[Chunk],
    document_ir: DocumentIR,
    policy: ChunkingPolicy,
) -> tuple[int, int, int, float, int]:
    eligible = {
        node.node_id: len(node.text_payload.exact_text)
        for node in document_ir.nodes
        if node.text_payload is not None
        and node.text_payload.exact_text
        and _is_citable_node(node, policy)
    }
    intervals: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    referenced_chars = 0
    for chunk in chunks:
        for span in chunk.source_spans:
            if (
                span.node_id not in eligible
                or span.source_start_char is None
                or span.source_end_char is None
                or not span.is_citable
            ):
                continue
            start = max(0, span.source_start_char)
            end = min(eligible[span.node_id], span.source_end_char)
            if end <= start:
                continue
            intervals[span.node_id].append((start, end))
            referenced_chars += end - start
    unique = sum(_merged_length(values) for values in intervals.values())
    total = sum(eligible.values())
    missing = max(0, total - unique)
    ratio = 1.0 if total == 0 else unique / total
    return total, unique, missing, ratio, max(0, referenced_chars - unique)


def _is_citable_node(node: DocumentNode, policy: ChunkingPolicy) -> bool:
    if (
        node.kind is NodeKind.COMMENT
        and policy.comments_policy == "metadata_only"
    ):
        return False
    return not (
        node.anchor.story_kind.value in {"header", "footer"}
        and policy.header_footer_policy == "metadata_only"
    )


def _merged_length(intervals: Sequence[tuple[int, int]]) -> int:
    total = 0
    current_start = -1
    current_end = -1
    for start, end in sorted(intervals):
        if start > current_end:
            total += max(0, current_end - current_start)
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    return total + max(0, current_end - current_start)


def _crosses_sections(chunk: Chunk, document_ir: DocumentIR) -> bool:
    nodes = {node.node_id: node for node in document_ir.nodes}
    sections = {
        nodes[span.node_id].anchor.section_index
        for span in chunk.source_spans
        if span.node_id in nodes
        and nodes[span.node_id].anchor.section_index is not None
    }
    return len(sections) > 1


def _crosses_groups(chunk: Chunk, document_ir: DocumentIR) -> bool:
    nodes = {node.node_id: node for node in document_ir.nodes}
    groups = {
        _source_group(span.node_id, nodes)
        for span in chunk.source_spans
        if span.node_id in nodes
    }
    return len(groups) > 1


def _source_group(
    node_id: str,
    nodes: dict[str, DocumentNode],
) -> tuple[str, str, str]:
    node = nodes[node_id]
    current = node
    while current.parent_node_id is not None:
        parent = nodes[current.parent_node_id]
        if parent.kind in {NodeKind.TABLE, NodeKind.NOTE, NodeKind.COMMENT}:
            return (
                node.anchor.part_uri,
                node.anchor.story_kind.value,
                parent.node_id,
            )
        current = parent
    return (
        node.anchor.part_uri,
        node.anchor.story_kind.value,
        "root",
    )


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    index = max(0, math.ceil(len(values) * percentile) - 1)
    return values[index]


def _represented_ancestors(
    represented: set[str],
    document_ir: DocumentIR,
    kind: NodeKind,
) -> set[str]:
    nodes = {node.node_id: node for node in document_ir.nodes}
    ancestors: set[str] = set()
    for node_id in represented:
        current_id: str | None = node_id
        while current_id is not None:
            current = nodes[current_id]
            if current.kind is kind:
                ancestors.add(current.node_id)
                break
            current_id = current.parent_node_id
    return ancestors


def _warnings(
    document_ir: DocumentIR,
    policy: ChunkingPolicy,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if policy.provisional:
        warnings.append("PROVISIONAL_CHUNKING_PARAMETERS")
    if policy.header_footer_policy == "metadata_only" and any(
        node.anchor.story_kind.value in {"header", "footer"}
        and node.text.strip()
        for node in document_ir.nodes
    ):
        warnings.append("HEADER_FOOTER_METADATA_ONLY")
    if policy.comments_policy == "metadata_only" and any(
        node.kind is NodeKind.COMMENT for node in document_ir.nodes
    ):
        warnings.append("COMMENTS_METADATA_ONLY")
    return tuple(warnings)
