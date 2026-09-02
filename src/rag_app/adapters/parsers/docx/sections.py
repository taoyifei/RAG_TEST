"""把 section 属性和页眉页脚引用映射为独立 IR 节点。"""

from __future__ import annotations

from typing import Protocol

from lxml import etree

from rag_app.adapters.parsers.docx.builder import IrNodeBuilder
from rag_app.adapters.parsers.docx.namespaces import (
    OFFICE_REL,
    WORD,
    qn,
    word_attr,
)
from rag_app.core.models import NodeKind, SourceAnchor, StoryKind


class SectionContext(Protocol):
    """section 解析需要的最小 block 上下文。"""

    builder: IrNodeBuilder
    section_node_ids: list[str]

    def anchor(
        self,
        *,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        section_index: int | None = None,
    ) -> SourceAnchor:
        """创建 section 来源锚点。

        Args:
            part_uri: 当前 Part URI。
            story_kind: 当前 story 类型。
            structural_path: 稳定结构路径。
            section_index: Section 顺序号。

        Returns:
            Section 来源锚点。

        """
        ...


def parse_section(  # noqa: PLR0913
    parser: SectionContext,
    section: etree._Element,
    *,
    parent_node_id: str | None,
    part_uri: str,
    story_kind: StoryKind,
    structural_path: tuple[str, ...],
) -> None:
    """保存 section 属性以及显式 header/footer 引用。

    Args:
        parser: 共享 block 解析上下文。
        section: 当前 `w:sectPr` 元素。
        parent_node_id: 可选父节点 ID。
        part_uri: 当前 Part URI。
        story_kind: 当前 story 类型。
        structural_path: 稳定结构路径。

    Returns:
        无返回值。

    """
    section_index = len(parser.section_node_ids)
    metadata: dict[str, object] = {
        "break_type": _child_value(section, "type") or "nextPage",
        "title_page": section.find(qn(WORD, "titlePg")) is not None,
        "header_references": _story_references(
            section,
            "headerReference",
        ),
        "footer_references": _story_references(
            section,
            "footerReference",
        ),
        "page_size": _attributes(section.find(qn(WORD, "pgSz"))),
        "page_margins": _attributes(section.find(qn(WORD, "pgMar"))),
    }
    node = parser.builder.add(
        kind=NodeKind.SECTION,
        parent_node_id=parent_node_id,
        anchor=parser.anchor(
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
            section_index=section_index,
        ),
        metadata=metadata,
    )
    parser.section_node_ids.append(node.node_id)


def _story_references(
    section: etree._Element,
    local: str,
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for reference in section.findall(qn(WORD, local)):
        relationship_id = reference.get(qn(OFFICE_REL, "id"))
        if relationship_id is None:
            continue
        reference_type = reference.get(word_attr("type")) or "default"
        result.append((reference_type, relationship_id))
    return tuple(result)


def _child_value(parent: etree._Element, local: str) -> str | None:
    node = parent.find(qn(WORD, local))
    return None if node is None else node.get(word_attr("val"))


def _attributes(node: etree._Element | None) -> tuple[tuple[str, str], ...]:
    if node is None:
        return ()
    values = [
        (etree.QName(name).localname, str(value))
        for name, value in node.attrib.items()
    ]
    return tuple(sorted(values))
