"""按 section、run、atomic unit 和 source span 生成确定性分块。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Protocol, cast

from tokenizers import Tokenizer

from rag_app.contracts import (
    Chunk,
    ChunkIdentity,
    ChunkRole,
    ChunkSourceSpan,
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
    OcrState,
    stable_chunk_id,
)

__all__ = [
    "Chunker",
    "ChunkerConfig",
    "HuggingFaceTokenCounter",
    "TokenCounter",
    "Utf8TokenCounter",
]

_MISSING_METADATA = object()
_TAIL_GROUP_COUNT = 2


class TokenCounter(Protocol):
    """模型相关 token 计数器接口。"""

    def count(self, text: str) -> int:
        """计算文本 token 数。

        Args:
            text: 待计数文本。

        Returns:
            非负 token 数。

        """
        ...


class Utf8TokenCounter:
    """用 UTF-8 字节数提供确定性的保守计数。"""

    def count(self, text: str) -> int:
        """计算 UTF-8 字节数。

        Args:
            text: 待计数文本。

        Returns:
            UTF-8 编码后的字节数。

        """
        return len(text.encode("utf-8"))


class HuggingFaceTokenCounter:
    """从本地 tokenizer.json 精确计算模型 token 数。"""

    def __init__(self, tokenizer_path: Path) -> None:
        """加载只读本地 tokenizer。

        Args:
            tokenizer_path: 已固化并校验的 tokenizer.json。

        Returns:
            无返回值。

        """
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))

    def count(self, text: str) -> int:
        """计算不含额外 special token 的 token 数。

        Args:
            text: 待计数文本。

        Returns:
            tokenizer 生成的 token 数。

        """
        return len(
            self._tokenizer.encode(
                text,
                add_special_tokens=False,
            ).ids
        )


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    """仅作为 provisional candidate 的分块参数。"""

    target_tokens: int
    hard_max_tokens: int
    overlap_tokens: int

    def __post_init__(self) -> None:
        """校验 target、hard max 与 overlap 边界。

        Args:
            无参数。

        Returns:
            无返回值。

        Raises:
            ValueError: 参数不能保证严格前进或正 token 上限。

        """
        if self.target_tokens <= 0:
            raise ValueError("target_tokens 必须为正数。")
        if self.hard_max_tokens < self.target_tokens:
            raise ValueError("hard_max_tokens 不能小于 target_tokens。")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ValueError("overlap_tokens 必须非负且小于 target_tokens。")


@dataclass(frozen=True, slots=True)
class _Section:
    section_id: str
    heading_path: tuple[str, ...]
    heading_index: int | None
    elements: tuple[Element, ...]


@dataclass(frozen=True, slots=True)
class _Run:
    section: _Section
    group_id: str
    role: ChunkRole
    elements: tuple[Element, ...]


@dataclass(frozen=True, slots=True)
class _Piece:
    element: Element
    text: str
    source_start: int
    source_end: int
    forced: bool = False


@dataclass(frozen=True, slots=True)
class _Draft:
    text: str
    spans: tuple[ChunkSourceSpan, ...]
    repeats_header: bool = False


class Chunker:
    """实现 section-pack-v2-provisional 的确定性生产分块器。"""

    def __init__(
        self,
        config: ChunkerConfig,
        token_counter: TokenCounter,
        *,
        pipeline_fingerprint: str,
    ) -> None:
        """保存候选参数、tokenizer 和 pipeline 指纹。

        Args:
            config: 待真实检索消融的 provisional 参数。
            token_counter: 与 embedding 模型一致的 token 计数器。
            pipeline_fingerprint: 当前索引 pipeline 指纹。

        Returns:
            无返回值。

        """
        self._config = config
        self._tokens = token_counter
        self._pipeline_fingerprint = pipeline_fingerprint

    def chunk(
        self,
        source_id: str,
        doc_version: str,
        elements: list[Element],
        *,
        metadata: DocumentMetadata | object = _MISSING_METADATA,
    ) -> list[Chunk]:
        """把有序元素转换为不跨 section/run 的可追溯 chunks。

        Args:
            source_id: manifest 持久保存的来源标识。
            doc_version: 当前不可变文档版本。
            elements: 按原文顺序排列的解析元素。
            metadata: 已从 corpus policy 显式解析的文档元数据。

        Returns:
            标题不成块、表格/OCR 隔离且邻居不跨 group 的 chunks。

        Raises:
            TypeError: 未显式提供完整 DocumentMetadata。
            ValueError: 元素结构、token 上限或 source span 无法满足契约。

        """
        if not isinstance(metadata, DocumentMetadata):
            raise TypeError("metadata 必须显式提供完整 DocumentMetadata。")
        chunks: list[Chunk] = []
        for section in _sections(source_id, elements):
            for run in _runs(source_id, section):
                drafts = self._drafts(run)
                group_chunks = self._finalize(
                    source_id,
                    doc_version,
                    run,
                    drafts,
                    metadata,
                )
                chunks.extend(_link_group(group_chunks))
        return chunks

    def _drafts(self, run: _Run) -> tuple[_Draft, ...]:
        if run.role == ChunkRole.TEXT:
            return self._text_drafts(run.elements)
        if run.role == ChunkRole.TABLE:
            return self._table_drafts(run.elements[0])
        return self._ocr_drafts(run.elements[0])

    def _text_drafts(self, elements: tuple[Element, ...]) -> tuple[_Draft, ...]:
        pieces: list[_Piece] = []
        for element in elements:
            if self._tokens.count(element.text) <= self._config.hard_max_tokens:
                pieces.append(
                    _Piece(element, element.text, 0, len(element.text))
                )
                continue
            for start, end in self._semantic_segments(element.text):
                pieces.append(
                    _Piece(
                        element,
                        element.text[start:end],
                        start,
                        end,
                        forced=True,
                    )
                )
        drafts: list[_Draft] = []
        packable: list[_Piece] = []
        for piece in pieces:
            if not piece.forced:
                packable.append(piece)
                continue
            drafts.extend(self._pack_text(packable))
            packable = []
            drafts.append(_draft((piece,), _text_separator))
        drafts.extend(self._pack_text(packable))
        return tuple(drafts)

    def _pack_text(self, pieces: list[_Piece]) -> tuple[_Draft, ...]:
        return tuple(
            _draft(group, _text_separator)
            for group in self._pack(
                pieces,
                lambda group: _render(group, _text_separator),
            )
        )

    def _table_drafts(self, element: Element) -> tuple[_Draft, ...]:
        rows = _lines(element.text)
        if not rows:
            return ()
        header_text, header_start, header_end = rows[0]
        header = _Piece(
            element,
            header_text,
            header_start,
            header_end,
        )
        if len(rows) == 1:
            if self._tokens.count(header.text) <= self._config.hard_max_tokens:
                return (_draft((header,), _newline_separator),)
            return tuple(
                _draft(
                    (
                        _Piece(
                            element,
                            element.text[start:end],
                            start,
                            end,
                            forced=True,
                        ),
                    ),
                    _newline_separator,
                )
                for start, end in self._semantic_segments(element.text)
            )
        normal_rows: list[_Piece] = []
        drafts: list[_Draft] = []
        for row_text, row_start, row_end in rows[1:]:
            row = _Piece(element, row_text, row_start, row_end)
            if self._tokens.count(f"{header.text}\n{row.text}") <= (
                self._config.hard_max_tokens
            ):
                normal_rows.append(row)
                continue
            drafts.extend(self._pack_table_rows(header, normal_rows))
            normal_rows = []
            drafts.extend(self._split_long_table_row(header, row))
        drafts.extend(self._pack_table_rows(header, normal_rows))
        return tuple(drafts)

    def _pack_table_rows(
        self,
        header: _Piece,
        rows: list[_Piece],
    ) -> tuple[_Draft, ...]:
        groups = self._pack(
            rows,
            lambda group: _render((header, *group), _newline_separator),
        )
        return tuple(
            _Draft(
                text=_render((header, *group), _newline_separator),
                spans=_spans((header, *group), _newline_separator),
                repeats_header=True,
            )
            for group in groups
        )

    def _split_long_table_row(
        self,
        header: _Piece,
        row: _Piece,
    ) -> tuple[_Draft, ...]:
        prefix = f"{header.text}\n"
        cell_boundaries = tuple(
            index + 3
            for index in _find_all(row.text, " | ")
        )
        segments = self._semantic_segments(
            row.text,
            prefix=prefix,
            primary_boundaries=cell_boundaries,
        )
        drafts = []
        for start, end in segments:
            piece = _Piece(
                row.element,
                row.text[start:end],
                row.source_start + start,
                row.source_start + end,
                forced=True,
            )
            drafts.append(
                _Draft(
                    text=_render((header, piece), _newline_separator),
                    spans=_spans((header, piece), _newline_separator),
                    repeats_header=True,
                )
            )
        return tuple(drafts)

    def _ocr_drafts(self, element: Element) -> tuple[_Draft, ...]:
        lines = [
            _Piece(element, text, start, end)
            for text, start, end in _lines(element.text)
        ]
        pieces: list[_Piece] = []
        for line in lines:
            if self._tokens.count(line.text) <= self._config.hard_max_tokens:
                pieces.append(line)
                continue
            for start, end in self._semantic_segments(line.text):
                pieces.append(
                    _Piece(
                        element,
                        line.text[start:end],
                        line.source_start + start,
                        line.source_start + end,
                        forced=True,
                    )
                )
        drafts: list[_Draft] = []
        packable: list[_Piece] = []
        for piece in pieces:
            if not piece.forced:
                packable.append(piece)
                continue
            drafts.extend(self._pack_lines(packable))
            packable = []
            drafts.append(_draft((piece,), _newline_separator))
        drafts.extend(self._pack_lines(packable))
        return tuple(drafts)

    def _pack_lines(self, pieces: list[_Piece]) -> tuple[_Draft, ...]:
        return tuple(
            _draft(group, _newline_separator)
            for group in self._pack(
                pieces,
                lambda group: _render(group, _newline_separator),
            )
        )

    def _pack(
        self,
        pieces: list[_Piece],
        render: Callable[[tuple[_Piece, ...]], str],
    ) -> tuple[tuple[_Piece, ...], ...]:
        if not pieces:
            return ()
        groups: list[tuple[_Piece, ...]] = []
        current: tuple[_Piece, ...] = (pieces[0],)
        for piece in pieces[1:]:
            candidate = (*current, piece)
            current_tokens = self._tokens.count(render(current))
            candidate_tokens = self._tokens.count(render(candidate))
            if (
                candidate_tokens <= self._config.hard_max_tokens
                and abs(candidate_tokens - self._config.target_tokens)
                < abs(current_tokens - self._config.target_tokens)
            ):
                current = candidate
            else:
                groups.append(current)
                current = (piece,)
        groups.append(current)
        if len(groups) >= _TAIL_GROUP_COUNT:
            merged = (*groups[-2], *groups[-1])
            if self._tokens.count(render(merged)) <= (
                self._config.hard_max_tokens
            ):
                groups[-2:] = [merged]
        return tuple(groups)

    def _semantic_segments(
        self,
        text: str,
        *,
        prefix: str = "",
        primary_boundaries: tuple[int, ...] = (),
    ) -> tuple[tuple[int, int], ...]:
        segments: list[tuple[int, int]] = []
        start = 0
        while start < len(text):
            if self._tokens.count(f"{prefix}{text[start:]}") <= (
                self._config.hard_max_tokens
            ):
                segments.append((start, len(text)))
                break
            end = self._semantic_end(
                text,
                start,
                prefix=prefix,
                primary_boundaries=primary_boundaries,
            )
            if end <= start:
                raise ValueError("长原子切分未严格前进。")
            segments.append((start, end))
            overlap_start = _semantic_overlap_start(
                text,
                start,
                end,
                self._config.overlap_tokens,
                self._tokens,
            )
            start = overlap_start if overlap_start > start else end
        return tuple(segments)

    def _semantic_end(
        self,
        text: str,
        start: int,
        *,
        prefix: str,
        primary_boundaries: tuple[int, ...],
    ) -> int:
        target_end = _largest_fitting_end(
            text,
            start,
            self._config.target_tokens,
            self._tokens,
            prefix,
        )
        boundary = _preferred_boundary(
            text,
            start,
            target_end,
            primary_boundaries,
        )
        if boundary is not None:
            return boundary
        hard_end = _largest_fitting_end(
            text,
            start,
            self._config.hard_max_tokens,
            self._tokens,
            prefix,
        )
        boundary = _preferred_boundary(
            text,
            start,
            hard_end,
            primary_boundaries,
        )
        return hard_end if boundary is None else boundary

    def _finalize(
        self,
        source_id: str,
        doc_version: str,
        run: _Run,
        drafts: tuple[_Draft, ...],
        metadata: DocumentMetadata,
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        for segment_index, draft in enumerate(drafts, start=1):
            spans = tuple(
                span.model_copy(
                    update={
                        "locator": span.locator.model_copy(
                            update={"segment_index": segment_index}
                        ),
                        "is_repeated": (
                            draft.repeats_header
                            and segment_index > 1
                            and span_index == 0
                        ),
                    }
                )
                for span_index, span in enumerate(draft.spans)
            )
            locators = _unique_locators(spans)
            chunk_id = stable_chunk_id(
                source_id,
                ChunkIdentity(
                    section_id=run.section.section_id,
                    neighbor_group_id=run.group_id,
                    chunk_role=run.role,
                    source_spans=spans,
                ),
                draft.text,
            )
            is_ocr = run.role == ChunkRole.OCR
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    source_id=source_id,
                    doc_version=doc_version,
                    pipeline_fingerprint=self._pipeline_fingerprint,
                    section_id=run.section.section_id,
                    neighbor_group_id=run.group_id,
                    chunk_role=run.role,
                    source_spans=spans,
                    text=draft.text,
                    embedding_text=_embedding_text(
                        run.section.heading_path,
                        draft.text,
                    ),
                    element_kind={
                        ChunkRole.TEXT: ElementKind.PARAGRAPH,
                        ChunkRole.TABLE: ElementKind.TABLE,
                        ChunkRole.OCR: ElementKind.IMAGE,
                    }[run.role],
                    locators=locators,
                    content_sha256=hashlib.sha256(
                        draft.text.encode("utf-8")
                    ).hexdigest(),
                    document_status=metadata.document_status,
                    authority_level=metadata.authority_level,
                    effective_from=cast(
                        datetime | None,
                        _rfc3339_input(metadata.effective_from),
                    ),
                    effective_to=cast(
                        datetime | None,
                        _rfc3339_input(metadata.effective_to),
                    ),
                    contains_ocr=is_ocr,
                    minimum_ocr_confidence=(
                        run.elements[0].ocr_confidence if is_ocr else None
                    ),
                )
            )
        return chunks


def _sections(source_id: str, elements: list[Element]) -> tuple[_Section, ...]:
    sections: list[_Section] = []
    heading_path: tuple[str, ...] = ()
    heading_index: int | None = None
    current: list[Element] = []

    def append_current() -> None:
        """把当前非空 section 原子追加到结果。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        if not current:
            return
        sections.append(
            _Section(
                section_id=_stable_id(
                    "section",
                    (
                        source_id,
                        "" if heading_index is None else str(heading_index),
                        *heading_path,
                    ),
                ),
                heading_path=heading_path,
                heading_index=heading_index,
                elements=tuple(current),
            )
        )

    for element in elements:
        if element.kind != ElementKind.HEADING:
            current.append(element)
            continue
        append_current()
        current = []
        if element.locator.heading_index is None:
            raise ValueError("标题元素缺少 heading_index。")
        heading_path = element.locator.heading_path
        heading_index = element.locator.heading_index
    append_current()
    return tuple(sections)


def _runs(source_id: str, section: _Section) -> tuple[_Run, ...]:
    runs: list[_Run] = []
    text_elements: list[Element] = []
    run_index = 0

    def append_run(role: ChunkRole, elements: tuple[Element, ...]) -> None:
        """追加一个非空且身份稳定的逻辑 run。

        Args:
            role: TEXT、TABLE 或 OCR 结构角色。
            elements: 当前 run 的有序原始元素。

        Returns:
            无返回值。

        """
        nonlocal run_index
        if not elements:
            return
        run_index += 1
        runs.append(
            _Run(
                section=section,
                group_id=_stable_id(
                    "group",
                    (
                        source_id,
                        section.section_id,
                        role.value,
                        str(run_index),
                        *(element.element_id for element in elements),
                    ),
                ),
                role=role,
                elements=elements,
            )
        )

    def flush_text() -> None:
        """结束当前连续正文 run。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        nonlocal text_elements
        append_run(ChunkRole.TEXT, tuple(text_elements))
        text_elements = []

    for element in section.elements:
        if element.kind == ElementKind.PARAGRAPH:
            if element.text.strip():
                text_elements.append(element)
            continue
        flush_text()
        if element.kind == ElementKind.TABLE and element.text.strip():
            append_run(ChunkRole.TABLE, (element,))
        elif element.kind == ElementKind.IMAGE and _is_ocr_evidence(element):
            append_run(ChunkRole.OCR, (element,))
    flush_text()
    return tuple(runs)


def _draft(
    pieces: tuple[_Piece, ...],
    separator: Callable[[_Piece, _Piece], str],
) -> _Draft:
    return _Draft(
        text=_render(pieces, separator),
        spans=_spans(pieces, separator),
    )


def _render(
    pieces: tuple[_Piece, ...],
    separator: Callable[[_Piece, _Piece], str],
) -> str:
    if not pieces:
        return ""
    parts = [pieces[0].text]
    for previous, current in pairwise(pieces):
        parts.extend((separator(previous, current), current.text))
    return "".join(parts)


def _spans(
    pieces: tuple[_Piece, ...],
    separator: Callable[[_Piece, _Piece], str],
) -> tuple[ChunkSourceSpan, ...]:
    spans: list[ChunkSourceSpan] = []
    cursor = 0
    for index, piece in enumerate(pieces):
        if index:
            cursor += len(separator(pieces[index - 1], piece))
        end = cursor + len(piece.text)
        spans.append(
            ChunkSourceSpan(
                element_id=piece.element.element_id,
                locator=piece.element.locator,
                start_char=cursor,
                end_char=end,
                source_start_char=piece.source_start,
                source_end_char=piece.source_end,
            )
        )
        cursor = end
    return tuple(spans)


def _text_separator(previous: _Piece, current: _Piece) -> str:
    if (
        previous.element.list_level is not None
        and current.element.list_level is not None
    ):
        return "\n"
    return "\n\n"


def _newline_separator(previous: _Piece, current: _Piece) -> str:
    del previous, current
    return "\n"


def _preferred_boundary(
    text: str,
    start: int,
    limit: int,
    primary: tuple[int, ...],
) -> int | None:
    priorities = (
        tuple(index for index in primary if start < index <= limit),
        tuple(index + 2 for index in _find_all(text, "\n\n")),
        tuple(index + 1 for index in _find_all(text, "\n")),
        _character_boundaries(text, "。！？!?."),
        _character_boundaries(text, "；;"),
        _character_boundaries(text, "，、：,:"),
        tuple(
            index + 1
            for index, character in enumerate(text)
            if character.isspace()
        ),
    )
    for boundaries in priorities:
        candidates = [
            index for index in boundaries if start < index <= limit
        ]
        if candidates:
            return max(candidates)
    return None


def _semantic_overlap_start(
    text: str,
    segment_start: int,
    segment_end: int,
    overlap_tokens: int,
    token_counter: TokenCounter,
) -> int:
    if overlap_tokens == 0:
        return segment_end
    candidates = sorted(
        {
            index + 1
            for index, character in enumerate(text)
            if character in "\n。！？!?."
            and segment_start < index + 1 < segment_end
        },
        reverse=True,
    )
    for candidate in candidates:
        if token_counter.count(text[candidate:segment_end]) <= overlap_tokens:
            return candidate
    return segment_end


def _largest_fitting_end(
    text: str,
    start: int,
    max_tokens: int,
    token_counter: TokenCounter,
    prefix: str,
) -> int:
    low = start + 1
    high = len(text)
    best = start
    while low <= high:
        middle = (low + high) // 2
        if token_counter.count(f"{prefix}{text[start:middle]}") <= max_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _lines(text: str) -> tuple[tuple[str, int, int], ...]:
    lines: list[tuple[str, int, int]] = []
    cursor = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        end = cursor + len(line)
        if line:
            lines.append((line, cursor, end))
        cursor += len(raw_line)
    if cursor < len(text):
        lines.append((text[cursor:], cursor, len(text)))
    return tuple(lines)


def _character_boundaries(text: str, characters: str) -> tuple[int, ...]:
    return tuple(
        index + 1
        for index, character in enumerate(text)
        if character in characters
    )


def _find_all(text: str, needle: str) -> tuple[int, ...]:
    indexes: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return tuple(indexes)
        indexes.append(index)
        start = index + 1


def _unique_locators(
    spans: tuple[ChunkSourceSpan, ...],
) -> tuple[Locator, ...]:
    locators: list[Locator] = []
    for span in spans:
        if span.locator not in locators:
            locators.append(span.locator)
    return tuple(locators)


def _link_group(chunks: list[Chunk]) -> list[Chunk]:
    return [
        chunk.model_copy(
            update={
                "previous_chunk_id": (
                    chunks[index - 1].chunk_id if index else None
                ),
                "next_chunk_id": (
                    chunks[index + 1].chunk_id
                    if index + 1 < len(chunks)
                    else None
                ),
            }
        )
        for index, chunk in enumerate(chunks)
    ]


def _embedding_text(heading_path: tuple[str, ...], text: str) -> str:
    heading_context = " > ".join(heading_path)
    return text if not heading_context else f"{heading_context}\n{text}"


def _is_ocr_evidence(element: Element) -> bool:
    return bool(
        element.text.strip()
        and element.ocr_state
        in {OcrState.SUCCEEDED, OcrState.LOW_CONFIDENCE}
    )


def _stable_id(prefix: str, parts: tuple[str, ...]) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _rfc3339_input(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
