"""文档与 Query 共用的同步 Lexical Analyzer 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models.lexical import (
    AnalyzedLexicalDocument,
    AnalyzedLexicalQuery,
)


class LexicalAnalyzerPort(Protocol):
    """产生不修改 canonical 文本的内部 FTS 派生表示。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回可进入 Index Fingerprint 的分析器身份。

        Args:
            无参数；读取当前实现的静态身份。

        Returns:
            分析器 Component 描述。

        """
        ...

    def analyze_document(
        self, lexical_text: str
    ) -> AnalyzedLexicalDocument:
        """分析 canonical lexical text。

        Args:
            lexical_text: 不可被修改的 canonical lexical text。

        Returns:
            仅用于 FTS 索引的派生 token 文本。

        """
        ...

    def analyze_query(self, query: str) -> AnalyzedLexicalQuery:
        """使用同一 tokenization 分析不可信 Query。

        Args:
            query: 原始用户 Query。

        Returns:
            有界且不含 FTS 语法的 token groups。

        """
        ...


__all__ = ["LexicalAnalyzerPort"]
