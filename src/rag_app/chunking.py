"""将解析元素切分为可追溯且有硬 token 上限的分块。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from tokenizers import Tokenizer

from rag_app.contracts import (
    Chunk,
    DocumentMetadata,
    Element,
    ElementKind,
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
    """以 UTF-8 字节数给出不低估上下文占用的保守上界。"""

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
        """加载只读的本地 tokenizer 资产。

        Args:
            tokenizer_path: 已固化并校验的 tokenizer.json 路径。

        Returns:
            无返回值。

        """
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))

    def count(self, text: str) -> int:
        """计算不含额外特殊 token 的模型 token 数。

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
    """必须由冻结评测集确定的分块参数。"""

    target_tokens: int
    hard_max_tokens: int
    overlap_tokens: int

    def __post_init__(self) -> None:
        """校验分块边界。

        Args:
            无参数。

        Returns:
            无返回值。

        Raises:
            ValueError: 参数不能保证分块前进或 token 上限为正。

        """
        if self.target_tokens <= 0:
            raise ValueError("target_tokens 必须为正数。")
        if self.hard_max_tokens < self.target_tokens:
            raise ValueError("hard_max_tokens 不能小于 target_tokens。")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ValueError(
                "overlap_tokens 必须小于 target_tokens 且不能为负。"
            )


class Chunker:
    """按显式配置切分元素，不内置拍脑袋参数。"""

    def __init__(
        self,
        config: ChunkerConfig,
        token_counter: TokenCounter,
        *,
        pipeline_fingerprint: str,
    ) -> None:
        """初始化分块器。

        Args:
            config: 由冻结集确定的分块参数。
            token_counter: 与目标模型一致或不低估的 token 计数器。
            pipeline_fingerprint: 当前解析与索引 pipeline 指纹。

        Returns:
            无返回值。

        """
        self._config = config
        self._token_counter = token_counter
        self._pipeline_fingerprint = pipeline_fingerprint

    def chunk(
        self,
        source_id: str,
        doc_version: str,
        elements: list[Element],
        *,
        metadata: DocumentMetadata | object = _MISSING_METADATA,
    ) -> list[Chunk]:
        """将有文本证据的元素切分成稳定分块。

        Args:
            source_id: manifest 持久保存的来源标识。
            doc_version: 基于内容摘要的当前文档版本。
            elements: 按原文顺序排列的元素。
            metadata: 在创建 chunk 前解析完成的文档级元数据。

        Returns:
            可写入索引的分块；未完成或失败的 OCR 图片不产生证据。

        """
        if not isinstance(metadata, DocumentMetadata):
            raise TypeError("metadata 必须显式提供完整 DocumentMetadata。")
        chunks: list[Chunk] = []
        for element in elements:
            if not _is_evidence(element):
                continue
            for segment_index, segment in enumerate(
                self._segments_for_element(element),
                start=1,
            ):
                locator = element.locator.model_copy(
                    update={"segment_index": segment_index}
                )
                content_sha256 = hashlib.sha256(
                    segment.encode("utf-8")
                ).hexdigest()
                is_ocr = element.kind == ElementKind.IMAGE
                chunks.append(
                    Chunk(
                        chunk_id=stable_chunk_id(
                            source_id,
                            locator,
                            segment,
                        ),
                        source_id=source_id,
                        doc_version=doc_version,
                        pipeline_fingerprint=self._pipeline_fingerprint,
                        text=segment,
                        embedding_text=_embedding_text(element, segment),
                        element_kind=element.kind,
                        locators=(locator,),
                        content_sha256=content_sha256,
                        document_status=(
                            metadata.document_status
                        ),
                        authority_level=(
                            metadata.authority_level
                        ),
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
                            element.ocr_confidence if is_ocr else None
                        ),
                    )
                )
        return _with_neighbors(chunks)

    def _segments_for_element(self, element: Element) -> list[str]:
        if element.kind == ElementKind.TABLE:
            return self._table_segments(element.text)
        return self._split_long_element(element.text)

    def _split_long_element(self, text: str) -> list[str]:
        if self._token_counter.count(text) <= self._config.hard_max_tokens:
            return [text]
        segments: list[str] = []
        start = 0
        while start < len(text):
            end = _largest_fitting_end(
                text,
                start,
                self._config.target_tokens,
                self._token_counter,
            )
            if end <= start:
                raise ValueError(
                    "单个字符超过 target_tokens，无法安全分块。"
                )
            segment = text[start:end]
            segments.append(segment)
            if end == len(text):
                break
            overlap_start = _overlap_start(
                text,
                start,
                end,
                self._config.overlap_tokens,
                self._token_counter,
            )
            start = max(start + 1, overlap_start)
        return segments

    def _table_segments(self, text: str) -> list[str]:
        rows = text.splitlines()
        if len(rows) <= 1:
            return self._split_long_element(text)
        header = rows[0]
        groups: list[str] = []
        current_rows: list[str] = []
        for row in rows[1:]:
            candidate = "\n".join((header, *current_rows, row))
            candidate_tokens = self._token_counter.count(candidate)
            if (
                current_rows
                and candidate_tokens > self._config.target_tokens
            ):
                groups.append("\n".join((header, *current_rows)))
                current_rows = []
                candidate = "\n".join((header, row))
                candidate_tokens = self._token_counter.count(candidate)
            if candidate_tokens > self._config.hard_max_tokens:
                if current_rows:
                    groups.append("\n".join((header, *current_rows)))
                    current_rows = []
                groups.extend(self._split_long_element(candidate))
                continue
            current_rows.append(row)
        if current_rows:
            groups.append("\n".join((header, *current_rows)))
        return groups


def _is_evidence(element: Element) -> bool:
    if not element.text.strip():
        return False
    if element.kind != ElementKind.IMAGE:
        return True
    return element.ocr_state in {
        OcrState.SUCCEEDED,
        OcrState.LOW_CONFIDENCE,
    }


def _embedding_text(element: Element, segment: str) -> str:
    heading_context = " > ".join(element.locator.heading_path)
    if not heading_context or segment == heading_context:
        return segment
    return f"{heading_context}\n{segment}"


def _rfc3339_input(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _with_neighbors(chunks: list[Chunk]) -> list[Chunk]:
    linked: list[Chunk] = []
    for index, chunk in enumerate(chunks):
        previous_id = chunks[index - 1].chunk_id if index > 0 else None
        next_id = (
            chunks[index + 1].chunk_id
            if index + 1 < len(chunks)
            else None
        )
        linked.append(
            chunk.model_copy(
                update={
                    "previous_chunk_id": previous_id,
                    "next_chunk_id": next_id,
                }
            )
        )
    return linked


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


def _overlap_start(
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
