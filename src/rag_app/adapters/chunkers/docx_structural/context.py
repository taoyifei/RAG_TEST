"""确定性 embedding-only 上下文。"""

from __future__ import annotations

from rag_app.adapters.chunkers.docx_structural.atoms import AtomicUnit
from rag_app.adapters.chunkers.docx_structural.rendering import (
    render_context_fragments,
)
from rag_app.core.models import ChunkRole

_CONTEXT_CHARACTER_CAP = 160
_ROLE_LABELS = {
    ChunkRole.TEXT: "正文",
    ChunkRole.LIST: "列表",
    ChunkRole.TABLE: "表格",
    ChunkRole.NOTE: "脚注或尾注",
    ChunkRole.IMAGE_METADATA: "图片元数据",
    ChunkRole.HEADER_FOOTER: "页眉页脚",
    ChunkRole.TEXT_BOX: "文本框",
    ChunkRole.COMMENT: "批注",
}


def embedding_text(
    document_title: str,
    atom: AtomicUnit,
    citation_text: str,
) -> str:
    """构造不冒充 citation 来源的确定性 embedding 文本。

    Args:
        document_title: 展示标题，仅用于上下文。
        atom: 当前 pack 的首个结构原子。
        citation_text: 可精确引用的正文。

    Returns:
        受字符预算约束的前缀与 citation 正文。

    """
    lines: list[str] = []
    if document_title:
        lines.append(f"文档：{document_title}")
    if atom.heading_path:
        lines.append(f"位置：{' > '.join(atom.heading_path)}")
    lines.append(f"类型：{_ROLE_LABELS[atom.role]}")
    if atom.table_header_fragments:
        header = render_context_fragments(atom.table_header_fragments)
        if header:
            lines.append(f"表头：{header}")
    prefix = "\n".join(lines)
    if len(prefix) > _CONTEXT_CHARACTER_CAP:
        prefix = f"{prefix[: _CONTEXT_CHARACTER_CAP - 1]}…"
    return f"{prefix}\n\n{citation_text}"
