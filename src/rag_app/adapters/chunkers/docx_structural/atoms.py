"""Section、Run 和 AtomicUnit 的内部不可变规划模型。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.core.models import (
    ChunkContextDependency,
    ChunkRole,
    DocumentNode,
    SourceAnchor,
    SourceSpanKind,
)
from rag_app.core.models.common import JsonObject


@dataclass(frozen=True, slots=True)
class SourceFragment:
    """先保留来源再渲染的最小文本片段。"""

    text: str
    span_type: SourceSpanKind
    node_id: str | None = None
    source_anchor: SourceAnchor | None = None
    source_start_char: int | None = None
    source_end_char: int | None = None
    metadata: JsonObject = ()
    is_repeated: bool = False


@dataclass(frozen=True, slots=True)
class AtomicUnit:
    """不会在普通装包中拆开的结构原子。"""

    unit_id: str
    role: ChunkRole
    parent_node_id: str | None
    section_id: str
    neighbor_group_id: str
    heading_path: tuple[str, ...]
    fragments: tuple[SourceFragment, ...]
    metadata: JsonObject = ()
    child_group_ids: tuple[str, ...] = ()
    note_refs: tuple[str, ...] = ()
    table_header_fragments: tuple[SourceFragment, ...] = ()
    structural_context: str = ""
    context_dependencies: tuple[ChunkContextDependency, ...] = ()


@dataclass(frozen=True, slots=True)
class RunPlan:
    """禁止与其他 run 相邻装包的一组有序原子。"""

    run_id: str
    role: ChunkRole
    section_id: str
    neighbor_group_id: str
    heading_path: tuple[str, ...]
    atoms: tuple[AtomicUnit, ...]
    context_dependencies: tuple[ChunkContextDependency, ...] = ()


@dataclass(frozen=True, slots=True)
class SectionPlan:
    """由标题路径或独立 story 界定的结构段。"""

    section_id: str
    heading_path: tuple[str, ...]
    runs: tuple[RunPlan, ...]
    context_dependencies: tuple[ChunkContextDependency, ...] = ()


def has_visible_text(text: str) -> bool:
    """判断来源文本是否至少包含一个非空白字符。

    Args:
        text: 不做规范化的来源原文。

    Returns:
        至少包含一个非空白字符时为 True。

    """
    return any(not character.isspace() for character in text)


def node_text_fragments(node: DocumentNode) -> tuple[SourceFragment, ...]:
    """把段落或列表节点转换为有序来源片段。

    Args:
        node: 带 TextPayload 的 IR 节点。

    Returns:
        可选派生编号加逐字原文片段。

    """
    payload = node.text_payload
    if payload is None or not has_visible_text(payload.exact_text):
        return ()
    fragments: list[SourceFragment] = []
    attributes = node.list_attributes
    if attributes is not None and attributes.marker:
        fragments.append(
            SourceFragment(
                text=attributes.marker,
                span_type=SourceSpanKind.DERIVED_NUMBERING,
                node_id=node.node_id,
                source_anchor=node.anchor,
                metadata=(
                    ("level", attributes.level),
                    ("ordinal", attributes.ordinal),
                    ("restart_group", attributes.restart_group),
                    ("num_id", dict(node.metadata).get("num_id")),
                ),
            )
        )
    exact_text = payload.exact_text
    raw_origin = dict(node.metadata).get("origin")
    origin = raw_origin if isinstance(raw_origin, str) else None
    source_kinds = {
        "ocr": SourceSpanKind.OCR_TEXT,
        "diagram_relation": SourceSpanKind.DIAGRAM_RELATION,
        "derived_caption_or_association": (
            SourceSpanKind.DERIVED_CAPTION_OR_ASSOCIATION
        ),
    }
    source_kind = (
        source_kinds.get(origin, SourceSpanKind.ORIGINAL_TEXT)
        if origin is not None
        else SourceSpanKind.ORIGINAL_TEXT
    )
    fragments.append(
        SourceFragment(
            text=exact_text,
            span_type=source_kind,
            node_id=node.node_id,
            source_anchor=node.anchor,
            source_start_char=0,
            source_end_char=len(exact_text),
            metadata=node.metadata if origin is not None else (),
        )
    )
    return tuple(fragments)
