"""Document IR 到旧 Element 的显式有损兼容 adapter。"""

from __future__ import annotations

import hashlib

from pydantic import Field, StrictInt

from rag_app.contracts import Element, ElementKind, Locator, OcrState
from rag_app.core.models import DocumentIR, DocumentNode, NodeKind
from rag_app.core.models.common import FrozenModel
from rag_app.core.ports.blob_store import BlobStorePort


class CompatibilityIssue(FrozenModel):
    """一次兼容转换中不可表达的结构事实。"""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    count: StrictInt = Field(default=1, gt=0)
    safe_message: str = Field(min_length=1, max_length=512)


class CompatibilityReport(FrozenModel):
    """IR 到旧 Element 的转换、跳过和损失汇总。"""

    converted_count: StrictInt = Field(ge=0)
    skipped_count: StrictInt = Field(ge=0)
    issues: tuple[CompatibilityIssue, ...] = ()


def document_ir_to_legacy_elements(
    document_ir: DocumentIR,
    blob_store: BlobStorePort | None = None,
) -> tuple[tuple[Element, ...], CompatibilityReport]:
    """按来源顺序转换旧能力可表达的 IR 节点。

    Args:
        document_ir: 已通过全局校验的 Document IR。
        blob_store: 图片节点需要的受控 BlobStore。

    Returns:
        旧 Element 元组和显式 CompatibilityReport。

    """
    nodes_by_id = {node.node_id: node for node in document_ir.nodes}
    ordered = sorted(
        document_ir.nodes,
        key=lambda node: (node.anchor.ordinal, node.order, node.node_id),
    )
    elements: list[Element] = []
    skipped = 0
    issue_counts: dict[str, int] = {}
    headings: list[str] = []
    for node in ordered:
        if node.kind is NodeKind.TABLE:
            _increment(issue_counts, "IR_TABLE_EXPORTED_AS_FLAT_TEXT")
            continue
        if node.kind in {
            NodeKind.TABLE_ROW,
            NodeKind.TABLE_CELL,
            NodeKind.CONTENT_CONTROL,
        }:
            skipped += 1
            _increment(issue_counts, "IR_NODE_NOT_EXPRESSIBLE_IN_LEGACY")
            continue
        element = _node_to_element(
            node,
            document_ir,
            nodes_by_id,
            tuple(headings),
            blob_store,
        )
        if element is None:
            skipped += 1
            _increment(issue_counts, "IR_IMAGE_BLOB_UNAVAILABLE")
            continue
        elements.append(element)
        if node.kind is NodeKind.HEADING and node.text:
            headings[:] = [node.text]
        if node.revision_mark is not None:
            _increment(issue_counts, "IR_REVISION_METADATA_NOT_EXPRESSIBLE")
        if node.metadata:
            metadata_keys = set(dict(node.metadata))
            represented = {
                "legacy_element_id",
                "legacy_flattened_table",
                "legacy_heading_index",
            }
            if metadata_keys - represented:
                _increment(issue_counts, "IR_METADATA_NOT_EXPRESSIBLE")
    if document_ir.relationships:
        issue_counts["IR_RELATIONSHIPS_NOT_EXPRESSIBLE"] = len(
            document_ir.relationships
        )
    issues = tuple(
        CompatibilityIssue(
            code=code,
            count=count,
            safe_message=_issue_message(code),
        )
        for code, count in sorted(issue_counts.items())
    )
    return (
        tuple(elements),
        CompatibilityReport(
            converted_count=len(elements),
            skipped_count=skipped,
            issues=issues,
        ),
    )


def _node_to_element(
    node: DocumentNode,
    document_ir: DocumentIR,
    nodes_by_id: dict[str, DocumentNode],
    heading_path: tuple[str, ...],
    blob_store: BlobStorePort | None,
) -> Element | None:
    kind = _legacy_kind(node.kind)
    if kind is None:
        return None
    text = node.text
    metadata = dict(node.metadata)
    legacy_id = metadata.get("legacy_element_id")
    resolved_legacy_id = legacy_id if isinstance(legacy_id, str) else None
    if resolved_legacy_id is None and node.parent_node_id is not None:
        parent = nodes_by_id[node.parent_node_id]
        parent_id = dict(parent.metadata).get("legacy_element_id")
        if isinstance(parent_id, str):
            resolved_legacy_id = parent_id
    element_id = (
        resolved_legacy_id
        or f"element_{node.node_id.removeprefix('node_')}"
    )
    locator = Locator(
        file_path=document_ir.source.display_name,
        heading_path=(
            (*heading_path, text)
            if node.kind is NodeKind.HEADING
            else heading_path
        ),
        heading_index=_legacy_heading_index(node, nodes_by_id),
        paragraph_index=(
            _positive(node.anchor.paragraph_index)
            if node.kind in {NodeKind.PARAGRAPH, NodeKind.LIST_ITEM}
            else None
        ),
        table_index=(
            _positive(node.anchor.table_index)
            if node.kind is NodeKind.TABLE_REPRESENTATION
            else None
        ),
        image_index=_image_index(node),
        fragment=_fragment(node),
    )
    if node.kind is not NodeKind.IMAGE:
        return Element(
            element_id=element_id,
            kind=kind,
            text=text,
            locator=locator,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            list_level=(
                node.list_attributes.level
                if node.list_attributes is not None
                else None
            ),
        )
    if blob_store is None or node.image_attributes is None:
        return None
    blob = blob_store.get(node.image_attributes.blob_ref)
    if (
        blob is None
        or blob.content_sha256 != node.image_attributes.content_sha256
    ):
        return None
    return Element(
        element_id=element_id,
        kind=ElementKind.IMAGE,
        text="",
        locator=locator,
        content_sha256=blob.content_sha256,
        media_type=blob.media_type,
        media_name=node.image_attributes.display_name,
        binary_data=blob.content,
        ocr_state=OcrState.PENDING,
    )


def _legacy_kind(kind: NodeKind) -> ElementKind | None:
    if kind is NodeKind.HEADING:
        return ElementKind.HEADING
    if kind in {NodeKind.PARAGRAPH, NodeKind.LIST_ITEM}:
        return ElementKind.PARAGRAPH
    if kind is NodeKind.TABLE_REPRESENTATION:
        return ElementKind.TABLE
    if kind is NodeKind.IMAGE:
        return ElementKind.IMAGE
    return None


def _fragment(node: DocumentNode) -> str:
    if node.text:
        return node.text[:240]
    if node.image_attributes is not None:
        return (node.image_attributes.display_name or "image")[:240]
    return node.kind.value


def _positive(value: int | None) -> int | None:
    if value is None:
        return None
    return max(1, value)


def _legacy_heading_index(
    node: DocumentNode,
    nodes_by_id: dict[str, DocumentNode],
) -> int | None:
    value = dict(node.metadata).get("legacy_heading_index")
    if value is None and node.parent_node_id is not None:
        value = dict(nodes_by_id[node.parent_node_id].metadata).get(
            "legacy_heading_index"
        )
    if isinstance(value, int):
        return _positive(value)
    return None


def _image_index(node: DocumentNode) -> int | None:
    if node.kind is not NodeKind.IMAGE:
        return None
    segment = node.anchor.structural_path[-1]
    _, _, value = segment.partition(":")
    return int(value) if value.isdigit() and int(value) > 0 else 1


def _increment(counts: dict[str, int], code: str) -> None:
    counts[code] = counts.get(code, 0) + 1


def _issue_message(code: str) -> str:
    messages = {
        "IR_IMAGE_BLOB_UNAVAILABLE": "图片 Blob 不可用，未生成旧 Element。",
        "IR_METADATA_NOT_EXPRESSIBLE": "IR metadata 无法写入旧 Element。",
        "IR_NODE_NOT_EXPRESSIBLE_IN_LEGACY": (
            "复杂 IR 节点无法由旧 Element 表达。"
        ),
        "IR_RELATIONSHIPS_NOT_EXPRESSIBLE": (
            "IR relationship 无法写入旧 Element。"
        ),
        "IR_REVISION_METADATA_NOT_EXPRESSIBLE": "修订标记无法写入旧 Element。",
        "IR_TABLE_EXPORTED_AS_FLAT_TEXT": "表格仅以扁平文本导出。",
    }
    return messages[code]


class LegacyElementCompatibilityAdapter:
    """提供面向依赖注入的 IR 到旧 Element 同步接口。"""

    def __init__(self, blob_store: BlobStorePort | None = None) -> None:
        """保存可选图片 BlobStore。

        Args:
            blob_store: 读取图片二进制的受控 Store。

        Returns:
            无返回值。

        """
        self._blob_store = blob_store

    def convert(
        self,
        document_ir: DocumentIR,
    ) -> tuple[tuple[Element, ...], CompatibilityReport]:
        """转换一个 IR。

        Args:
            document_ir: 已校验的格式中立 IR。

        Returns:
            可表达 Element 与兼容报告。

        """
        return document_ir_to_legacy_elements(document_ir, self._blob_store)
