"""从有序来源片段生成 citation 文本和精确跨度。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.adapters.chunkers.docx_structural.atoms import (
    AtomicUnit,
    SourceFragment,
)
from rag_app.core.models import ChunkRole, SourceSpan, SourceSpanKind


@dataclass(frozen=True, slots=True)
class RenderedCitation:
    """渲染后的 citation 文本和全覆盖 spans。"""

    text: str
    spans: tuple[SourceSpan, ...]


def separator_fragment(text: str) -> SourceFragment:
    """构造不可引用且没有来源范围的受控分隔符。

    Args:
        text: 非空分隔符文本。

    Returns:
        SEPARATOR 来源片段。

    """
    return SourceFragment(text=text, span_type=SourceSpanKind.SEPARATOR)


def render_fragments(
    fragments: tuple[SourceFragment, ...],
) -> RenderedCitation:
    """按顺序渲染来源片段并生成无间隙跨度。

    Args:
        fragments: 已按最终 citation 顺序排列的片段。

    Returns:
        citation 文本和逐字符全覆盖 spans。

    """
    text_parts: list[str] = []
    spans: list[SourceSpan] = []
    cursor = 0
    for fragment in fragments:
        if not fragment.text:
            continue
        end = cursor + len(fragment.text)
        is_separator = fragment.span_type is SourceSpanKind.SEPARATOR
        spans.append(
            SourceSpan(
                span_type=fragment.span_type,
                node_id=fragment.node_id,
                source_anchor=fragment.source_anchor,
                structural_path=(
                    fragment.source_anchor.structural_path
                    if fragment.source_anchor is not None
                    else ()
                ),
                chunk_start_char=cursor,
                chunk_end_char=end,
                source_start_char=fragment.source_start_char,
                source_end_char=fragment.source_end_char,
                is_repeated=fragment.is_repeated,
                is_citable=not is_separator,
                metadata=fragment.metadata,
            )
        )
        text_parts.append(fragment.text)
        cursor = end
    return RenderedCitation(text="".join(text_parts), spans=tuple(spans))


def render_atoms(atoms: tuple[AtomicUnit, ...]) -> RenderedCitation:
    """渲染同一 run 内的一组原子。

    Args:
        atoms: 不跨 section 或 run 的有序原子。

    Returns:
        含原子间受控 separator span 的 citation。

    """
    fragments: list[SourceFragment] = []
    for index, atom in enumerate(atoms):
        if index:
            fragments.append(
                separator_fragment(_atom_separator(atoms[index - 1], atom))
            )
        fragments.extend(atom.fragments)
    return render_fragments(tuple(fragments))


def render_context_fragments(
    fragments: tuple[SourceFragment, ...],
) -> str:
    """只渲染可重建上下文，不发布新的 citation span。

    Args:
        fragments: 标题或表头等真实来源片段。

    Returns:
        确定性上下文文本。

    """
    return "".join(fragment.text for fragment in fragments)


def _atom_separator(previous: AtomicUnit, current: AtomicUnit) -> str:
    if previous.role is current.role is ChunkRole.LIST:
        return "\n"
    if previous.role is current.role is ChunkRole.TABLE:
        return "\n"
    return "\n\n"
