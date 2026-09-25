"""本轮自然问答的私有引用句柄与已送模证据绑定。"""

from __future__ import annotations

from html import escape

from rag_app.application.answering.natural_answer import NaturalReference

CITATION_PROTOCOL_REVISION = "wk-ref-h1"


class NaturalSourceRegistry:
    """仅为当前请求已选择的材料分配 cN，离开请求即丢弃。"""

    def __init__(self, references: tuple[NaturalReference, ...]) -> None:
        self._by_handle: dict[str, NaturalReference] = {}
        self._by_alias: dict[str, str] = {}
        for number, reference in enumerate(references, start=1):
            handle = f"c{number}"
            if reference.alias in self._by_alias:
                raise ValueError("本轮材料的公开编号重复。")
            self._by_handle[handle] = reference
            self._by_alias[reference.alias] = handle

    def handle_for_alias(self, alias: str) -> str:
        """仅把已登记的公开编号转换为本轮私有句柄。"""
        return self._by_alias[alias]

    def resolve(self, handle: str) -> NaturalReference | None:
        """未知或非当前证据句柄没有引用资格。"""
        return self._by_handle.get(handle)

    def references(
        self, handles: tuple[str, ...]
    ) -> tuple[NaturalReference, ...]:
        """按模型引用次序返回本轮经核验的正式来源。"""
        return tuple(
            reference
            for handle in handles
            if (reference := self.resolve(handle)) is not None
        )

    def render_sources(
        self, passages: tuple[tuple[NaturalReference, str], ...]
    ) -> str:
        """构建已转义的来源块，不发送文档、版本或 Chunk 内部身份。"""
        blocks = ["<sources>"]
        for reference, content in passages:
            handle = self.handle_for_alias(reference.alias)
            title = escape(reference.document_title, quote=True)
            body = escape(content, quote=True)
            blocks.append(
                f'<source id="{handle}" title="{title}">{body}</source>'
            )
        blocks.append("</sources>")
        return "\n".join(blocks)


__all__ = ["CITATION_PROTOCOL_REVISION", "NaturalSourceRegistry"]
