"""解析 section story、页眉页脚、脚注尾注和批注。"""

from __future__ import annotations

from rag_app.adapters.parsers.docx.blocks import BlockParser
from rag_app.adapters.parsers.docx.namespaces import WORD, qn, word_attr
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import (
    DocumentRelationship,
    NodeKind,
    StoryKind,
)
from rag_app.core.policies import CommentsPolicy, StoryPolicy

_PAIR_LENGTH = 2


def parse_related_stories(
    parser: BlockParser,
) -> tuple[DocumentRelationship, ...]:
    """解析主文档可达的附属 story 并建立节点关系。

    Args:
        parser: 已完成主正文解析的 block parser。

    Returns:
        主正文引用到 note、comment 和 bookmark 的关系。

    """
    _resolve_sections(parser)
    note_nodes = _parse_notes(parser)
    comment_nodes = _parse_comments(parser)
    relationships: list[DocumentRelationship] = []
    for index, (source_id, reference_kind, note_id) in enumerate(
        parser.note_references
    ):
        story = (
            StoryKind.FOOTNOTE
            if reference_kind == "footnoteReference"
            else StoryKind.ENDNOTE
        )
        target_id = note_nodes.get((story, note_id))
        if target_id is None:
            parser.issues.add(
                "DOCX_NOTE_REFERENCE_MISSING",
                action="reference_metadata_only",
                message="正文引用的脚注或尾注不存在。",
            )
            continue
        relationships.append(
            DocumentRelationship(
                relationship_id=f"note:{index}",
                relationship_type=f"document-{story.value}",
                source_node_id=source_id,
                target_node_id=target_id,
            )
        )
    for index, (source_id, comment_id) in enumerate(
        parser.comment_references
    ):
        target_id = comment_nodes.get(comment_id)
        if target_id is None:
            parser.issues.add(
                "DOCX_COMMENT_REFERENCE_ORPHAN",
                action="reference_metadata_only",
                message="正文批注引用没有可关联的 CommentNode。",
            )
            continue
        relationships.append(
            DocumentRelationship(
                relationship_id=f"comment:{index}",
                relationship_type="document-comment",
                source_node_id=source_id,
                target_node_id=target_id,
            )
        )
    for index, (source_id, bookmark_name) in enumerate(
        parser.cross_references
    ):
        target_id = parser.bookmarks.get(bookmark_name)
        if target_id is None:
            parser.issues.add(
                "DOCX_BOOKMARK_TARGET_MISSING",
                action="visible_field_result_only",
                message="REF 或 PAGEREF 指向缺失 bookmark。",
            )
            continue
        relationships.append(
            DocumentRelationship(
                relationship_id=f"bookmark:{index}",
                relationship_type="document-bookmark",
                source_node_id=source_id,
                target_node_id=target_id,
            )
        )
    return tuple(relationships)


def _resolve_sections(parser: BlockParser) -> None:
    header_parts: set[str] = set()
    footer_parts: set[str] = set()
    inherited_headers: dict[str, tuple[str, str]] = {}
    inherited_footers: dict[str, tuple[str, str]] = {}
    for node_id in parser.section_node_ids:
        draft = parser.builder.get(node_id)
        headers = _effective_bindings(
            parser,
            draft.metadata.get("header_references", ()),
            inherited_headers,
            node_id,
        )
        footers = _effective_bindings(
            parser,
            draft.metadata.get("footer_references", ()),
            inherited_footers,
            node_id,
        )
        inherited_headers = dict(headers)
        inherited_footers = dict(footers)
        draft.metadata["effective_header_bindings"] = tuple(
            sorted(
                (kind, part_uri)
                for kind, (part_uri, _) in headers.items()
            )
        )
        draft.metadata["effective_footer_bindings"] = tuple(
            sorted(
                (kind, part_uri)
                for kind, (part_uri, _) in footers.items()
            )
        )
        draft.metadata["header_inherited_from"] = tuple(
            sorted(
                (kind, source_node_id)
                for kind, (_, source_node_id) in headers.items()
                if source_node_id != node_id
            )
        )
        draft.metadata["footer_inherited_from"] = tuple(
            sorted(
                (kind, source_node_id)
                for kind, (_, source_node_id) in footers.items()
                if source_node_id != node_id
            )
        )
        header_parts.update(part_uri for part_uri, _ in headers.values())
        footer_parts.update(part_uri for part_uri, _ in footers.values())
    settings_part = _related_part(parser, "settings")
    even_odd = False
    if settings_part is not None:
        settings = parser.package.xml(settings_part)
        even_odd = settings.find(qn(WORD, "evenAndOddHeaders")) is not None
    for node_id in parser.section_node_ids:
        parser.builder.get(node_id).metadata["even_and_odd_headers"] = even_odd
    parts = tuple(
        (part_uri, StoryKind.HEADER) for part_uri in sorted(header_parts)
    ) + tuple(
        (part_uri, StoryKind.FOOTER) for part_uri in sorted(footer_parts)
    )
    if not parts:
        return
    if parser.policy.headers_footers is StoryPolicy.EXCLUDE:
        return
    if parser.policy.headers_footers is StoryPolicy.METADATA_ONLY:
        parser.issues.add(
            "DOCX_HEADER_FOOTER_METADATA_ONLY",
            action="metadata_only",
            message="页眉页脚 Part 已建立 section binding，正文未进入节点。",
            count=len(parts),
        )
        return
    for part_uri, story_kind in parts:
        root = parser.package.xml(part_uri)
        container = parser.builder.add(
            kind=NodeKind.CONTENT_CONTROL,
            anchor=parser.anchor(
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=(f"{story_kind.value}:root",),
            ),
            metadata={"story_definition": story_kind.value},
        )
        parser.parse_container(
            root,
            parent_node_id=container.node_id,
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=(f"{story_kind.value}:root",),
            table_depth=0,
        )


def _effective_bindings(
    parser: BlockParser,
    raw: object,
    inherited: dict[str, tuple[str, str]],
    section_node_id: str,
) -> dict[str, tuple[str, str]]:
    result = dict(inherited)
    if not isinstance(raw, (tuple, list)):
        return result
    main_part = parser.package.catalog.main_part_uri
    for item in raw:
        if (
            not isinstance(item, (tuple, list))
            or len(item) != _PAIR_LENGTH
        ):
            continue
        reference_type, relationship_id = item
        if not isinstance(reference_type, str) or not isinstance(
            relationship_id,
            str,
        ):
            continue
        relationship = parser.package.relationship(
            main_part,
            relationship_id,
        )
        if relationship is None or relationship.target_part_uri is None:
            parser.issues.add(
                "DOCX_SECTION_STORY_RELATIONSHIP_MISSING",
                action="inherit_or_empty",
                message="Section 的页眉页脚关系不存在。",
            )
            continue
        result[reference_type] = (
            relationship.target_part_uri,
            section_node_id,
        )
    return result


def _parse_notes(
    parser: BlockParser,
) -> dict[tuple[StoryKind, str], str]:
    result: dict[tuple[StoryKind, str], str] = {}
    parts = (
        (_related_part(parser, "footnotes"), StoryKind.FOOTNOTE, "footnote"),
        (_related_part(parser, "endnotes"), StoryKind.ENDNOTE, "endnote"),
    )
    for part_uri, story_kind, element_name in parts:
        if part_uri is None:
            continue
        root = parser.package.xml(part_uri)
        note_elements = root.findall(qn(WORD, element_name))
        regular = [
            note
            for note in note_elements
            if (note.get(word_attr("type")) or "")
            not in {"separator", "continuationSeparator"}
            and (note.get(word_attr("id")) or "") not in {"-1", "0"}
        ]
        if parser.policy.footnotes_endnotes is StoryPolicy.EXCLUDE:
            continue
        if parser.policy.footnotes_endnotes is StoryPolicy.METADATA_ONLY:
            if regular:
                parser.issues.add(
                    "DOCX_NOTE_METADATA_ONLY",
                    action="metadata_only",
                    message="脚注尾注已计数，正文未进入节点。",
                    count=len(regular),
                )
            continue
        for note in regular:
            note_id = note.get(word_attr("id"))
            if note_id is None:
                parser.issues.add(
                    "DOCX_NOTE_ID_MISSING",
                    action="note_skipped",
                    message="脚注或尾注缺少 ID。",
                )
                continue
            key = (story_kind, note_id)
            if key in result:
                parser.issues.add(
                    "DOCX_NOTE_ID_DUPLICATE",
                    action="first_note_kept",
                    message="脚注或尾注 ID 重复。",
                )
                continue
            node = parser.builder.add(
                kind=NodeKind.NOTE,
                anchor=parser.anchor(
                    part_uri=part_uri,
                    story_kind=story_kind,
                    structural_path=(f"{story_kind.value}:{note_id}",),
                ),
                metadata={"note_id": note_id},
            )
            result[key] = node.node_id
            parser.parse_container(
                note,
                parent_node_id=node.node_id,
                part_uri=part_uri,
                story_kind=story_kind,
                structural_path=(f"{story_kind.value}:{note_id}",),
                table_depth=0,
            )
    return result


def _parse_comments(parser: BlockParser) -> dict[str, str]:
    part_uri = _related_part(parser, "comments")
    if part_uri is None:
        return {}
    root = parser.package.xml(part_uri)
    comments = root.findall(qn(WORD, "comment"))
    if parser.policy.comments is CommentsPolicy.REJECT:
        raise InvalidDocument(
            "ParsingPolicy 拒绝包含批注的 DOCX。",
            stage="docx-ooxml-v4.comments",
        )
    if parser.policy.comments is CommentsPolicy.METADATA_ONLY:
        if comments:
            parser.issues.add(
                "DOCX_COMMENT_METADATA_ONLY",
                action="metadata_only",
                message="批注已计数，正文未进入检索节点。",
                count=len(comments),
            )
        return {}
    result: dict[str, str] = {}
    for comment in comments:
        comment_id = comment.get(word_attr("id"))
        if comment_id is None:
            parser.issues.add(
                "DOCX_COMMENT_ID_MISSING",
                action="comment_skipped",
                message="批注缺少 ID。",
            )
            continue
        if comment_id in result:
            parser.issues.add(
                "DOCX_COMMENT_ID_DUPLICATE",
                action="first_comment_kept",
                message="批注 ID 重复。",
            )
            continue
        node = parser.builder.add(
            kind=NodeKind.COMMENT,
            anchor=parser.anchor(
                part_uri=part_uri,
                story_kind=StoryKind.COMMENT,
                structural_path=(f"comment:{comment_id}",),
            ),
            metadata={
                "comment_id": comment_id,
                "author": comment.get(word_attr("author")),
                "timestamp": comment.get(word_attr("date")),
            },
        )
        result[comment_id] = node.node_id
        parser.parse_container(
            comment,
            parent_node_id=node.node_id,
            part_uri=part_uri,
            story_kind=StoryKind.COMMENT,
            structural_path=(f"comment:{comment_id}",),
            table_depth=0,
        )
    return result


def _related_part(parser: BlockParser, suffix: str) -> str | None:
    main_part = parser.package.catalog.main_part_uri
    for relationship in parser.package.catalog.relationships_from(main_part):
        if relationship.relationship_type.endswith(f"/{suffix}"):
            return relationship.target_part_uri
    fallback = f"/word/{suffix}.xml"
    return (
        fallback
        if parser.package.catalog.part(fallback) is not None
        else None
    )
