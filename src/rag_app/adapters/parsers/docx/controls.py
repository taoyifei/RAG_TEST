"""解析结构化文档标签并排除导航型目录内容。"""

from __future__ import annotations

from typing import Protocol

from lxml import etree

from rag_app.adapters.parsers.docx.media import (
    BlockMediaContext,
    preserve_unsupported,
)
from rag_app.adapters.parsers.docx.namespaces import WORD, qn, word_attr
from rag_app.core.models import NodeKind, StoryKind


class ContentControlContext(BlockMediaContext, Protocol):
    """内容控件解析需要的最小 block 上下文。"""

    def parse_container(  # noqa: PLR0913
        self,
        container: etree._Element,
        *,
        parent_node_id: str | None,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        table_depth: int,
    ) -> None:
        """递归解析控件内 block。

        Args:
            container: 内容控件内的 block 容器。
            parent_node_id: 控件容器节点 ID。
            part_uri: 当前 Part URI。
            story_kind: 当前 story 类型。
            structural_path: 稳定结构路径。
            table_depth: 当前表格嵌套深度。

        Returns:
            无返回值。

        """
        ...


def parse_content_control(  # noqa: PLR0913
    parser: ContentControlContext,
    control: etree._Element,
    *,
    parent_node_id: str | None,
    part_uri: str,
    story_kind: StoryKind,
    structural_path: tuple[str, ...],
    table_depth: int,
) -> None:
    """保存普通内容控件，并跳过目录导航副本。

    Args:
        parser: 共享 block 解析上下文。
        control: 当前 `w:sdt` 元素。
        parent_node_id: 可选父节点 ID。
        part_uri: 当前 Part URI。
        story_kind: 当前 story 类型。
        structural_path: 稳定结构路径。
        table_depth: 当前表格嵌套深度。

    Returns:
        无返回值。

    """
    properties = control.find(qn(WORD, "sdtPr"))
    gallery = (
        None
        if properties is None
        else properties.find(f".//{{{WORD}}}docPartGallery")
    )
    gallery_value = None if gallery is None else gallery.get(word_attr("val"))
    if (
        gallery_value
        and gallery_value.strip().casefold() == "table of contents"
    ):
        parser.issues.add(
            "DOCX_TOC_CONTROL_SKIPPED",
            action="navigation_metadata_only",
            message="目录内容控件未作为正式证据。",
        )
        return
    content = control.find(qn(WORD, "sdtContent"))
    if content is None:
        preserve_unsupported(
            parser,
            control,
            parent_node_id=parent_node_id,
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
        )
        return
    metadata: dict[str, object] = {}
    for local, key in (("tag", "tag"), ("alias", "alias")):
        node = None if properties is None else properties.find(qn(WORD, local))
        value = None if node is None else node.get(word_attr("val"))
        if value:
            metadata[key] = value[:256]
    container_node = parser.builder.add(
        kind=NodeKind.CONTENT_CONTROL,
        parent_node_id=parent_node_id,
        anchor=parser.anchor(
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
        ),
        metadata=metadata,
    )
    parser.parse_container(
        content,
        parent_node_id=container_node.node_id,
        part_uri=part_uri,
        story_kind=story_kind,
        structural_path=structural_path,
        table_depth=table_depth,
    )
