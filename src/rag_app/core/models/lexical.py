"""对称文档与 Query 分析的基础设施无关模型。"""

from __future__ import annotations

from pydantic import Field

from rag_app.core.models.common import FrozenModel


class AnalyzedLexicalDocument(FrozenModel):
    """供 FTS adapter 写入的确定性派生文本。"""

    analyzer_id: str = Field(min_length=1)
    analyzer_version: str = Field(min_length=1)
    tokens: tuple[str, ...]
    fts_index_text: str


class AnalyzedLexicalQuery(FrozenModel):
    """供受控 Query Builder 使用的确定性 token groups。"""

    analyzer_id: str = Field(min_length=1)
    analyzer_version: str = Field(min_length=1)
    normalized_query: str
    cjk_groups: tuple[tuple[str, ...], ...]
    identifier_tokens: tuple[str, ...]
    token_count: int = Field(ge=0)


__all__ = ["AnalyzedLexicalDocument", "AnalyzedLexicalQuery"]
