"""Section、Run 和 AtomicUnit 的内部不可变规划模型。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.core.models import (
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


@dataclass(frozen=True, slots=True)
class RunPlan:
    """禁止与其他 run 相邻装包的一组有序原子。"""

    run_id: str
    role: ChunkRole
    section_id: str
    neighbor_group_id: str
    heading_path: tuple[str, ...]
    atoms: tuple[AtomicUnit, ...]


@dataclass(frozen=True, slots=True)
class SectionPlan:
    """由标题路径或独立 story 界定的结构段。"""

    section_id: str
    heading_path: tuple[str, ...]
    runs: tuple[RunPlan, ...]


def node_text_fragments(node: DocumentNode) -> tuple[SourceFragment, ...]:
    """把段落或列表节点转换为有序来源片段。

    Args:
        node: 带 TextPayload 的 IR 节点。

    Returns:
        可选派生编号加逐字原文片段。

    """
    payload = node.text_payload
    if payload is None or not payload.exact_text:
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
    fragments.append(
        SourceFragment(
            text=exact_text,
            span_type=SourceSpanKind.ORIGINAL_TEXT,
            node_id=node.node_id,
            source_anchor=node.anchor,
            source_start_char=0,
            source_end_char=len(exact_text),
        )
    )
    return tuple(fragments)
