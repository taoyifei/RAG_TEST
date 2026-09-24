"""将 DocumentIR 的真实来源片段映射为 Go 分块器阅读视图。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rag_app.adapters.chunkers.docx_structural.rendering import (
    render_atoms,
    render_fragments,
    separator_fragment,
)
from rag_app.core.models import SourceSpan, SourceSpanKind

if TYPE_CHECKING:
    from rag_app.adapters.chunkers.docx_structural.atoms import RunPlan
    from rag_app.core.models import DocumentNode

_LINE_ENDING = re.compile(r"\r\n|\r")


@dataclass(frozen=True, slots=True)
class ReadingView:
    """单个来源组的规范化文本及对原始 IR 的逐段映射。"""

    text: str
    spans: tuple[SourceSpan, ...]


def build_reading_view(
    run: RunPlan,
    nodes: dict[str, DocumentNode],
) -> ReadingView:
    """保留表格和列表的现有结构渲染，让上游决定分块边界。"""
    prefix = []
    for level, dependency in enumerate(run.context_dependencies, start=1):
        node = nodes.get(dependency.source_node_id)
        if node is None:
            raise ValueError("阅读视图标题来源节点不存在。")
        if (
            node.text_payload is None
            or not node.text_payload.exact_text.strip()
        ):
            raise ValueError("阅读视图标题来源无文本。")
        # 标题只作为检索上下文，不能冒充当前 run 的原生正文。
        prefix.append(
            separator_fragment(
                "#" * min(level, 6)
                + " "
                + node.text_payload.exact_text.strip()
                + "\n"
            )
        )
    heading = render_fragments(tuple(prefix))
    body = render_atoms(run.atoms)
    shifted = tuple(
        span.model_copy(
            update={
                "chunk_start_char": span.chunk_start_char + len(heading.text),
                "chunk_end_char": span.chunk_end_char + len(heading.text),
            }
        )
        for span in body.spans
    )
    return _normalize(
        ReadingView(heading.text + body.text, (*heading.spans, *shifted))
    )


def slice_source_spans(
    view: ReadingView,
    start: int,
    end: int,
) -> tuple[SourceSpan, ...]:
    """按 Go rune 区间切分来源；Python 字符索引采用同一代码点单位。"""
    if not 0 <= start < end <= len(view.text):
        raise ValueError("Go 分块区间超出阅读视图。")
    result: list[SourceSpan] = []
    for span in view.spans:
        clipped_start = max(start, span.chunk_start_char)
        clipped_end = min(end, span.chunk_end_char)
        if clipped_end <= clipped_start:
            continue
        updates: dict[str, object] = {
            "chunk_start_char": clipped_start - start,
            "chunk_end_char": clipped_end - start,
        }
        if span.span_type is SourceSpanKind.DERIVED_NUMBERING and (
            clipped_start != span.chunk_start_char
            or clipped_end != span.chunk_end_char
        ):
            result.append(
                SourceSpan(
                    span_type=SourceSpanKind.SEPARATOR,
                    chunk_start_char=clipped_start - start,
                    chunk_end_char=clipped_end - start,
                    is_citable=False,
                )
            )
            continue
        if span.source_start_char is not None:
            if span.span_type is SourceSpanKind.NORMALIZED_TEXT:
                if (
                    clipped_start != span.chunk_start_char
                    or clipped_end != span.chunk_end_char
                ):
                    raise ValueError("Go 边界拆断规范化字符。")
            else:
                updates["source_start_char"] = (
                    span.source_start_char
                    + clipped_start
                    - span.chunk_start_char
                )
                updates["source_end_char"] = (
                    span.source_start_char + clipped_end - span.chunk_start_char
                )
        result.append(SourceSpan.model_validate(span.model_dump() | updates))
    if (
        not result
        or result[0].chunk_start_char != 0
        or result[-1].chunk_end_char != end - start
    ):
        raise ValueError("分块来源跨度未覆盖完整文本。")
    return tuple(result)


def _normalize(view: ReadingView) -> ReadingView:
    pieces: list[str] = []
    mapped: list[SourceSpan] = []
    cursor = 0
    for span in view.spans:
        original = view.text[span.chunk_start_char : span.chunk_end_char]
        last = 0
        for match in _LINE_ENDING.finditer(original):
            if match.start() > last:
                cursor = _append_piece(
                    pieces,
                    mapped,
                    span,
                    original[last : match.start()],
                    last,
                    match.start(),
                    cursor,
                    normalized=False,
                )
            cursor = _append_piece(
                pieces,
                mapped,
                span,
                "\n",
                match.start(),
                match.end(),
                cursor,
                normalized=True,
            )
            last = match.end()
        if last < len(original):
            cursor = _append_piece(
                pieces,
                mapped,
                span,
                original[last:],
                last,
                len(original),
                cursor,
                normalized=False,
            )
    text = "".join(pieces)
    if len(text) != cursor:
        raise ValueError("阅读视图规范化后的跨度长度不一致。")
    return ReadingView(text, tuple(mapped))


def _append_piece(  # noqa: PLR0913, PLR0917
    pieces: list[str],
    mapped: list[SourceSpan],
    span: SourceSpan,
    text: str,
    source_start_offset: int,
    source_end_offset: int,
    cursor: int,
    *,
    normalized: bool,
) -> int:
    updates: dict[str, object] = {
        "chunk_start_char": cursor,
        "chunk_end_char": cursor + len(text),
    }
    if span.source_start_char is not None:
        updates["source_start_char"] = (
            span.source_start_char + source_start_offset
        )
        updates["source_end_char"] = span.source_start_char + source_end_offset
    if normalized and span.source_start_char is not None:
        updates["span_type"] = SourceSpanKind.NORMALIZED_TEXT
        updates["is_repeated"] = False
        updates["metadata"] = dict(span.metadata) | {
            "normalization": "crlf-to-lf-v1",
            "original_span_type": span.span_type.value,
        }
    elif normalized and span.span_type is SourceSpanKind.DERIVED_NUMBERING:
        updates = {
            "span_type": SourceSpanKind.SEPARATOR,
            "chunk_start_char": cursor,
            "chunk_end_char": cursor + len(text),
            "is_citable": False,
        }
        mapped.append(SourceSpan.model_validate(updates))
        pieces.append(text)
        return cursor + len(text)
    mapped.append(SourceSpan.model_validate(span.model_dump() | updates))
    pieces.append(text)
    return cursor + len(text)
