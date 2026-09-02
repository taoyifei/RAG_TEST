"""保存 DOCX 媒体实例并处理未知可索引结构。"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import PurePosixPath
from typing import Protocol

from lxml import etree

from rag_app.adapters.parsers.docx.builder import IrNodeBuilder
from rag_app.adapters.parsers.docx.drawings import (
    DrawingReference,
    image_references,
)
from rag_app.adapters.parsers.docx.issues import IssueCollector
from rag_app.adapters.parsers.docx.namespaces import WORD, local_name, qn
from rag_app.adapters.parsers.docx.package import DocxPackage
from rag_app.core.errors import InvalidDocument, UnsupportedDocumentFeature
from rag_app.core.models import (
    ImageAttributes,
    NodeKind,
    ParsedArtifact,
    SourceAnchor,
    StoryKind,
)
from rag_app.core.policies import (
    ImagesPolicy,
    ParsingMode,
    ParsingPolicy,
    UnknownIndexableContentPolicy,
)

_EMU_PER_PIXEL = 9_525


class BlockMediaContext(Protocol):
    """媒体解析需要的最小 block 上下文。"""

    package: DocxPackage
    policy: ParsingPolicy
    builder: IrNodeBuilder
    issues: IssueCollector
    artifacts: dict[str, ParsedArtifact]

    def anchor(
        self,
        *,
        part_uri: str,
        story_kind: StoryKind,
        structural_path: tuple[str, ...],
        relationship_id: str | None = None,
    ) -> SourceAnchor:
        """创建媒体来源锚点。

        Args:
            part_uri: 当前 Part URI。
            story_kind: 当前 story 类型。
            structural_path: 稳定结构路径。
            relationship_id: 可选媒体关系 ID。

        Returns:
            媒体来源锚点。

        """
        ...


def parse_image(  # noqa: PLR0913
    parser: BlockMediaContext,
    reference: DrawingReference,
    *,
    parent_node_id: str,
    part_uri: str,
    story_kind: StoryKind,
    structural_path: tuple[str, ...],
) -> None:
    """保存一个图片实例，并按内容摘要复用 Blob。

    Args:
        parser: 共享 block 解析上下文。
        reference: Drawing/VML 图片引用。
        parent_node_id: 图片所属父节点 ID。
        part_uri: 当前 Part URI。
        story_kind: 当前 story 类型。
        structural_path: 图片实例的稳定结构路径。

    Returns:
        无返回值。

    """
    if parser.policy.images is ImagesPolicy.REJECT:
        raise InvalidDocument(
            "ParsingPolicy 拒绝 DOCX 图片。",
            stage="docx-ooxml-v4.image",
        )
    relationship = parser.package.relationship(
        part_uri,
        reference.relationship_id,
    )
    if relationship is None:
        parser.issues.add(
            "DOCX_IMAGE_RELATIONSHIP_MISSING",
            action="image_omitted",
            message="图片引用的 relationship 不存在。",
            unsupported_media=1,
        )
        return
    if relationship.target_mode == "External":
        parser.issues.add(
            "DOCX_EXTERNAL_IMAGE_NOT_DOWNLOADED",
            action="metadata_only",
            message="外链图片未下载。",
            unsupported_media=1,
        )
        return
    target = relationship.target_part_uri
    if target is None:
        return
    payload = parser.package.read(target)
    digest = hashlib.sha256(payload).hexdigest()
    part = parser.package.catalog.part(target)
    media_type = (
        "application/octet-stream" if part is None else part.content_type
    )
    if media_type == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(target)
        media_type = guessed or media_type
    if not media_type.startswith("image/"):
        parser.issues.add(
            "DOCX_IMAGE_MEDIA_TYPE_UNKNOWN",
            action="blob_preserved_with_type",
            message="图片媒体类型未知，按原始字节保存。",
        )
    artifact_id = f"sha256:{digest}"
    parser.artifacts.setdefault(
        artifact_id,
        ParsedArtifact(
            artifact_id=artifact_id,
            content_sha256=digest,
            media_type=media_type,
            content=payload,
            role="embedded_media",
        ),
    )
    attributes = ImageAttributes(
        blob_ref=artifact_id,
        media_type=media_type,
        content_sha256=digest,
        display_name=reference.name or PurePosixPath(target).name,
        alt_text=reference.alt_text or reference.title,
        width_px=_emu_to_pixels(reference.width_emu),
        height_px=_emu_to_pixels(reference.height_emu),
    )
    parser.builder.add(
        kind=NodeKind.IMAGE,
        parent_node_id=parent_node_id,
        anchor=parser.anchor(
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
            relationship_id=reference.relationship_id,
        ),
        image_attributes=attributes,
        metadata={
            "placement": reference.placement,
            "media_part_uri": target,
            "width_emu": reference.width_emu,
            "height_emu": reference.height_emu,
        },
    )


def preserve_unsupported(  # noqa: PLR0913
    parser: BlockMediaContext,
    node: etree._Element,
    *,
    parent_node_id: str | None,
    part_uri: str,
    story_kind: StoryKind,
    structural_path: tuple[str, ...],
    force_indexable: bool = False,
) -> None:
    """按严格或 best-effort 策略处理未知结构。

    Args:
        parser: 共享 block 解析上下文。
        node: 未知 OOXML 元素。
        parent_node_id: 可选父节点 ID。
        part_uri: 当前 Part URI。
        story_kind: 当前 story 类型。
        structural_path: 稳定结构路径。
        force_indexable: 是否强制视为含可索引证据。

    Returns:
        无返回值。

    """
    visible_text = "".join(
        item.text or ""
        for item in node.iter(qn(WORD, "t"))
        if (item.text or "").strip()
    )
    indexable = (
        force_indexable
        or bool(visible_text)
        or bool(image_references(node))
        or any(True for _ in node.iter(qn(WORD, "tbl")))
    )
    if not indexable:
        parser.issues.add(
            "DOCX_UNKNOWN_WRAPPER_SKIPPED",
            severity="info",
            action="skip_no_evidence",
            message="未知 wrapper 不含可索引证据。",
        )
        return
    should_reject = (
        parser.policy.mode is ParsingMode.STRICT
        or parser.policy.unknown_indexable_content
        is UnknownIndexableContentPolicy.REJECT
    )
    if should_reject:
        raise UnsupportedDocumentFeature(
            "DOCX 包含无法完整表示的可索引结构。",
            stage="docx-ooxml-v4.semantic",
            details={"element": local_name(node)},
        )
    digest = hashlib.sha256(visible_text.encode("utf-8")).hexdigest()
    parser.builder.add(
        kind=NodeKind.UNSUPPORTED,
        parent_node_id=parent_node_id,
        anchor=parser.anchor(
            part_uri=part_uri,
            story_kind=story_kind,
            structural_path=structural_path,
        ),
        metadata={
            "element": local_name(node),
            "visible_text_sha256": digest,
            "visible_characters": len(visible_text),
        },
    )
    parser.issues.add(
        "DOCX_UNSUPPORTED_INDEXABLE_CONTENT",
        action="unsupported_node",
        message="未知可索引结构以 UnsupportedNode 保留。",
        unsupported_text=int(bool(visible_text) or force_indexable),
        unsupported_media=int(bool(image_references(node))),
    )


def validate_hyperlinks(
    parser: BlockMediaContext,
    part_uri: str,
    metadata: dict[str, object],
) -> None:
    """验证超链接关系并只保留外部协议类型。

    Args:
        parser: 共享 block 解析上下文。
        part_uri: 超链接所在 Part URI。
        metadata: 待补充安全关系信息的段落 metadata。

    Returns:
        无返回值。

    """
    values = metadata.get("hyperlink_relationship_ids", ())
    if not isinstance(values, (tuple, list)):
        return
    schemes: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        relationship = parser.package.relationship(part_uri, value)
        if relationship is None:
            parser.issues.add(
                "DOCX_HYPERLINK_RELATIONSHIP_MISSING",
                action="display_text_only",
                message="超链接 relationship 不存在。",
            )
            continue
        if relationship.external_scheme:
            schemes.append(relationship.external_scheme)
    if schemes:
        metadata["external_hyperlink_schemes"] = tuple(schemes)


def _emu_to_pixels(value: int | None) -> int | None:
    if value is None or value <= 0:
        return None
    return max(1, round(value / _EMU_PER_PIXEL))
