"""从最终 chunks 聚合非敏感结构报告。"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from rag_app.core.models import (
    Chunk,
    ChunkingPolicy,
    ChunkingReport,
    DocumentIR,
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
    represented_cells = _represented_ancestors(
        represented_nodes,
        document_ir,
        NodeKind.TABLE_CELL,
    )
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
        source_span_coverage=1.0 if chunks else 0.0,
        duplicated_citable_chars=repeated_chars,
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
        stable_id_duplicate_count=len(chunks)
        - len({chunk.chunk_id for chunk in chunks}),
        required_embedding_slots=policy.required_embedding_slots,
        max_embedding_tokens_by_slot=tuple(slot_limits.items()),
        chunks_over_limit_by_slot=over_by_slot,
        warnings=warnings,
        elapsed_seconds=elapsed_seconds,
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
