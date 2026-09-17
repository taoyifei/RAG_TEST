"""基于 canonical Chunk 元数据构造只用于重排的确定性上下文。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from rag_app.core.models import RankedChunk

_PREFIX_CHARACTER_LIMIT = 600
_CONTEXT_REVISION = "wb08r-context-v1"


@dataclass(frozen=True, slots=True)
class ContextualTextViews:
    """不改变索引文本和引用正文的候选重排视图。"""

    rerank_text: str
    lexical_text_override: None
    embedding_text_override: None
    context_fields: tuple[str, ...]
    revision: str


def contextual_rerank_text(candidate: RankedChunk) -> ContextualTextViews:
    """复用现有索引，为一次 Chunk 重排补入有界可信元数据。

    Args:
        candidate: 已从活动索引复核的 canonical Chunk。

    Returns:
        仅含重排文本的视图；引用和索引文本均不覆盖。

    """
    chunk = candidate.hydrated.chunk
    metadata = dict(chunk.metadata)
    citation = chunk.citation_text
    lines: list[str] = []
    fields: list[str] = []
    title = _normalized(candidate.hydrated.display_name)[:240]
    if title and title not in _normalized(citation[:_PREFIX_CHARACTER_LIMIT]):
        lines.append(title)
        fields.append("文档")
    headings = tuple(
        dict.fromkeys(
            normalized
            for part in chunk.heading_path[:6]
            if (normalized := _normalized(part))
        )
    )
    heading = " / ".join(headings)
    if heading and heading not in _normalized(
        citation[:_PREFIX_CHARACTER_LIMIT]
    ):
        lines.append(heading)
        fields.append("章节")
    _append_field(
        lines,
        fields,
        label="部门",
        raw=metadata.get("department_name"),
        limit=80,
        citation=citation,
    )
    category = metadata.get("category_path")
    if isinstance(category, (list, tuple)):
        parts = tuple(
            dict.fromkeys(
                normalized
                for part in category
                if isinstance(part, str)
                if (normalized := _normalized(part)[:120])
            )
        )
        _append_field(
            lines,
            fields,
            label="分类",
            raw=" > ".join(parts),
            limit=600,
            citation=citation,
        )
    _append_field(
        lines,
        fields,
        label="结构",
        raw=chunk.role.value.upper(),
        limit=24,
        citation=citation,
    )
    prefix = "\n".join(lines)[:_PREFIX_CHARACTER_LIMIT]
    return ContextualTextViews(
        rerank_text=f"{prefix}\n\n{citation}" if prefix else citation,
        lexical_text_override=None,
        embedding_text_override=None,
        context_fields=tuple(fields),
        revision=_CONTEXT_REVISION,
    )


def _append_field(  # noqa: PLR0913
    lines: list[str],
    fields: list[str],
    *,
    label: str,
    raw: object,
    limit: int,
    citation: str,
) -> None:
    """避免空值、重复字段及正文已有的相同开头。"""
    if not isinstance(raw, str):
        return
    value = _normalized(raw)[:limit]
    if not value or value in _normalized(citation[:_PREFIX_CHARACTER_LIMIT]):
        return
    if any(line == f"{label}：{value}" for line in lines):
        return
    lines.append(f"{label}：{value}")
    fields.append(label)


def _normalized(value: str) -> str:
    """将可信字段归一化为单行、单空格文本。"""
    return " ".join(unicodedata.normalize("NFKC", value).split())
