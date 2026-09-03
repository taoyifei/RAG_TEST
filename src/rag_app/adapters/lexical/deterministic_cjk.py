"""无网络词典、文档与 Query 对称的 CJK bigram 分析器。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.models.lexical import (
    AnalyzedLexicalDocument,
    AnalyzedLexicalQuery,
)

_TOKEN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]+|"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*",
    flags=re.UNICODE,
)
_CJK = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]+$")


class DeterministicCjkBigramAnalyzer:
    """以有界 NFKC tokenization 生成安全 FTS 派生文本。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.LEXICAL_STORE,
        name="deterministic-cjk-bigram",
        version="2",
        mode=ProviderMode.DETERMINISTIC,
    )

    def __init__(
        self,
        *,
        max_document_characters: int = 100_000,
        max_query_characters: int = 2_048,
        max_cjk_run: int = 128,
        max_document_tokens: int = 20_000,
        max_query_tokens: int = 256,
    ) -> None:
        """保存明确的输入和 token 上限。

        Args:
            max_document_characters: 单个文档字段的字符上限。
            max_query_characters: Query 字符上限。
            max_cjk_run: 单次处理的连续 CJK 窗口上限。
            max_document_tokens: 单个文档字段的 token 上限。
            max_query_tokens: Query 去重后的 token 上限。

        Returns:
            无返回值。

        """
        limits = (
            max_document_characters,
            max_query_characters,
            max_cjk_run,
            max_document_tokens,
            max_query_tokens,
        )
        if any(value <= 0 for value in limits):
            raise ValueError("Lexical Analyzer 上限必须为正数。")
        self._max_document_characters = max_document_characters
        self._max_query_characters = max_query_characters
        self._max_cjk_run = max_cjk_run
        self._max_document_tokens = max_document_tokens
        self._max_query_tokens = max_query_tokens

    def analyze_document(
        self, lexical_text: str
    ) -> AnalyzedLexicalDocument:
        """保留词频地生成文档 FTS token 文本。

        Args:
            lexical_text: 不可修改的 canonical lexical text。

        Returns:
            不可引用的派生索引文本。

        """
        normalized = self._normalize(
            lexical_text, self._max_document_characters
        )
        tokens = tuple(self._tokens(normalized))
        if len(tokens) > self._max_document_tokens:
            raise ValueError("Lexical 文档 token 数超过上限。")
        return AnalyzedLexicalDocument(
            analyzer_id="deterministic-cjk-bigram",
            analyzer_version="2",
            tokens=tokens,
            fts_index_text=" ".join(tokens),
        )

    def analyze_query(self, query: str) -> AnalyzedLexicalQuery:
        """去重 Query token 并保留受控 CJK AND groups。

        Args:
            query: 不可信用户 Query。

        Returns:
            可安全构造参数化 MATCH 的分析结果。

        """
        normalized = self._normalize(query, self._max_query_characters)
        cjk_groups: list[tuple[str, ...]] = []
        identifiers: list[str] = []
        all_tokens: list[str] = []
        for token in _TOKEN.findall(normalized):
            if _CJK.fullmatch(token):
                group = tuple(dict.fromkeys(self._cjk_tokens(token)))
                if group:
                    cjk_groups.append(group)
                    all_tokens.extend(group)
            else:
                identifiers.append(token)
                all_tokens.append(token)
        unique_identifiers = tuple(dict.fromkeys(identifiers))
        unique_groups = tuple(dict.fromkeys(cjk_groups))
        unique_count = len(set(all_tokens))
        if unique_count > self._max_query_tokens:
            raise ValueError("Lexical Query token 数超过上限。")
        return AnalyzedLexicalQuery(
            analyzer_id="deterministic-cjk-bigram",
            analyzer_version="2",
            normalized_query=normalized,
            cjk_groups=unique_groups,
            identifier_tokens=unique_identifiers,
            token_count=unique_count,
        )

    def _normalize(self, value: str, maximum: int) -> str:
        if len(value) > maximum:
            raise ValueError("Lexical Analyzer 输入字符数超过上限。")
        return unicodedata.normalize("NFKC", value).casefold()

    def _tokens(self, normalized: str) -> Iterable[str]:
        for token in _TOKEN.findall(normalized):
            if _CJK.fullmatch(token):
                yield from self._cjk_tokens(token)
            else:
                yield token

    def _cjk_tokens(self, run: str) -> Iterable[str]:
        for segment in _bounded_cjk_segments(run, self._max_cjk_run):
            if len(segment) == 1:
                yield segment
                continue
            yield segment
            for index in range(len(segment) - 1):
                yield segment[index : index + 2]
            yield from segment


def _bounded_cjk_segments(run: str, maximum: int) -> tuple[str, ...]:
    if len(run) <= maximum:
        return (run,)
    segments: list[str] = []
    start = 0
    while start < len(run):
        end = min(len(run), start + maximum)
        segments.append(run[start:end])
        if end == len(run):
            break
        start = end - 1
    return tuple(segments)


__all__ = ["DeterministicCjkBigramAnalyzer"]
