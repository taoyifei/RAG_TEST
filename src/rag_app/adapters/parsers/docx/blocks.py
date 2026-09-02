"""按 OOXML block 顺序构造正文、表格和内嵌媒体节点。"""

from __future__ import annotations

from collections import defaultdict

from lxml import etree

from rag_app.adapters.parsers.docx.builder import IrNodeBuilder
from rag_app.adapters.parsers.docx.controls import parse_content_control
from rag_app.adapters.parsers.docx.drawings import (
    has_ole_or_diagram,
    image_references,
    text_box_contents,
)
from rag_app.adapters.parsers.docx.issues import IssueCollector
from rag_app.adapters.parsers.docx.media import (
    parse_image,
    preserve_unsupported,
    validate_hyperlinks,
)
from rag_app.adapters.parsers.docx.namespaces import (
    MATH,
    WORD,
    local_name,
    qn,
    word_attr,
)
from rag_app.adapters.parsers.docx.numbering import NumberingCatalog
from rag_app.adapters.parsers.docx.package import DocxPackage
from rag_app.adapters.parsers.docx.revisions import revision_visibility
from rag_app.adapters.parsers.docx.sections import parse_section
from rag_app.adapters.parsers.docx.styles import StyleCatalog
from rag_app.adapters.parsers.docx.tables import parse_table
from rag_app.adapters.parsers.docx.text import extract_paragraph_text
from rag_app.core.models import (
    ListAttributes,
    NodeKind,
    RevisionMark,
    SourceAnchor,
    StoryKind,
)
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports.blob_store import BlobWriteRequest


class BlockParser:
    """共享 package、样式、编号和 IR builder 的 block parser。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        package: DocxPackage,
        policy: ParsingPolicy,
        builder: IrNodeBuilder,
        issues: IssueCollector,
        styles: StyleCatalog,
        numbering: NumberingCatalog,
    ) -> None:
        """创建一次文档解析上下文。

        Args:
            package: 已通过安全校验的 package。
            policy: 冻结解析策略。
            builder: 当前文档 IR builder。
            issues: 共享问题收集器。
            styles: 段落样式 catalog。
            numbering: 自动编号 catalog。

        Returns:
            无返回值。

        """
        self.package = package
        self.policy = policy
        self.builder = builder
        self.issues = issues
        self.styles = styles
        self.numbering = numbering
        self.blob_writes: dict[str, BlobWriteRequest] = {}
        self.note_references: list[tuple[str, str, str]] = []
        self.comment_references: list[tuple[str, str]] = []
        self.cross_references: list[tuple[str, str]] = []
        self.bookmarks: dict[str, str] = {}
        self.section_node_ids: list[str] = []
        self.revision_count = 0
        self._ordinals: defaultdict[
            tuple[str, StoryKind], int
        ] = defaultdict(int)
        self._paragraphs: defaultdict[
            tuple[str, StoryKind], int
        ] = defaultdict(int)
        self._tables: defaultdict[tuple[str, StoryKind], int] = defaultdict(int)
        self._heading_stacks: defaultdict[
            tuple[str, StoryKind], list[str]
        ] = defaultdict(list)

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
        """解析容器的直接 block 子节点。

        Args:
            container: body、cell、content control 或 story 容器。
            parent_node_id: 可选父 IR 节点。
            part_uri: 当前 story Part URI。
            story_kind: 当前 story 类型。
            structural_path: 容器的稳定路径。
            table_depth: 当前嵌套表格深度。

        Returns:
            无返回值。

        """
        self._parse_children(
            container,
            parent_node_id=parent_node_id,
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
            table_depth=table_depth,
            inherited_revision=None,
        )

    def anchor(  # noqa: PLR0913
        self,
        *,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        section_index: int | None = None,
        paragraph_index: int | None = None,
        table_index: int | None = None,
        row_index: int | None = None,
        cell_index: int | None = None,
        relationship_id: str | None = None,
        source_length: int | None = None,
    ) -> SourceAnchor:
        """创建当前 Part 内单调且稳定的来源锚点。

        Args:
            part_uri: 当前 package Part。
            story_kind: story 类型。
            structural_path: 节点结构路径。
            section_index: 可选 section 序号。
            paragraph_index: 可选段落序号。
            table_index: 可选表格序号。
            row_index: 可选行序号。
            cell_index: 可选物理 cell 序号。
            relationship_id: 可选 OOXML 关系 ID。
            source_length: 可选源字符数量。

        Returns:
            不含机器路径的 SourceAnchor。

        """
        key = (part_uri, story_kind)
        ordinal = self._ordinals[key]
        self._ordinals[key] += 1
        return SourceAnchor(
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
            ordinal=ordinal,
            section_index=section_index,
            paragraph_index=paragraph_index,
            table_index=table_index,
            row_index=row_index,
            cell_index=cell_index,
            relationship_id=relationship_id,
            source_start_char=0 if source_length is not None else None,
            source_end_char=source_length,
        )

    def _parse_children(  # noqa: PLR0913
        self,
        container: etree._Element,
        *,
        parent_node_id: str | None,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        table_depth: int,
        inherited_revision: RevisionMark | None,
    ) -> None:
        for child_index, child in enumerate(container):
            self.package.check_timeout()
            kind = local_name(child)
            child_path = (*structural_path, f"{kind}:{child_index}")
            if kind in {"pPr", "tcPr", "trPr", "tblPr", "tblGrid"}:
                continue
            if kind == "p":
                self._parse_paragraph(
                    child,
                    parent_node_id=parent_node_id,
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=child_path,
                    table_depth=table_depth,
                    inherited_revision=inherited_revision,
                )
                continue
            if kind == "tbl":
                key = (part_uri, story_kind)
                table_index = self._tables[key]
                self._tables[key] += 1
                parse_table(
                    self,
                    child,
                    parent_node_id=parent_node_id,
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=child_path,
                    table_index=table_index,
                    table_depth=table_depth,
                )
                continue
            if kind == "sectPr":
                parse_section(
                    self,
                    child,
                    parent_node_id=parent_node_id,
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=child_path,
                )
                continue
            if kind == "sdt":
                parse_content_control(
                    self,
                    child,
                    parent_node_id=parent_node_id,
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=child_path,
                    table_depth=table_depth,
                )
                continue
            if kind == "customXml":
                self._parse_children(
                    child,
                    parent_node_id=parent_node_id,
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=child_path,
                    table_depth=table_depth,
                    inherited_revision=inherited_revision,
                )
                continue
            if kind in {"ins", "del", "moveFrom", "moveTo"}:
                visible, mark = revision_visibility(child, self.policy)
                self.revision_count += 1
                if visible:
                    self._parse_children(
                        child,
                        parent_node_id=parent_node_id,
                        part_uri=part_uri,
                        story_kind=story_kind,
                        structural_path=child_path,
                        table_depth=table_depth,
                        inherited_revision=mark,
                    )
                continue
            preserve_unsupported(
                self,
                child,
                parent_node_id=parent_node_id,
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=child_path,
                force_indexable=(kind == "altChunk"),
            )

    def _parse_paragraph(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        paragraph: etree._Element,
        *,
        parent_node_id: str | None,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        table_depth: int,
        inherited_revision: RevisionMark | None,
    ) -> None:
        extraction = extract_paragraph_text(
            paragraph,
            self.policy,
            self.issues,
        )
        revision_count = dict(extraction.metadata).get("revision_count", 0)
        if isinstance(revision_count, int):
            self.revision_count += revision_count
        if extraction.is_toc:
            self.issues.add(
                "DOCX_TOC_FIELD_SKIPPED",
                action="navigation_metadata_only",
                message="目录字段结果未作为正式证据。",
            )
            return
        key = (part_uri, story_kind)
        paragraph_index = self._paragraphs[key]
        self._paragraphs[key] += 1
        heading_level = self.styles.heading_level(paragraph, self.policy)
        num_id, num_level = self.styles.numbering(paragraph)
        list_attributes: ListAttributes | None = None
        kind = NodeKind.PARAGRAPH
        metadata = dict(extraction.metadata)
        style_id = _paragraph_style_id(paragraph)
        effective_style = self.styles.effective(style_id)
        if style_id is not None:
            metadata["style_id"] = style_id
        if effective_style is not None and effective_style.name is not None:
            metadata["style_name"] = effective_style.name
        if effective_style is not None:
            metadata["style_hidden"] = effective_style.hidden
            metadata["style_quick_format"] = effective_style.quick_format
            metadata["style_unhide_when_used"] = (
                effective_style.unhide_when_used
            )
            if effective_style.next_style_id is not None:
                metadata["next_style_id"] = effective_style.next_style_id
            if effective_style.linked_style_id is not None:
                metadata["linked_style_id"] = (
                    effective_style.linked_style_id
                )
        if heading_level is not None:
            kind = NodeKind.HEADING
            metadata["heading_level"] = heading_level
            stack = self._heading_stacks[key]
            del stack[heading_level - 1 :]
        elif num_id is not None and num_level is not None:
            kind = NodeKind.LIST_ITEM
            label = self.numbering.next_label(num_id, num_level)
            list_attributes = ListAttributes(
                level=num_level,
                ordered=label.ordered,
                marker=label.marker,
                ordinal=label.ordinal,
                restart_group=label.restart_group,
            )
            metadata["num_id"] = num_id
        paragraph_node = self.builder.add(
            kind=kind,
            parent_node_id=parent_node_id,
            anchor=self.anchor(
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=structural_path,
                paragraph_index=paragraph_index,
                source_length=len(extraction.exact_text),
            ),
            exact_text=extraction.exact_text,
            semantic_text=extraction.semantic_text,
            revision_mark=extraction.revision_mark or inherited_revision,
            list_attributes=list_attributes,
            metadata=metadata,
        )
        heading_stack = self._heading_stacks[key]
        if heading_level is not None:
            heading_stack.append(paragraph_node.node_id)
        if heading_stack:
            paragraph_node.metadata["heading_breadcrumb_node_ids"] = tuple(
                heading_stack
            )
        for bookmark in extraction.bookmark_names:
            self.bookmarks.setdefault(bookmark, paragraph_node.node_id)
        for reference_kind, reference_id in extraction.note_references:
            self.note_references.append(
                (paragraph_node.node_id, reference_kind, reference_id)
            )
        for reference_id in extraction.comment_references:
            self.comment_references.append(
                (paragraph_node.node_id, reference_id)
            )
        field_targets = metadata.get("field_targets", ())
        if isinstance(field_targets, (tuple, list)):
            for target in field_targets:
                if isinstance(target, str):
                    self.cross_references.append(
                        (paragraph_node.node_id, target)
                    )
        validate_hyperlinks(self, part_uri, metadata)
        for break_index, break_type in enumerate(extraction.break_types):
            if break_type == "line":
                continue
            self.builder.add(
                kind=NodeKind.BREAK,
                parent_node_id=paragraph_node.node_id,
                anchor=self.anchor(
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=(
                        *structural_path,
                        f"break:{break_index}",
                    ),
                ),
                metadata={"break_type": break_type},
            )
        for image_index, reference in enumerate(image_references(paragraph)):
            parse_image(
                self,
                reference,
                parent_node_id=paragraph_node.node_id,
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=(
                    *structural_path,
                    f"image:{image_index}",
                ),
            )
        for text_box_index, text_box in enumerate(text_box_contents(paragraph)):
            text_box_path = (
                *structural_path,
                f"textbox:{text_box_index}",
            )
            container_node = self.builder.add(
                kind=NodeKind.CONTENT_CONTROL,
                parent_node_id=paragraph_node.node_id,
                anchor=self.anchor(
                    part_uri=part_uri,
                    story_kind=StoryKind.TEXT_BOX,
                    structural_path=text_box_path,
                ),
                metadata={"story_container": StoryKind.TEXT_BOX.value},
            )
            self.parse_container(
                text_box,
                parent_node_id=container_node.node_id,
                part_uri=part_uri,
                story_kind=StoryKind.TEXT_BOX,
                structural_path=text_box_path,
                table_depth=table_depth,
            )
        if has_ole_or_diagram(paragraph):
            self.issues.add(
                "DOCX_OBJECT_NOT_EXECUTED",
                action="metadata_only",
                message="OLE、Object 或 diagram 未执行或解包。",
            )
        if any(True for _ in paragraph.iter(qn(MATH, "oMath"))):
            self.issues.add(
                "DOCX_MATH_METADATA_ONLY",
                action="metadata_only",
                message="Office Math 未计算，仅保留可见 fallback 文本。",
            )
        section = paragraph.find(f"./{{{WORD}}}pPr/{{{WORD}}}sectPr")
        if section is not None:
            parse_section(
                self,
                section,
                parent_node_id=parent_node_id,
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=(*structural_path, "sectPr:0"),
            )

def _paragraph_style_id(paragraph: etree._Element) -> str | None:
    node = paragraph.find(f"./{{{WORD}}}pPr/{{{WORD}}}pStyle")
    return None if node is None else node.get(word_attr("val"))
