"""DOCX OOXML v4 adapter 使用的固定命名空间。"""

from __future__ import annotations

from lxml import etree

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
OFFICE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PACKAGE_REL = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
CONTENT_TYPES = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
WORD_DRAWING = (
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
)
PICTURE = (
    "http://schemas.openxmlformats.org/drawingml/2006/picture"
)
VML = "urn:schemas-microsoft-com:vml"
MATH = "http://schemas.openxmlformats.org/officeDocument/2006/math"

NS = {
    "a": DRAWING,
    "ct": CONTENT_TYPES,
    "m": MATH,
    "pic": PICTURE,
    "pr": PACKAGE_REL,
    "r": OFFICE_REL,
    "v": VML,
    "w": WORD,
    "wp": WORD_DRAWING,
}


def qn(namespace: str, local_name: str) -> str:
    """构造扩展名称。

    Args:
        namespace: 完整 XML 命名空间。
        local_name: 不含前缀的本地名称。

    Returns:
        lxml 可直接使用的扩展名称。

    """
    return f"{{{namespace}}}{local_name}"


def word_attr(local_name: str) -> str:
    """构造 WordprocessingML 属性名。

    Args:
        local_name: 不含 `w:` 前缀的属性名。

    Returns:
        WordprocessingML 扩展属性名。

    """
    return qn(WORD, local_name)


def local_name(node: etree._Element) -> str:
    """返回元素的本地名称。

    Args:
        node: lxml 元素。

    Returns:
        不含命名空间的元素名称。

    """
    return etree.QName(node).localname
