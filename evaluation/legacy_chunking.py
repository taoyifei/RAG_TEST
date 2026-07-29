"""冻结旧版 element-level 行为并生成真实 fixed-token 基线窗口。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.chunking import ChunkerConfig, TokenCounter
from rag_app.contracts import Element, ElementKind, Locator, OcrState

__all__ = [
    "FixedTokenWindow",
    "LegacyElementChunk",
    "fixed_token_windows",
    "legacy_element_chunks",
]


@dataclass(frozen=True, slots=True)
class LegacyElementChunk:
    """仅用于消融对照的旧版单元素分块。"""

    element_id: str
    element_kind: ElementKind
    text: str
    embedding_text: str
    locator: Locator


@dataclass(frozen=True, slots=True)
class FixedTokenWindow:
    """固定 token 基线中的一个真实文本窗口。"""

    text: str
    source_start_char: int
    source_end_char: int
    locators: tuple[Locator, ...]


@dataclass(frozen=True, slots=True)
class _SourcePart:
    locator: Locator
    start_char: int
    end_char: int


def legacy_element_chunks(
    elements: tuple[Element, ...] | list[Element],
    token_counter: TokenCounter,
    config: ChunkerConfig,
) -> tuple[LegacyElementChunk, ...]:
    """按旧版单元素规则生成只用于评估的冻结基线。

    Args:
        elements: 按原文顺序排列的解析元素。
        token_counter: 与候选方案相同的 token 计数器。
        config: legacy 候选的 target、hard max 与 overlap。

    Returns:
        保留标题独立成块及旧版元素内切分行为的不可变结果。

    """
    chunks: list[LegacyElementChunk] = []
    for element in elements:
        if not _is_evidence(element):
            continue
        segments = (
            _legacy_table_segments(element.text, token_counter, config)
            if element.kind == ElementKind.TABLE
            else _legacy_split(element.text, token_counter, config)
        )
        for segment_index, segment in enumerate(segments, start=1):
            locator = element.locator.model_copy(
                update={"segment_index": segment_index}
            )
            chunks.append(
                LegacyElementChunk(
                    element_id=element.element_id,
                    element_kind=element.kind,
                    text=segment,
                    embedding_text=_embedding_text(element, segment),
                    locator=locator,
                )
            )
    return tuple(chunks)


def fixed_token_windows(
    elements: tuple[Element, ...] | list[Element],
    token_counter: TokenCounter,
    *,
    window_tokens: int = 512,
) -> tuple[FixedTokenWindow, ...]:
    """生成有真实文本、locator 与全局字符范围的固定 token 窗口。

    Args:
        elements: 按原文顺序排列的解析元素。
        token_counter: 用于限制每个窗口的 token 计数器。
        window_tokens: 每个窗口允许的最大 token 数。

    Returns:
        无 overlap、完整覆盖拼接证据流的真实窗口。

    Raises:
        ValueError: 窗口上限无效或单字符无法放入窗口。

    """
    if window_tokens <= 0:
        raise ValueError("window_tokens 必须为正数。")
    text, parts = _source_stream(elements)
    if not text:
        return ()
    windows: list[FixedTokenWindow] = []
    start = 0
    while start < len(text):
        end = _largest_fitting_end(
            text,
            start,
            window_tokens,
            token_counter,
        )
        if end <= start:
            raise ValueError("单个字符超过 fixed window token 上限。")
        locators = tuple(
            part.locator
            for part in parts
            if part.start_char < end and part.end_char > start
        )
        windows.append(
            FixedTokenWindow(
                text=text[start:end],
                source_start_char=start,
                source_end_char=end,
                locators=_ordered_unique_locators(locators),
            )
        )
        start = end
    return tuple(windows)


def _source_stream(
    elements: tuple[Element, ...] | list[Element],
) -> tuple[str, tuple[_SourcePart, ...]]:
    evidence = tuple(element for element in elements if _is_evidence(element))
    fragments: list[str] = []
    parts: list[_SourcePart] = []
    cursor = 0
    for index, element in enumerate(evidence):
        if index:
            fragments.append("\n")
            cursor += 1
        start = cursor
        fragments.append(element.text)
        cursor += len(element.text)
        parts.append(
            _SourcePart(
                locator=element.locator,
                start_char=start,
                end_char=cursor,
            )
        )
    return "".join(fragments), tuple(parts)


def _legacy_split(
    text: str,
    token_counter: TokenCounter,
    config: ChunkerConfig,
) -> tuple[str, ...]:
    if token_counter.count(text) <= config.hard_max_tokens:
        return (text,)
    segments: list[str] = []
    start = 0
    while start < len(text):
        end = _largest_fitting_end(
            text,
            start,
            config.target_tokens,
            token_counter,
        )
        if end <= start:
            raise ValueError("单个字符超过 legacy target token 上限。")
        segments.append(text[start:end])
        if end == len(text):
            break
        start = max(
            start + 1,
            _legacy_overlap_start(
                text,
                start,
                end,
                config.overlap_tokens,
                token_counter,
            ),
        )
    return tuple(segments)


def _legacy_table_segments(
    text: str,
    token_counter: TokenCounter,
    config: ChunkerConfig,
) -> tuple[str, ...]:
    rows = text.splitlines()
    if len(rows) <= 1:
        return _legacy_split(text, token_counter, config)
    header = rows[0]
    groups: list[str] = []
    current_rows: list[str] = []
    for row in rows[1:]:
        candidate = "\n".join((header, *current_rows, row))
        candidate_tokens = token_counter.count(candidate)
        if current_rows and candidate_tokens > config.target_tokens:
            groups.append("\n".join((header, *current_rows)))
            current_rows = []
            candidate = "\n".join((header, row))
            candidate_tokens = token_counter.count(candidate)
        if candidate_tokens > config.hard_max_tokens:
            if current_rows:
                groups.append("\n".join((header, *current_rows)))
                current_rows = []
            groups.extend(_legacy_split(candidate, token_counter, config))
            continue
        current_rows.append(row)
    if current_rows:
        groups.append("\n".join((header, *current_rows)))
    return tuple(groups)


def _is_evidence(element: Element) -> bool:
    if not element.text.strip():
        return False
    if element.kind != ElementKind.IMAGE:
        return True
    return element.ocr_state in {
        OcrState.SUCCEEDED,
        OcrState.LOW_CONFIDENCE,
    }


def _embedding_text(element: Element, text: str) -> str:
    heading_context = " > ".join(element.locator.heading_path)
    if not heading_context or heading_context == text:
        return text
    return f"{heading_context}\n{text}"


def _largest_fitting_end(
    text: str,
    start: int,
    max_tokens: int,
    token_counter: TokenCounter,
) -> int:
    low = start + 1
    high = len(text)
    best = start
    while low <= high:
        middle = (low + high) // 2
        if token_counter.count(text[start:middle]) <= max_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _legacy_overlap_start(
    text: str,
    segment_start: int,
    segment_end: int,
    overlap_tokens: int,
    token_counter: TokenCounter,
) -> int:
    if overlap_tokens == 0:
        return segment_end
    start = segment_end
    while start > segment_start:
        candidate = start - 1
        if token_counter.count(text[candidate:segment_end]) > overlap_tokens:
            break
        start = candidate
    return start


def _ordered_unique_locators(
    locators: tuple[Locator, ...],
) -> tuple[Locator, ...]:
    unique: list[Locator] = []
    keys: set[str] = set()
    for locator in locators:
        key = locator.logical_key()
        if key in keys:
            continue
        keys.add(key)
        unique.append(locator)
    return tuple(unique)
