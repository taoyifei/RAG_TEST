"""超长原子的语义边界拆分、严格前进和完整句 overlap。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import replace
from itertools import pairwise

from rag_app.adapters.chunkers.docx_structural.atoms import (
    AtomicUnit,
    SourceFragment,
)
from rag_app.adapters.chunkers.docx_structural.context import embedding_text
from rag_app.adapters.chunkers.docx_structural.rendering import render_fragments
from rag_app.core.models import ChunkingPolicy, SourceSpanKind
from rag_app.core.ports import TokenCounterPort


def split_atom(
    atom: AtomicUnit,
    *,
    document_title: str,
    policy: ChunkingPolicy,
    token_counter: TokenCounterPort,
) -> tuple[AtomicUnit, ...]:
    """只在原子超限时按最高优先级边界严格拆分。

    Args:
        atom: 待检查的结构原子。
        document_title: embedding-only 文档标题。
        policy: 冻结 token 和 overlap 参数。
        token_counter: 无网络计数端口。

    Returns:
        至少一个保持来源顺序的原子 segment。

    Raises:
        ValueError: 固定上下文已耗尽预算，无法容纳任何字符。

    """
    rendered = render_fragments(atom.fragments)

    def fits(candidate: str, limit: int) -> bool:
        """检查候选正文及其 embedding 上下文是否落在上限内。

        Args:
            candidate: 当前候选 citation 文本。
            limit: 允许的 token 上限。

        Returns:
            citation 与 embedding 都不超限时为 True。

        """
        return _fits(
            atom,
            candidate,
            document_title,
            limit,
            token_counter,
        )

    if _fits(
        atom,
        rendered.text,
        document_title,
        policy.hard_max_tokens,
        token_counter,
    ):
        return (atom,)
    segments: list[AtomicUnit] = []
    fresh_start = 0
    segment_start = 0
    segment_index = 0
    while fresh_start < len(rendered.text):
        hard_end = _largest_fitting_end(
            rendered.text,
            segment_start,
            policy.hard_max_tokens,
            fits,
        )
        if hard_end <= fresh_start:
            raise ValueError("embedding prefix 已耗尽 hard max，无法严格前进。")
        target_end = _largest_fitting_end(
            rendered.text,
            segment_start,
            policy.target_tokens,
            fits,
        )
        end = _preferred_boundary(
            rendered.text,
            fresh_start,
            max(fresh_start, target_end),
            hard_end,
            _fragment_boundaries(atom.fragments),
        )
        end = _protect_grapheme_boundary(rendered.text, fresh_start, end)
        if end <= fresh_start:
            end = _protect_grapheme_boundary(
                rendered.text,
                fresh_start,
                hard_end,
            )
        if end <= fresh_start:
            raise ValueError("语义 splitter 无法在 hard max 内严格前进。")
        fragments = _slice_fragments(
            atom.fragments,
            segment_start,
            end,
            repeated_before=fresh_start,
        )
        segments.append(
            replace(
                atom,
                unit_id=f"{atom.unit_id}:segment:{segment_index}",
                fragments=fragments,
                metadata=tuple(
                    sorted(
                        (
                            *atom.metadata,
                            ("segment_index", segment_index),
                            ("segment_source_start", segment_start),
                            ("segment_source_end", end),
                        ),
                        key=lambda item: item[0],
                    )
                ),
            )
        )
        fresh_start = end
        segment_start = _semantic_overlap_start(
            rendered.text,
            (segment_start, end),
            policy.overlap_cap_tokens,
            token_counter,
            atom.fragments,
        )
        segment_index += 1
    return tuple(segments)


def _fits(
    atom: AtomicUnit,
    citation_text: str,
    document_title: str,
    limit: int,
    token_counter: TokenCounterPort,
) -> bool:
    citation = token_counter.count(citation_text).count
    embedded = token_counter.count(
        embedding_text(document_title, atom, citation_text)
    ).count
    return citation <= limit and embedded <= limit


def _largest_fitting_end(
    text: str,
    start: int,
    limit: int,
    fits: Callable[[str, int], bool],
) -> int:
    low = start + 1
    high = len(text)
    best = start
    while low <= high:
        middle = (low + high) // 2
        candidate = text[start:middle]
        if fits(candidate, limit):
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _preferred_boundary(
    text: str,
    start: int,
    target_end: int,
    hard_end: int,
    structural: tuple[int, ...],
) -> int:
    priorities = (
        structural,
        _needle_boundaries(text, "\n\n"),
        _needle_boundaries(text, "\n"),
        _character_boundaries(text, "。！？!?."),
        _character_boundaries(text, "；;"),
        _character_boundaries(text, "，、：,:"),
        tuple(index + 1 for index, char in enumerate(text) if char.isspace()),
    )
    for limit in (target_end, hard_end):
        for boundaries in priorities:
            candidates = [
                index for index in boundaries if start < index <= limit
            ]
            if candidates:
                return max(candidates)
    return hard_end


def _semantic_overlap_start(
    text: str,
    segment: tuple[int, int],
    overlap_cap: int,
    token_counter: TokenCounterPort,
    fragments: tuple[SourceFragment, ...],
) -> int:
    segment_start, segment_end = segment
    if overlap_cap == 0 or text[segment_end - 1] not in "\n。！？!?.":
        return segment_end
    starts = [segment_start]
    starts.extend(
        index + 1
        for index in range(segment_start, segment_end - 1)
        if text[index] in "\n。！？!?."
    )
    for candidate in starts:
        if candidate >= segment_end:
            continue
        suffix = text[candidate:segment_end]
        if token_counter.count(suffix).count > overlap_cap:
            continue
        if _range_contains_derived(fragments, candidate, segment_end):
            continue
        return candidate
    return segment_end


def _slice_fragments(
    fragments: tuple[SourceFragment, ...],
    start: int,
    end: int,
    *,
    repeated_before: int,
) -> tuple[SourceFragment, ...]:
    sliced: list[SourceFragment] = []
    cursor = 0
    for fragment in fragments:
        fragment_end = cursor + len(fragment.text)
        left = max(start, cursor)
        right = min(end, fragment_end)
        if left < right:
            boundaries = [left]
            if left < repeated_before < right:
                boundaries.append(repeated_before)
            boundaries.append(right)
            for piece_left, piece_right in pairwise(boundaries):
                local_left = piece_left - cursor
                local_right = piece_right - cursor
                span_type = fragment.span_type
                is_repeated = fragment.is_repeated
                if (
                    piece_right <= repeated_before
                    and span_type is SourceSpanKind.ORIGINAL_TEXT
                ):
                    span_type = SourceSpanKind.REPEATED_CONTEXT
                    is_repeated = True
                source_start = fragment.source_start_char
                source_end = fragment.source_end_char
                if source_start is not None:
                    source_start += local_left
                    source_end = source_start + (local_right - local_left)
                sliced.append(
                    SourceFragment(
                        text=fragment.text[local_left:local_right],
                        span_type=span_type,
                        node_id=fragment.node_id,
                        source_anchor=fragment.source_anchor,
                        source_start_char=source_start,
                        source_end_char=source_end,
                        metadata=fragment.metadata,
                        is_repeated=is_repeated,
                    )
                )
        cursor = fragment_end
    return tuple(sliced)


def _fragment_boundaries(
    fragments: tuple[SourceFragment, ...],
) -> tuple[int, ...]:
    boundaries: list[int] = []
    cursor = 0
    for fragment in fragments:
        cursor += len(fragment.text)
        boundaries.append(cursor)
    return tuple(boundaries)


def _range_contains_derived(
    fragments: tuple[SourceFragment, ...],
    start: int,
    end: int,
) -> bool:
    cursor = 0
    for fragment in fragments:
        fragment_end = cursor + len(fragment.text)
        overlaps = max(start, cursor) < min(end, fragment_end)
        if overlaps and fragment.span_type is SourceSpanKind.DERIVED_NUMBERING:
            return True
        cursor = fragment_end
    return False


def _protect_grapheme_boundary(text: str, start: int, end: int) -> int:
    while end > start and end < len(text):
        next_character = text[end]
        if (
            unicodedata.combining(next_character) == 0
            and next_character != "\u200d"
        ):
            break
        end -= 1
    return end


def _needle_boundaries(text: str, needle: str) -> tuple[int, ...]:
    boundaries: list[int] = []
    offset = 0
    while True:
        index = text.find(needle, offset)
        if index < 0:
            return tuple(boundaries)
        boundaries.append(index + len(needle))
        offset = index + 1


def _character_boundaries(text: str, characters: str) -> tuple[int, ...]:
    return tuple(
        index + 1
        for index, character in enumerate(text)
        if character in characters
    )
