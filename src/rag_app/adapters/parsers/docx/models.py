"""DOCX OOXML v4 adapter 内部数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import JsonValue

from rag_app.core.models import (
    CellGrid,
    ImageAttributes,
    ListAttributes,
    NodeKind,
    RevisionMark,
    SourceAnchor,
    TextPayload,
)
from rag_app.core.models.common import JsonObject


@dataclass(frozen=True, slots=True)
class PartInfo:
    """一个 OPC Part 的安全目录信息。"""

    part_uri: str
    content_type: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RelationshipInfo:
    """一个已规范化的 OPC Relationship。"""

    source_part_uri: str
    relationship_id: str
    relationship_type: str
    target_mode: str
    target_part_uri: str | None
    external_scheme: str | None = None


@dataclass(frozen=True, slots=True)
class PartCatalog:
    """不携带原文的 Part 与 Relationship 审计目录。"""

    main_part_uri: str
    parts: tuple[PartInfo, ...]
    relationships: tuple[RelationshipInfo, ...]

    def part(self, part_uri: str) -> PartInfo | None:
        """按 URI 查找 Part。

        Args:
            part_uri: 已规范化的绝对 package URI。

        Returns:
            找到的 Part，否则为 `None`。

        """
        return next(
            (part for part in self.parts if part.part_uri == part_uri),
            None,
        )

    def relationships_from(
        self,
        source_part_uri: str,
    ) -> tuple[RelationshipInfo, ...]:
        """返回一个 Part 发出的全部关系。

        Args:
            source_part_uri: 关系源 Part URI，根 package 使用 `/`。

        Returns:
            按关系 ID 排序的关系元组。

        """
        return tuple(
            relationship
            for relationship in self.relationships
            if relationship.source_part_uri == source_part_uri
        )


@dataclass(frozen=True, slots=True)
class EffectiveStyle:
    """可证明的段落有效样式属性。"""

    style_id: str
    name: str | None
    outline_level: int | None
    num_id: int | None
    num_level: int | None
    hidden: bool
    next_style_id: str | None
    linked_style_id: str | None
    quick_format: bool
    unhide_when_used: bool


@dataclass(frozen=True, slots=True)
class NumberingLabel:
    """一次列表计数推导结果。"""

    marker: str | None
    ordinal: int
    restart_group: str
    ordered: bool | None


@dataclass(frozen=True, slots=True)
class TextExtraction:
    """段落可见文本和相关结构标记。"""

    exact_text: str
    semantic_text: str
    metadata: tuple[tuple[str, object], ...] = ()
    revision_mark: RevisionMark | None = None
    break_types: tuple[str, ...] = ()
    image_relationship_ids: tuple[str, ...] = ()
    note_references: tuple[tuple[str, str], ...] = ()
    comment_references: tuple[str, ...] = ()
    bookmark_names: tuple[str, ...] = ()
    is_toc: bool = False


@dataclass(slots=True)
class NodeDraft:
    """构造 Document IR 前的 adapter 内部可变节点。"""

    node_id: str
    kind: NodeKind
    order: int
    anchor: SourceAnchor
    parent_node_id: str | None = None
    children: list[str] = field(default_factory=list)
    text_payload: TextPayload | None = None
    revision_mark: RevisionMark | None = None
    list_attributes: ListAttributes | None = None
    cell_grid: CellGrid | None = None
    image_attributes: ImageAttributes | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def json_value(value: object) -> JsonValue:
    """把 adapter 内部不可变结构转为 Pydantic JSON 值。

    Args:
        value: 只应包含 JSON 标量、mapping 或 sequence 的值。

    Returns:
        递归转换后的 JSON 值。

    Raises:
        ValueError: 值包含 adapter 不允许写入 Core 的对象。

    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        if value and all(
            isinstance(item, (tuple, list))
            and len(item) == 2  # noqa: PLR2004
            and isinstance(item[0], str)
            for item in value
        ):
            return {str(item[0]): json_value(item[1]) for item in value}
        return [json_value(item) for item in value]
    raise ValueError(
        f"DOCX metadata 包含非 JSON 类型：{type(value).__name__}。"
    )


def json_object(value: tuple[tuple[str, object], ...]) -> JsonObject:
    """把 adapter 键值对转为 Core JsonObject。

    Args:
        value: 不允许重复键的 adapter metadata。

    Returns:
        按键排序的 Core JsonObject。

    """
    return tuple(sorted((key, json_value(item)) for key, item in value))
