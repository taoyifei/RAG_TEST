"""格式中立的文档身份、来源、节点和解析报告。"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from enum import StrEnum
from typing import Self

from pydantic import Field, StrictInt, field_validator, model_validator

from rag_app.core.identifiers import canonical_json
from rag_app.core.models.common import FrozenModel, MetadataModel

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ProjectScope(FrozenModel):
    """一次用例允许访问的项目边界。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")


class KnowledgeBaseScope(FrozenModel):
    """一次用例允许访问的知识库边界。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")


class DocumentRef(FrozenModel):
    """稳定逻辑文档引用，显示名不参与身份。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    display_name: str = Field(min_length=1, max_length=512)


class DocumentVersionRef(FrozenModel):
    """绑定内容摘要的不可变文档版本引用。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    content_sha256: str = Field(pattern=_SHA256_PATTERN)


class DocumentSource(MetadataModel):
    """不信任显示名或扩展名的受控文档来源描述。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    display_name: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    extension: str = Field(pattern=r"^\.[a-z0-9]{1,16}$")
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: StrictInt = Field(ge=0)
    blob_ref: str | None = Field(default=None, min_length=1, max_length=512)
    materialized_path: str | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        repr=False,
    )

    @field_validator("extension", mode="before")
    @classmethod
    def _normalize_extension(cls, value: object) -> object:
        if isinstance(value, str):
            return value.casefold()
        return value

    @model_validator(mode="after")
    def _validate_source_reference(self) -> Self:
        if self.blob_ref is None and self.materialized_path is None:
            raise ValueError("DocumentSource 必须提供 blob_ref 或受控本地路径。")
        return self


class StoryKind(StrEnum):
    """节点所属的文档 story。"""

    BODY = "body"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"
    ENDNOTE = "endnote"
    COMMENT = "comment"
    TEXT_BOX = "text_box"


class NodeKind(StrEnum):
    """不绑定具体文件格式的节点类型。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_REPRESENTATION = "table_representation"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    IMAGE = "image"
    CONTENT_CONTROL = "content_control"


class SourceAnchor(FrozenModel):
    """由稳定逻辑段组成的格式内来源位置。"""

    part_uri: str = Field(min_length=1, max_length=512)
    story_kind: StoryKind
    structural_path: tuple[str, ...] = Field(min_length=1)
    ordinal: StrictInt = Field(ge=0)
    section_index: StrictInt | None = Field(default=None, ge=0)
    paragraph_index: StrictInt | None = Field(default=None, ge=0)
    table_index: StrictInt | None = Field(default=None, ge=0)
    row_index: StrictInt | None = Field(default=None, ge=0)
    cell_index: StrictInt | None = Field(default=None, ge=0)
    relationship_id: str | None = Field(default=None, min_length=1)
    source_start_char: StrictInt | None = Field(default=None, ge=0)
    source_end_char: StrictInt | None = Field(default=None, ge=0)

    @field_validator("part_uri")
    @classmethod
    def _validate_part_uri(cls, value: str) -> str:
        if "\\" in value or ".." in value.split("/"):
            raise ValueError("part_uri 必须是安全的包内逻辑 URI。")
        return value

    @field_validator("structural_path")
    @classmethod
    def _validate_structural_path(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not item or "/" in item or "\\" in item for item in value):
            raise ValueError("structural_path 必须由非空稳定逻辑段组成。")
        return value

    @model_validator(mode="after")
    def _validate_character_range(self) -> Self:
        start = self.source_start_char
        end = self.source_end_char
        if (start is None) != (end is None):
            raise ValueError("source char range 必须同时提供起点和终点。")
        if start is not None and end is not None and end < start:
            raise ValueError("source char range 终点不能早于起点。")
        return self


class TextPayload(FrozenModel):
    """区分显示文本和最小规范化语义文本。"""

    exact_text: str = Field(default="", repr=False)
    semantic_text: str = Field(default="", repr=False)
    exact_sha256: str = Field(pattern=_SHA256_PATTERN)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_hashes(self) -> Self:
        exact = hashlib.sha256(self.exact_text.encode("utf-8")).hexdigest()
        semantic = hashlib.sha256(
            self.semantic_text.encode("utf-8")
        ).hexdigest()
        if self.exact_sha256 != exact:
            raise ValueError("exact_text SHA-256 不一致。")
        if self.semantic_sha256 != semantic:
            raise ValueError("semantic_text SHA-256 不一致。")
        return self


class RevisionMark(FrozenModel):
    """保留修订语义但不绑定 OOXML 字段的标记。"""

    kind: str = Field(min_length=1, max_length=80)
    author: str | None = Field(default=None, max_length=256)
    timestamp: str | None = Field(default=None, max_length=80)


class ListAttributes(FrozenModel):
    """列表节点的格式中立属性。"""

    level: StrictInt = Field(ge=0, le=32)
    ordered: bool | None = None
    marker: str | None = Field(default=None, max_length=80)


class CellGrid(FrozenModel):
    """表格单元格的逻辑网格坐标。"""

    row_index: StrictInt = Field(ge=0)
    column_index: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(default=1, gt=0)
    column_span: StrictInt = Field(default=1, gt=0)


class ImageAttributes(FrozenModel):
    """只保存 BlobRef 和非敏感图片元数据。"""

    blob_ref: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    display_name: str | None = Field(default=None, max_length=512)
    alt_text: str | None = Field(default=None, max_length=2048)
    width_px: StrictInt | None = Field(default=None, gt=0)
    height_px: StrictInt | None = Field(default=None, gt=0)


class DocumentRelationship(MetadataModel):
    """两个 IR 节点之间的显式关系。"""

    relationship_id: str = Field(min_length=1, max_length=256)
    relationship_type: str = Field(min_length=1, max_length=256)
    source_node_id: str = Field(pattern=r"^node_[0-9a-f]{32}$")
    target_node_id: str = Field(pattern=r"^node_[0-9a-f]{32}$")


class DocumentNode(MetadataModel):
    """扁平节点表中的一个格式中立节点。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    node_id: str = Field(pattern=r"^node_[0-9a-f]{32}$")
    kind: NodeKind
    parent_node_id: str | None = Field(
        default=None,
        pattern=r"^node_[0-9a-f]{32}$",
    )
    child_ids: tuple[str, ...] = ()
    order: StrictInt = Field(ge=0)
    anchor: SourceAnchor
    text_payload: TextPayload | None = None
    revision_mark: RevisionMark | None = None
    list_attributes: ListAttributes | None = None
    cell_grid: CellGrid | None = None
    image_attributes: ImageAttributes | None = None

    @field_validator("child_ids")
    @classmethod
    def _validate_child_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("child_ids 禁止重复。")
        if any(not item.startswith("node_") for item in value):
            raise ValueError("child_ids 必须使用 node_ 前缀。")
        return value

    @model_validator(mode="after")
    def _validate_attribute_combination(self) -> Self:
        if self.list_attributes is not None and self.kind is not NodeKind.LIST_ITEM:
            raise ValueError("只有 ListItem 节点允许 list_attributes。")
        if self.cell_grid is not None and self.kind is not NodeKind.TABLE_CELL:
            raise ValueError("只有 TableCell 节点允许 cell_grid。")
        if self.image_attributes is not None and self.kind is not NodeKind.IMAGE:
            raise ValueError("只有 Image 节点允许 image_attributes。")
        if self.kind is NodeKind.IMAGE and self.image_attributes is None:
            raise ValueError("Image 节点必须提供 image_attributes。")
        return self

    @property
    def node_type(self) -> str:
        """返回 P01 兼容节点类型。

        Args:
            无参数；读取当前节点。

        Returns:
            NodeKind 的稳定字符串值。

        """
        return self.kind.value

    @property
    def structural_path(self) -> tuple[str, ...]:
        """返回 P01 兼容结构路径。

        Args:
            无参数；读取当前节点。

        Returns:
            SourceAnchor 中的稳定逻辑路径。

        """
        return self.anchor.structural_path

    @property
    def text(self) -> str:
        """返回 P01 兼容语义文本。

        Args:
            无参数；读取当前节点。

        Returns:
            semantic_text；无文本载荷时为空字符串。

        """
        if self.text_payload is None:
            return ""
        return self.text_payload.semantic_text

    @property
    def content_sha256(self) -> str:
        """返回 P01 兼容内容摘要。

        Args:
            无参数；读取当前节点。

        Returns:
            语义文本、图片或空内容的 SHA-256。

        """
        if self.text_payload is not None:
            return self.text_payload.semantic_sha256
        if self.image_attributes is not None:
            return self.image_attributes.content_sha256
        return hashlib.sha256(b"").hexdigest()


class ParseIssue(MetadataModel):
    """不含原文的单类解析问题。"""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    severity: str = Field(pattern=r"^(info|warning|error)$")
    action: str = Field(min_length=1, max_length=120)
    anchor: SourceAnchor | None = None
    count: StrictInt = Field(default=1, gt=0)
    safe_message: str = Field(min_length=1, max_length=512)


class ParseReport(FrozenModel):
    """解析覆盖率、问题聚合和非敏感耗时。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    parser_id: str = Field(default="unknown", min_length=1, max_length=120)
    parser_version: str = Field(default="unknown", min_length=1, max_length=120)
    node_count: StrictInt = Field(default=0, ge=0)
    visible_text_nodes: StrictInt = Field(default=0, ge=0)
    represented_visible_text_nodes: StrictInt = Field(default=0, ge=0)
    unsupported_with_text: StrictInt = Field(default=0, ge=0)
    unsupported_with_media: StrictInt = Field(default=0, ge=0)
    story_counts: tuple[tuple[str, StrictInt], ...] = ()
    issues: tuple[ParseIssue, ...] = ()
    elapsed_seconds: float = Field(default=0.0, ge=0.0, exclude=True)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_coverage(self) -> Self:
        if self.represented_visible_text_nodes > self.visible_text_nodes:
            raise ValueError("represented count 不能超过 visible count。")
        keys = [key for key, _ in self.story_counts]
        if len(keys) != len(set(keys)):
            raise ValueError("story_counts 禁止重复 story。")
        return self

    @property
    def coverage(self) -> float:
        """返回可见文本节点覆盖率。

        Args:
            无参数；读取当前报告。

        Returns:
            零到一的表示覆盖率；无可见文本时为一。

        """
        if self.visible_text_nodes == 0:
            return 1.0
        return self.represented_visible_text_nodes / self.visible_text_nodes


class DocumentIR(MetadataModel):
    """扁平节点表、显式关系和解析报告组成的 V1 IR。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    source: DocumentSource
    document: DocumentRef
    version: DocumentVersionRef
    root_node_ids: tuple[str, ...]
    nodes: tuple[DocumentNode, ...]
    relationships: tuple[DocumentRelationship, ...] = ()
    parse_report: ParseReport

    @model_validator(mode="after")
    def _validate_ir(self) -> Self:
        validate_document_ir(self)
        return self


class ParseSource(FrozenModel):
    """ParserPort 的受控字节输入及格式元数据。"""

    media_type: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=512)
    content: bytes = Field(repr=False)
    extension: str = ".docx"
    blob_ref: str | None = Field(default=None, max_length=512)

    @field_validator("extension", mode="before")
    @classmethod
    def _normalize_source_extension(cls, value: object) -> object:
        if isinstance(value, str):
            return value.casefold()
        return value


class ParseResult(FrozenModel):
    """ParserPort 的 IR 与报告结果。"""

    document_ir: DocumentIR
    report: ParseReport

    @model_validator(mode="after")
    def _validate_report(self) -> Self:
        if self.report != self.document_ir.parse_report:
            raise ValueError("ParseResult report 必须与 DocumentIR 一致。")
        return self


def text_payload(exact_text: str, semantic_text: str | None = None) -> TextPayload:
    """创建带双 SHA-256 的文本载荷。

    Args:
        exact_text: 解析得到的有效可见字符。
        semantic_text: 最小规范化文本；缺失时使用 exact_text。

    Returns:
        已校验两个文本摘要的载荷。

    """
    semantic = exact_text if semantic_text is None else semantic_text
    return TextPayload(
        exact_text=exact_text,
        semantic_text=semantic,
        exact_sha256=hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
        semantic_sha256=hashlib.sha256(semantic.encode("utf-8")).hexdigest(),
    )


def validate_document_ir(document_ir: DocumentIR) -> None:
    """以 O(n) 校验节点、父子、顺序、环和关系不变量。

    Args:
        document_ir: 待校验的 V1 Document IR。

    Returns:
        无返回值。

    Raises:
        ValueError: 任一全局结构或覆盖率不变量不成立。

    """
    nodes_by_id = {node.node_id: node for node in document_ir.nodes}
    if len(nodes_by_id) != len(document_ir.nodes):
        raise ValueError("DocumentIR node_id 必须唯一。")
    root_ids = set(document_ir.root_node_ids)
    if len(root_ids) != len(document_ir.root_node_ids):
        raise ValueError("root_node_ids 禁止重复。")
    if not root_ids.issubset(nodes_by_id):
        raise ValueError("DocumentIR root 必须存在。")
    observed_roots = {
        node.node_id for node in document_ir.nodes if node.parent_node_id is None
    }
    if observed_roots != root_ids:
        raise ValueError("root 必须且只能包含 parent=None 的节点。")

    children_by_parent: dict[str | None, list[DocumentNode]] = defaultdict(list)
    for node in document_ir.nodes:
        if node.parent_node_id is not None:
            parent = nodes_by_id.get(node.parent_node_id)
            if parent is None:
                raise ValueError("非 root 节点的 parent 必须存在。")
            if node.node_id not in parent.child_ids:
                raise ValueError("child.parent 与 parent.child_ids 必须对称。")
        for child_id in node.child_ids:
            child = nodes_by_id.get(child_id)
            if child is None or child.parent_node_id != node.node_id:
                raise ValueError("parent.child_ids 与 child.parent 必须对称。")
        children_by_parent[node.parent_node_id].append(node)

    for siblings in children_by_parent.values():
        orders = sorted(node.order for node in siblings)
        if orders != list(range(len(siblings))):
            raise ValueError("同一 parent 的 order 必须唯一且从零连续。")

    colors: dict[str, int] = {}
    for start_id in nodes_by_id:
        trail: list[str] = []
        current_id: str | None = start_id
        while current_id is not None and colors.get(current_id, 0) == 0:
            colors[current_id] = 1
            trail.append(current_id)
            current_id = nodes_by_id[current_id].parent_node_id
        if current_id is not None and colors.get(current_id) == 1:
            raise ValueError("DocumentIR 节点关系禁止成环。")
        for visited_id in trail:
            colors[visited_id] = 2

    for relationship in document_ir.relationships:
        if (
            relationship.source_node_id not in nodes_by_id
            or relationship.target_node_id not in nodes_by_id
        ):
            raise ValueError("relationship source/target 节点必须存在。")
    if document_ir.source.document_id != document_ir.document.document_id:
        raise ValueError("DocumentSource 与 DocumentRef 身份不一致。")
    if document_ir.source.document_version_id != document_ir.version.document_version_id:
        raise ValueError("DocumentSource 与 DocumentVersionRef 身份不一致。")
    if document_ir.source.content_sha256 != document_ir.version.content_sha256:
        raise ValueError("DocumentSource 与 DocumentVersionRef 摘要不一致。")
    if document_ir.parse_report.node_count != len(document_ir.nodes):
        raise ValueError("ParseReport node_count 必须等于节点数量。")


def canonical_document_ir_json(
    document_ir: DocumentIR,
    *,
    include_content: bool = True,
) -> str:
    """序列化稳定且不含本地路径或二进制数据的 IR JSON。

    Args:
        document_ir: 已通过全局校验的 IR。
        include_content: 是否保留 exact/semantic 文本。

    Returns:
        字段排序、无 elapsed 的规范化 JSON。

    """
    payload = document_ir.model_dump(mode="json", exclude_none=False)
    if not include_content:
        for node in payload["nodes"]:
            text = node.get("text_payload")
            if isinstance(text, dict):
                text.pop("exact_text", None)
                text.pop("semantic_text", None)
    return canonical_json(payload)
