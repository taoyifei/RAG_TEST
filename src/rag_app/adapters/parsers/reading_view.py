"""将可读文本映射到不可变解析产物，而非杜撰原文件坐标。"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator

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
    # 图片字节单独持久化，正文只保留可读占位，不暴露 sidecar 临时 URL。
    visible_text = _IMAGE_MARKDOWN.sub(
        lambda match: f"[图片：{match.group(1).strip() or '无说明'}]",
        markdown.lstrip("\ufeff"),
    )
    encoded = visible_text.encode("utf-8")
    if not visible_text.strip() or len(encoded) > _MAX_MARKDOWN_BYTES:
        raise InvalidDocument(
            "解析正文为空或超过 16 MiB 上限。", stage="weknora-reading.content"
        )
    original_digest = hashlib.sha256(source.content).hexdigest()
    parsed_digest = hashlib.sha256(encoded).hexdigest()
    original_artifact_id = f"sha256:{original_digest}"
    parsed_artifact_id = f"sha256:{parsed_digest}"
    version_id = document_version_id(
        context.document.document_id, original_digest
    )
    blocks = _readable_blocks(visible_text)
    nodes = tuple(
        _reading_node(
            version_id,
            index,
            visible_text[start:end],
            start,
            end,
            parsed_artifact_id,
            heading,
        )
        for index, (start, end, heading) in enumerate(blocks)
    )
    if not nodes:
        raise InvalidDocument(
            "解析正文没有可索引段落。", stage="weknora-reading.content"
        )
    image_occurrences = tuple(
        _image_artifact(content, mime_type) for mime_type, content in images
    )
    image_ids = tuple(item.artifact_id for item in image_occurrences)
    image_artifacts = tuple(
        {item.artifact_id: item for item in image_occurrences}.values()
    )
    issues = (
        (
            ParseIssue(
                code="PARSED_MEDIA_TEXT_ONLY",
                severity="warning",
                action="persist_inline_media_without_citation_claim",
                safe_message="图片已保存为独立产物；文本引用不声称识别图片内容。",
                count=len(image_occurrences),
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
        media_count=len(image_occurrences),
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
                "embedded_media_artifact_ids": list(image_ids),
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
    artifacts.extend(image_artifacts)
    return ParseResult(
        document_ir=document_ir,
        report=report,
        artifacts=tuple(artifacts),
    )


def _reading_node(  # noqa: PLR0913, PLR0917
    version_id: str,
    order: int,
    text: str,
    artifact_start: int,
    artifact_end: int,
    artifact_id: str,
    heading: bool,
) -> DocumentNode:
    payload = text_payload(text)
    kind = NodeKind.HEADING if heading else NodeKind.PARAGRAPH
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
            }
        ),
    )


def _readable_blocks(markdown: str) -> Iterator[tuple[int, int, bool]]:
    """按原产物字符偏移切分空行和标题，不改变块内文字。"""
    block_start: int | None = None
    offset = 0
    for line in markdown.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        if not line.strip():
            if block_start is not None:
                yield block_start, line_start, False
                block_start = None
            continue
        if line.lstrip().startswith("# ") or re.match(r"^#{2,6} ", line):
            if block_start is not None:
                yield block_start, line_start, False
                block_start = None
            yield line_start, offset, True
            continue
        if block_start is None:
            block_start = line_start
    if block_start is not None:
        yield block_start, len(markdown), False


def _image_artifact(content: bytes, mime_type: str) -> ParsedArtifact:
    digest = hashlib.sha256(content).hexdigest()
    return ParsedArtifact(
        artifact_id=f"sha256:{digest}",
        content_sha256=digest,
        media_type=mime_type,
        content=content,
        role="embedded_media",
    )
