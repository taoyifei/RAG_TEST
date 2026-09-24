"""将可读文本映射到不可变解析产物，而非杜撰原文件坐标。"""

from __future__ import annotations

import hashlib
import re
import time
from bisect import bisect_right
from collections.abc import Iterator
from itertools import pairwise

from rag_app.core.errors import InvalidDocument
from rag_app.core.identifiers import document_version_id, node_id
from rag_app.core.models import (
    DocumentIR,
    DocumentNode,
    DocumentSource,
    DocumentVersionRef,
    NodeKind,
    ParseContext,
    ParsedArtifact,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParseSource,
    SourceAnchor,
    StoryKind,
    text_payload,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.policies import ParsingPolicy

_PART_URI = "/parsed/document.md"
_IMAGE_MARKDOWN = re.compile(r"!\[([^\]]*)\]\([^\n)]*\)")
_MAX_MARKDOWN_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_OCCURRENCES = 1024
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)")
_SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST_MARKER = re.compile(r"^ {0,3}(?:[-+*][ \t]+|\d{1,9}[.)][ \t]+)")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def build_reading_view_result(  # noqa: PLR0913
    source: ParseSource,
    context: ParseContext,
    policy: ParsingPolicy,
    *,
    markdown: str,
    parser_id: str,
    parser_version: str,
    images: tuple[tuple[str, bytes], ...] = (),
    started_at: float | None = None,
) -> ParseResult:
    """以解析产物作为引用依据，保留原件和产物的不同身份。"""
    if len(source.content) > policy.max_file_bytes:
        raise InvalidDocument(
            "原文件超过解析策略大小上限。", stage="weknora-reading.resource"
        )
    # 阅读正文不得暴露 sidecar 临时 URL；完整解析文本另存不可变产物。
    raw_text = markdown.lstrip("\ufeff")
    visible_text, image_positions = _sanitized_markdown_images(raw_text)
    bom_length = len(markdown) - len(raw_text)
    if bom_length:
        image_positions = [
            {
                **item,
                "raw_start_char": item["raw_start_char"] + bom_length,
                "raw_end_char": item["raw_end_char"] + bom_length,
            }
            for item in image_positions
        ]
    encoded = visible_text.encode("utf-8")
    if not visible_text.strip() or len(encoded) > _MAX_MARKDOWN_BYTES:
        raise InvalidDocument(
            "解析正文为空或超过 16 MiB 上限。", stage="weknora-reading.content"
        )
    original_digest = hashlib.sha256(source.content).hexdigest()
    parsed_digest = hashlib.sha256(encoded).hexdigest()
    raw_markdown = markdown.encode("utf-8")
    raw_digest = hashlib.sha256(raw_markdown).hexdigest()
    original_artifact_id = f"sha256:{original_digest}"
    parsed_artifact_id = f"sha256:{parsed_digest}"
    raw_artifact_id = f"sha256:{raw_digest}"
    version_id = document_version_id(
        context.document.document_id, original_digest
    )
    blocks = tuple(_readable_blocks(visible_text))
    if (
        blocks[0][0] != 0
        or blocks[-1][1] != len(visible_text)
        or any(left[1] != right[0] for left, right in pairwise(blocks))
    ):
        raise InvalidDocument(
            "阅读视图未完整覆盖解析正文。", stage="weknora-reading.mapping"
        )
    nodes = tuple(
        _reading_node(
            version_id,
            index,
            visible_text[start:end],
            start,
            end,
            parsed_artifact_id,
            heading_level,
            syntax,
        )
        for index, (start, end, heading_level, syntax) in enumerate(blocks)
    )
    if not nodes:
        raise InvalidDocument(
            "解析正文没有可索引段落。", stage="weknora-reading.content"
        )
    image_artifact_occurrences = tuple(
        _image_artifact(content, mime_type) for mime_type, content in images
    )
    image_ids = tuple(item.artifact_id for item in image_artifact_occurrences)
    image_artifacts = tuple(
        {item.artifact_id: item for item in image_artifact_occurrences}.values()
    )
    issues = (
        (
            ParseIssue(
                code="PARSED_MEDIA_TEXT_ONLY",
                severity="warning",
                action="persist_inline_media_without_citation_claim",
                safe_message="图片已保存为独立产物；文本引用不声称识别图片内容。",
                count=len(image_artifact_occurrences),
            ),
        )
        if image_artifacts
        else ()
    )
    elapsed_seconds = (
        0.0 if started_at is None else time.monotonic() - started_at
    )
    report = ParseReport(
        parser_id=parser_id,
        parser_version=parser_version,
        node_count=len(nodes),
        visible_text_nodes=len(nodes),
        represented_visible_text_nodes=len(nodes),
        part_count=1,
        media_count=len(image_artifact_occurrences),
        story_counts=((StoryKind.BODY.value, len(nodes)),),
        issues=issues,
        warnings=tuple(issue.code for issue in issues),
        elapsed_seconds=elapsed_seconds,
    )
    document_ir = DocumentIR(
        source=DocumentSource(
            document_id=context.document.document_id,
            document_version_id=version_id,
            display_name=source.display_name,
            media_type=source.media_type,
            extension=source.extension,
            content_sha256=original_digest,
            size_bytes=len(source.content),
            blob_ref=original_artifact_id,
        ),
        document=context.document.model_copy(
            update={"display_name": source.display_name}
        ),
        version=DocumentVersionRef(
            document_id=context.document.document_id,
            document_version_id=version_id,
            content_sha256=original_digest,
        ),
        root_node_ids=tuple(node.node_id for node in nodes),
        nodes=nodes,
        parse_report=report,
        metadata=freeze_json_object(
            {
                "citation_basis": "parsed_artifact",
                "parsed_artifact_id": parsed_artifact_id,
                "parsed_artifact_sha256": parsed_digest,
                "raw_markdown_artifact_id": raw_artifact_id,
                "reading_domain_revision": "weknora-markdown-domain-v2",
                "embedded_media_artifact_ids": list(image_ids),
                "image_occurrences": image_positions,
                "source_representation": "markdown-reading-view",
                "parsing_policy_id": policy.policy_id,
            }
        ),
    )
    artifacts = [
        ParsedArtifact(
            artifact_id=original_artifact_id,
            content_sha256=original_digest,
            media_type=source.media_type,
            content=source.content,
            role="source_document",
        )
    ]
    if parsed_artifact_id != original_artifact_id:
        artifacts.append(
            ParsedArtifact(
                artifact_id=parsed_artifact_id,
                content_sha256=parsed_digest,
                media_type="text/markdown; charset=utf-8",
                content=encoded,
                role="derived_document",
            )
        )
    if raw_artifact_id not in {original_artifact_id, parsed_artifact_id}:
        artifacts.append(
            ParsedArtifact(
                artifact_id=raw_artifact_id,
                content_sha256=raw_digest,
                media_type="text/markdown; charset=utf-8",
                content=raw_markdown,
                role="derived_document",
            )
        )
    artifacts.extend(image_artifacts)
    return ParseResult(
        document_ir=document_ir,
        report=report,
        artifacts=tuple(artifacts),
    )


def _sanitized_markdown_images(
    markdown: str,
) -> tuple[str, list[dict[str, int]]]:
    """替换临时图片地址，并记录每次真实语法出现的双重坐标。"""
    blocks = tuple(_readable_blocks(markdown))
    block_starts = [item[0] for item in blocks]
    pieces: list[str] = []
    occurrences: list[dict[str, int]] = []
    source_cursor = 0
    reading_cursor = 0
    for match in _IMAGE_MARKDOWN.finditer(markdown):
        before = markdown[source_cursor : match.start()]
        pieces.append(before)
        reading_cursor += len(before)
        alt = match.group(1).strip() or "无说明"
        placeholder = f"[图片：{alt}]"
        pieces.append(placeholder)
        block_index = bisect_right(block_starts, match.start()) - 1
        if block_index >= 0 and blocks[block_index][3] != "code":
            if len(occurrences) >= _MAX_IMAGE_OCCURRENCES:
                raise InvalidDocument(
                    "图片出现次数超过解析上限。",
                    stage="weknora-reading.resource",
                )
            occurrences.append(
                {
                    "raw_start_char": match.start(),
                    "raw_end_char": match.end(),
                    "reading_start_char": reading_cursor,
                    "reading_end_char": reading_cursor + len(placeholder),
                }
            )
        reading_cursor += len(placeholder)
        source_cursor = match.end()
    pieces.append(markdown[source_cursor:])
    return "".join(pieces), occurrences


def _reading_node(  # noqa: PLR0913, PLR0917
    version_id: str,
    order: int,
    text: str,
    artifact_start: int,
    artifact_end: int,
    artifact_id: str,
    heading_level: int | None,
    syntax: str,
) -> DocumentNode:
    payload = text_payload(text)
    kind = NodeKind.HEADING if heading_level is not None else NodeKind.PARAGRAPH
    structural_path = ("parsed", f"block:{order}")
    return DocumentNode(
        node_id=node_id(
            version_id,
            _PART_URI,
            structural_path,
            kind.value,
            payload.semantic_sha256,
        ),
        kind=kind,
        order=order,
        anchor=SourceAnchor(
            part_uri=_PART_URI,
            story_kind=StoryKind.BODY,
            structural_path=structural_path,
            ordinal=order,
            source_start_char=0,
            source_end_char=len(text),
        ),
        text_payload=payload,
        metadata=freeze_json_object(
            {
                "citation_basis": "parsed_artifact",
                "parsed_artifact_id": artifact_id,
                "parsed_artifact_start_char": artifact_start,
                "parsed_artifact_end_char": artifact_end,
                "reading_syntax": syntax,
                **(
                    {"heading_level": heading_level}
                    if heading_level is not None
                    else {}
                ),
            }
        ),
    )


def _readable_blocks(  # noqa: PLR0912, PLR0915
    markdown: str,
) -> Iterator[tuple[int, int, int | None, str]]:
    """按 Markdown 块语法分段，并让节点区间逐字符覆盖解析产物。"""
    lines = markdown.splitlines(keepends=True)
    starts: list[int] = []
    cursor = 0
    for line in lines:
        starts.append(cursor)
        cursor += len(line)
    index = 0
    while index < len(lines):
        start = starts[index]
        line = lines[index]
        heading = _ATX_HEADING.match(line)
        fence = _FENCE_OPEN.match(line)
        syntax = "paragraph"
        heading_level: int | None = None
        stop = index + 1
        if fence is not None:
            marker = fence.group(1)
            closing = re.compile(
                rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$"
            )
            while stop < len(lines):
                if closing.match(lines[stop].rstrip("\r\n")):
                    stop += 1
                    break
                stop += 1
            syntax = "code"
        elif heading is not None:
            heading_level = len(heading.group(1))
            syntax = "heading"
        elif (
            index + 1 < len(lines)
            and line.strip()
            and _SETEXT_HEADING.match(lines[index + 1].rstrip("\r\n"))
        ):
            marker = lines[index + 1].lstrip()[0]
            heading_level = 1 if marker == "=" else 2
            syntax = "heading"
            stop += 1
        elif (
            index + 1 < len(lines)
            and "|" in line
            and _TABLE_RULE.match(lines[index + 1].rstrip("\r\n"))
        ):
            syntax = "table"
            stop += 1
            while (
                stop < len(lines) and "|" in lines[stop] and lines[stop].strip()
            ):
                stop += 1
        elif _LIST_MARKER.match(line):
            syntax = "list"
            while (
                stop < len(lines)
                and lines[stop].strip()
                and (
                    _LIST_MARKER.match(lines[stop])
                    or lines[stop].startswith(("  ", "\t"))
                )
            ):
                stop += 1
        elif line.strip():
            while (
                stop < len(lines)
                and lines[stop].strip()
                and not (
                    _ATX_HEADING.match(lines[stop])
                    or _FENCE_OPEN.match(lines[stop])
                    or _LIST_MARKER.match(lines[stop])
                )
            ):
                stop += 1
        else:
            syntax = "spacing"
            while stop < len(lines) and not lines[stop].strip():
                stop += 1
        end = starts[stop] if stop < len(lines) else len(markdown)
        yield start, end, heading_level, syntax
        index = stop


def _image_artifact(content: bytes, mime_type: str) -> ParsedArtifact:
    digest = hashlib.sha256(content).hexdigest()
    return ParsedArtifact(
        artifact_id=f"sha256:{digest}",
        content_sha256=digest,
        media_type=mime_type,
        content=content,
        role="embedded_media",
    )
