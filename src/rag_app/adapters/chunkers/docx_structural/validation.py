"""Chunk V3 来源、quote、token 和 neighbor 不变量。"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from rag_app.core.models import (
    Chunk,
    ChunkingPolicy,
    DocumentIR,
    DocumentNode,
    SourceSpan,
    SourceSpanKind,
)
from rag_app.core.ports import TokenCounterPort


def validate_chunks(
    chunks: Sequence[Chunk],
    document_ir: DocumentIR,
    policy: ChunkingPolicy,
    token_counter: TokenCounterPort,
) -> None:
    """复算最终文本并校验来源和 neighbor 图。

    Args:
        chunks: 已完成 ID 和双向链接的 chunks。
        document_ir: 来源节点表。
        policy: hard max 与 required slots 合同。
        token_counter: 与 Chunk 记录相同身份的计数端口。

    Returns:
        无返回值。

    Raises:
        ValueError: 任一来源、token、链接或图不变量不成立。

    """
    nodes = {node.node_id: node for node in document_ir.nodes}
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    if len(by_id) != len(chunks):
        raise ValueError("稳定 chunk ID 禁止重复。")
    hard_limit = min(policy.hard_max_tokens, policy.effective_embedding_max)
    for chunk in chunks:
        if token_counter.count(chunk.citation_text).count > hard_limit:
            raise ValueError("citation_text 超过有效 hard max。")
        if token_counter.count(chunk.embedding_text).count > hard_limit:
            raise ValueError("embedding_text 超过有效 hard max。")
        if chunk.tokenizer_id != token_counter.tokenizer_id:
            raise ValueError(
                "Chunk tokenizer identity 与最终 validator 不一致。"
            )
        _validate_sources(chunk, nodes)
        _validate_link(chunk, by_id, previous=True)
        _validate_link(chunk, by_id, previous=False)
    _validate_no_neighbor_cycles(chunks, by_id)


def quote_is_publishable(chunk: Chunk, start: int, end: int) -> bool:
    """判断 quote 是否完整落在可引用且无歧义的来源中。

    Args:
        chunk: quote 所属 Chunk。
        start: quote 在 citation_text 中的起点。
        end: quote 在 citation_text 中的终点。

    Returns:
        单一可引用 span，或同一来源连续 spans 完整覆盖时返回 True。

    """
    if not 0 <= start < end <= len(chunk.citation_text):
        return False
    spans = tuple(
        span
        for span in chunk.source_spans
        if span.chunk_start_char < end and span.chunk_end_char > start
    )
    if not spans or any(not span.is_citable for span in spans):
        return False
    if spans[0].chunk_start_char > start or spans[-1].chunk_end_char < end:
        return False
    node_ids = {span.node_id for span in spans}
    if len(node_ids) != 1:
        return False
    return all(
        _source_contiguous(left, right) for left, right in pairwise(spans)
    )


def _validate_sources(
    chunk: Chunk,
    nodes: dict[str, DocumentNode],
) -> None:
    for span in chunk.source_spans:
        if span.span_type is SourceSpanKind.SEPARATOR:
            continue
        if span.node_id not in nodes:
            raise ValueError("source span 指向 Document IR 之外的节点。")
        if span.span_type is SourceSpanKind.DERIVED_NUMBERING:
            continue
        node = nodes[span.node_id]
        source_text = _node_source_text(node)
        source_start = span.source_start_char
        source_end = span.source_end_char
        if source_start is None or source_end is None:
            raise ValueError("原文来源范围缺失。")
        expected = source_text[source_start:source_end]
        observed = chunk.citation_text[
            span.chunk_start_char : span.chunk_end_char
        ]
        if observed != expected:
            raise ValueError("citation 字符无法从 SourceSpan 逐字重建。")


def _node_source_text(node: DocumentNode) -> str:
    if node.text_payload is not None:
        return node.text_payload.exact_text
    if node.image_attributes is not None:
        return (
            node.image_attributes.alt_text
            or node.image_attributes.display_name
            or ""
        )
    return ""


def _validate_link(
    chunk: Chunk,
    chunks: dict[str, Chunk],
    *,
    previous: bool,
) -> None:
    linked_id = chunk.previous_chunk_id if previous else chunk.next_chunk_id
    if linked_id is None:
        return
    linked = chunks.get(linked_id)
    if linked is None:
        raise ValueError("neighbor link 指向不存在的 Chunk。")
    if (
        linked.version != chunk.version
        or linked.neighbor_group_id != chunk.neighbor_group_id
    ):
        raise ValueError("neighbor link 禁止跨 document version 或 group。")
    reverse = linked.next_chunk_id if previous else linked.previous_chunk_id
    if reverse != chunk.chunk_id:
        raise ValueError("neighbor link 必须双向一致。")


def _validate_no_neighbor_cycles(
    chunks: Sequence[Chunk],
    by_id: dict[str, Chunk],
) -> None:
    for start in chunks:
        visited: set[str] = set()
        current: Chunk | None = start
        while current is not None:
            if current.chunk_id in visited:
                raise ValueError("neighbor next 链禁止成环。")
            visited.add(current.chunk_id)
            current = (
                by_id[current.next_chunk_id]
                if current.next_chunk_id is not None
                else None
            )


def _source_contiguous(left: SourceSpan, right: SourceSpan) -> bool:
    if left.node_id != right.node_id:
        return False
    if left.chunk_end_char != right.chunk_start_char:
        return False
    if left.source_end_char is None or right.source_start_char is None:
        return (
            left.span_type
            is right.span_type
            is SourceSpanKind.DERIVED_NUMBERING
        )
    return left.source_end_char == right.source_start_char
