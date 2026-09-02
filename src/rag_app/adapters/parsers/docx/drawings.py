"""提取 DrawingML、VML 图片和文本框引用。"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from rag_app.adapters.parsers.docx.namespaces import (
    DRAWING,
    OFFICE_REL,
    VML,
    WORD,
    WORD_DRAWING,
    qn,
)


@dataclass(frozen=True, slots=True)
class DrawingReference:
    """一个图片显示实例的安全结构信息。"""

    relationship_id: str
    placement: str
    name: str | None
    alt_text: str | None
    title: str | None
    width_emu: int | None
    height_emu: int | None


def image_references(node: etree._Element) -> tuple[DrawingReference, ...]:
    """按文档顺序提取内嵌图片关系。

    Args:
        node: 段落、表格或 story 子树。

    Returns:
        每次显示引用独立保留的图片引用元组。

    """
    references: list[DrawingReference] = []
    for drawing in node.iter(qn(WORD, "drawing")):
        blips = drawing.findall(f".//{{{DRAWING}}}blip")
        for blip in blips:
            relationship_id = blip.get(qn(OFFICE_REL, "embed"))
            if relationship_id is None:
                continue
            inline = drawing.find(f".//{{{WORD_DRAWING}}}inline")
            anchor = drawing.find(f".//{{{WORD_DRAWING}}}anchor")
            container = inline if inline is not None else anchor
            doc_properties = (
                None
                if container is None
                else container.find(qn(WORD_DRAWING, "docPr"))
            )
            extent = (
                None
                if container is None
                else container.find(qn(WORD_DRAWING, "extent"))
            )
            references.append(
                DrawingReference(
                    relationship_id=relationship_id,
                    placement=(
                        "inline" if inline is not None else "anchor"
                    ),
                    name=(
                        None
                        if doc_properties is None
                        else doc_properties.get("name")
                    ),
                    alt_text=(
                        None
                        if doc_properties is None
                        else doc_properties.get("descr")
                    ),
                    title=(
                        None
                        if doc_properties is None
                        else doc_properties.get("title")
                    ),
                    width_emu=_integer_attribute(extent, "cx"),
                    height_emu=_integer_attribute(extent, "cy"),
                )
            )
    for image in node.iter(qn(VML, "imagedata")):
        relationship_id = image.get(qn(OFFICE_REL, "id"))
        if relationship_id is None:
            continue
        references.append(
            DrawingReference(
                relationship_id=relationship_id,
                placement="vml",
                name=image.get("title"),
                alt_text=image.get("alt"),
                title=image.get("title"),
                width_emu=None,
                height_emu=None,
            )
        )
    return tuple(references)


def text_box_contents(node: etree._Element) -> tuple[etree._Element, ...]:
    """返回 Drawing/VML 中的文本框内容容器。

    Args:
        node: 待扫描的段落或 story block。

    Returns:
        按 XML 顺序排列的 `w:txbxContent` 元组。

    """
    return tuple(node.iter(qn(WORD, "txbxContent")))


def has_ole_or_diagram(node: etree._Element) -> bool:
    """判断子树是否含 OLE、Object 或 diagram 结构。

    Args:
        node: 待检查的 OOXML 子树。

    Returns:
        找到不执行的复杂对象时为 `True`。

    """
    if any(True for _ in node.iter(qn(WORD, "object"))):
        return True
    return bool(
        node.xpath(
            ".//*[local-name()='relIds' or local-name()='OLEObject']"
        )
    )


def _integer_attribute(
    node: etree._Element | None,
    name: str,
) -> int | None:
    if node is None:
        return None
    value = node.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
