"""确定性 embedding-only 上下文。"""

from __future__ import annotations

from collections.abc import Sequence

from rag_app.adapters.chunkers.docx_structural.atoms import AtomicUnit
from rag_app.adapters.chunkers.docx_structural.rendering import (
    render_context_fragments,
)
from rag_app.core.models import ChunkRole
from rag_app.core.models.common import JsonObject

_CONTEXT_CHARACTER_CAP = 232
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
    *,
    structural_context: str | None = None,
    document_metadata: JsonObject = (),
) -> str:
    """构造不冒充 citation 来源的确定性 embedding 文本。

    Args:
        document_title: 展示标题，仅用于上下文。
        atom: 当前 pack 的首个结构原子。
        citation_text: 可精确引用的正文。
        structural_context: 可选的多原子逻辑结构上下文。
        document_metadata: 已入库的文档级部门、分类元数据。

    Returns:
        受字符预算约束的前缀与 citation 正文。

    """
    metadata = dict(document_metadata)
    lines: list[str] = []
    if document_title:
        lines.append(f"文档：{_bounded_value(document_title, 40)}")
    department = metadata.get("department_name")
    if isinstance(department, str) and department.strip():
        lines.append(f"部门：{_bounded_value(department, 20)}")
    category = metadata.get("category_path")
    if isinstance(category, (tuple, list)) and category and all(
        isinstance(part, str) for part in category
    ):
        lines.append(f"分类：{_bounded_value(' > '.join(category), 48)}")
    if atom.heading_path:
        heading = " > ".join(atom.heading_path)
        lines.append(f"位置：{_bounded_value(heading, 40)}")
    lines.append(f"类型：{_ROLE_LABELS[atom.role]}")
    if atom.table_header_fragments:
        header = render_context_fragments(atom.table_header_fragments)
        if header:
            lines.append(f"表头：{_bounded_value(header, 24)}")
    resolved_context = (
        atom.structural_context
        if structural_context is None
        else structural_context
    )
    if resolved_context:
        lines.append(f"结构：{_bounded_value(resolved_context, 20)}")
    prefix = "\n".join(lines)
    if len(prefix) > _CONTEXT_CHARACTER_CAP:
        prefix = f"{prefix[: _CONTEXT_CHARACTER_CAP - 1]}…"
    return f"{prefix}\n\n{citation_text}"


def _bounded_value(value: str, limit: int) -> str:
    """压缩元数据换行并在字段边界确定性截断。"""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def pack_structural_context(atoms: Sequence[AtomicUnit]) -> str:
    """合并同一 pack 中每个原子的结构上下文。

    Args:
        atoms: 保持来源顺序的结构原子。

    Returns:
        非空上下文按换行连接的字符串。

    """
    return "\n".join(
        atom.structural_context for atom in atoms if atom.structural_context
    )
