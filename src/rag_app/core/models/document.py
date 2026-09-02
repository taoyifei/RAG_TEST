"""格式中立的文档身份、来源、节点和解析报告。"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from rag_app.core.identifiers import canonical_json
from rag_app.core.models.common import FrozenModel, MetadataModel

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_DISPLAY_EXTENSION_LENGTH = 16
_SAFE_NODE_METADATA_KEYS = frozenset(
    {
        "break_type",
        "column_span",
        "external_hyperlink_schemes",
        "grid_after",
        "grid_before",
        "heading_level",
        "legacy_flattened_table",
        "num_id",
        "repeated_header",
        "row_span",
        "style_hidden",
        "style_quick_format",
        "style_unhide_when_used",
        "vmerge_anchor_node_id",
    }
)


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


class ParseContext(FrozenModel):
    """不进入解析策略指纹的运行时文档身份。"""

    document: DocumentRef


def validate_document_ref_uniqueness(
    documents: Sequence[DocumentRef],
) -> None:
    """确认 document_id 在项目和知识库边界之间全局唯一。

    Args:
        documents: 待创建或导入的逻辑文档引用。

    Returns:
        无返回值；全部引用通过全局唯一性检查。

    Raises:
        ValueError: 同一 document_id 被绑定到不同 project/KB。

    """
    scopes: dict[str, tuple[str, str]] = {}
    for document in documents:
        scope = (document.project_id, document.knowledge_base_id)
        existing = scopes.setdefault(document.document_id, scope)
        if existing != scope:
            raise ValueError("document_id 必须跨 project/KB 全局唯一。")


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
            raise ValueError(
                "DocumentSource 必须提供 blob_ref 或受控本地路径。"
            )
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
    SECTION = "section"
    BREAK = "break"
    NOTE = "note"
    COMMENT = "comment"
    UNSUPPORTED = "unsupported"


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
    ordinal: StrictInt | None = Field(default=None, ge=0)
    restart_group: str | None = Field(default=None, max_length=120)


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

    @model_validator(mode="before")
    @classmethod
    def _migrate_p01_shape(cls, value: object) -> object:
        """把 P01 provisional 节点输入迁移为 V1 字段。

        Args:
            value: Pydantic 收到的节点输入。

        Returns:
            原生 V1 输入，或由 P01 字段显式转换的 V1 输入。

        """
        if not isinstance(value, dict) or "kind" in value:
            return value
        if "node_type" not in value or "structural_path" not in value:
            return value
        migrated = dict(value)
        node_type = str(migrated.pop("node_type"))
        structural_path = tuple(migrated.pop("structural_path"))
        exact_text = str(migrated.pop("text", ""))
        expected_hash = str(migrated.pop("content_sha256", ""))
        metadata = dict(migrated.get("metadata", ()))
        list_level = metadata.get("list_level")
        kind = _p01_node_kind(node_type, list_level)
        payload = text_payload(exact_text)
        if (
            kind is not NodeKind.IMAGE
            and payload.semantic_sha256 != expected_hash
        ):
            raise ValueError("P01 DocumentNode 文本摘要不一致。")
        if not structural_path:
            structural_path = ("legacy", f"node:{migrated['node_id']}")
        migrated.update(
            kind=kind,
            order=0,
            anchor=SourceAnchor(
                part_uri="/legacy/document",
                story_kind=StoryKind.BODY,
                structural_path=structural_path,
                ordinal=0,
                source_start_char=0 if exact_text else None,
                source_end_char=len(exact_text) if exact_text else None,
            ),
            text_payload=(payload if kind is not NodeKind.IMAGE else None),
            list_attributes=(
                ListAttributes(level=list_level)
                if kind is NodeKind.LIST_ITEM
                and isinstance(list_level, int)
                else None
            ),
            image_attributes=(
                ImageAttributes(
                    blob_ref=f"legacy-unpersisted:{expected_hash}",
                    media_type=str(
                        metadata.get("media_type")
                        or "application/octet-stream"
                    ),
                    content_sha256=expected_hash,
                    display_name=(
                        str(metadata["media_name"])
                        if metadata.get("media_name") is not None
                        else None
                    ),
                )
                if kind is NodeKind.IMAGE
                else None
            ),
        )
        return migrated

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
        if (
            self.list_attributes is not None
            and self.kind is not NodeKind.LIST_ITEM
        ):
            raise ValueError("只有 ListItem 节点允许 list_attributes。")
        if self.cell_grid is not None and self.kind is not NodeKind.TABLE_CELL:
            raise ValueError("只有 TableCell 节点允许 cell_grid。")
        if (
            self.image_attributes is not None
            and self.kind is not NodeKind.IMAGE
        ):
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
    part_count: StrictInt = Field(default=0, ge=0)
    relationship_count: StrictInt = Field(default=0, ge=0)
    media_count: StrictInt = Field(default=0, ge=0)
    revision_count: StrictInt = Field(default=0, ge=0)
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_p01_shape(cls, value: object) -> object:
        """为 P01 的 document/version/nodes 构造补齐 V1 外壳。

        Args:
            value: Pydantic 收到的 DocumentIR 输入。

        Returns:
            原生 V1 输入，或补齐 source、roots 和 report 的输入。

        """
        if not isinstance(value, dict) or "source" in value:
            return value
        required = {"document", "version", "nodes"}
        if not required.issubset(value):
            return value
        migrated = dict(value)
        document = migrated["document"]
        version = migrated["version"]
        nodes = tuple(migrated["nodes"])
        document_id = str(_field_value(document, "document_id"))
        version_id = str(_field_value(version, "document_version_id"))
        content_hash = str(_field_value(version, "content_sha256"))
        display_name = str(_field_value(document, "display_name"))
        extension = _display_extension(display_name)
        root_ids = tuple(
            str(_field_value(node, "node_id"))
            for node in nodes
            if _field_value(node, "parent_node_id", None) is None
        )
        visible = sum(bool(_p01_node_text(node).strip()) for node in nodes)
        migrated.update(
            source=DocumentSource(
                document_id=document_id,
                document_version_id=version_id,
                display_name=display_name,
                media_type="application/octet-stream",
                extension=extension,
                content_sha256=content_hash,
                size_bytes=0,
                blob_ref=f"legacy-source:{content_hash}",
            ),
            root_node_ids=root_ids,
            parse_report=ParseReport(
                parser_id="p01-migration",
                parser_version="1",
                node_count=len(nodes),
                visible_text_nodes=visible,
                represented_visible_text_nodes=visible,
                story_counts=((StoryKind.BODY.value, len(nodes)),),
            ),
        )
        return migrated

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


class ParsedArtifact(FrozenModel):
    """Parser 返回且尚未写入持久化 Store 的内容制品。"""

    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    media_type: str = Field(min_length=1, max_length=255)
    content: bytes = Field(repr=False)
    role: Literal["source_document", "embedded_media"]

    @model_validator(mode="after")
    def _validate_content(self) -> Self:
        observed = hashlib.sha256(self.content).hexdigest()
        if observed != self.content_sha256:
            raise ValueError("ParsedArtifact 内容摘要不一致。")
        if self.artifact_id != f"sha256:{self.content_sha256}":
            raise ValueError("ParsedArtifact identity 必须绑定内容 SHA-256。")
        return self


class ParseResult(FrozenModel):
    """ParserPort 的 IR 与报告结果。"""

    document_ir: DocumentIR
    report: ParseReport
    artifacts: tuple[ParsedArtifact, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _migrate_p01_report(cls, value: object) -> object:
        """让 P01 独立 report 成为 IR 内的同一 V1 report。

        Args:
            value: Pydantic 收到的 ParseResult 输入。

        Returns:
            原始输入，或已同步 report 的 DocumentIR 输入。

        """
        if not isinstance(value, dict):
            return value
        document_ir = value.get("document_ir")
        report = value.get("report")
        if isinstance(document_ir, DocumentIR) and isinstance(
            report,
            ParseReport,
        ):
            return {
                **value,
                "document_ir": document_ir.model_copy(
                    update={"parse_report": report}
                ),
            }
        return value

    @model_validator(mode="after")
    def _validate_report(self) -> Self:
        if self.report.node_count != len(self.document_ir.nodes):
            raise ValueError("ParseResult report node_count 与 IR 不一致。")
        if self.report != self.document_ir.parse_report:
            raise ValueError("ParseResult report 必须与 DocumentIR 一致。")
        return self


def text_payload(
    exact_text: str,
    semantic_text: str | None = None,
) -> TextPayload:
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


def _p01_node_kind(node_type: str, list_level: object) -> NodeKind:
    if node_type == "heading":
        return NodeKind.HEADING
    if node_type == "paragraph":
        return (
            NodeKind.LIST_ITEM
            if isinstance(list_level, int)
            else NodeKind.PARAGRAPH
        )
    if node_type == "table":
        return NodeKind.TABLE_REPRESENTATION
    if node_type == "image":
        return NodeKind.IMAGE
    raise ValueError(f"P01 DocumentNode node_type 不受支持：{node_type}。")


def _field_value(value: object, name: str, default: object = ...) -> object:
    if isinstance(value, dict):
        if default is ...:
            return value[name]
        return value.get(name, default)
    if default is ...:
        return getattr(value, name)
    return getattr(value, name, default)


def _p01_node_text(value: object) -> str:
    if isinstance(value, DocumentNode):
        return value.text
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _display_extension(display_name: str) -> str:
    _, separator, suffix = display_name.rpartition(".")
    if (
        separator
        and suffix.isalnum()
        and len(suffix) <= _MAX_DISPLAY_EXTENSION_LENGTH
    ):
        return f".{suffix.casefold()}"
    return ".bin"


def validate_document_ir(document_ir: DocumentIR) -> None:  # noqa: PLR0912
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
        node.node_id
        for node in document_ir.nodes
        if node.parent_node_id is None
    }
    if observed_roots != root_ids:
        raise ValueError("root 必须且只能包含 parent=None 的节点。")
    ordered_roots = tuple(
        node.node_id
        for node in sorted(
            (
                node
                for node in document_ir.nodes
                if node.parent_node_id is None
            ),
            key=lambda node: (node.order, node.anchor.ordinal, node.node_id),
        )
    )
    if document_ir.root_node_ids != ordered_roots:
        raise ValueError("root_node_ids 必须匹配 root 的 order/anchor 顺序。")

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
        if node.text_payload is not None:
            start = node.anchor.source_start_char
            end = node.anchor.source_end_char
            if start is None or end is None:
                raise ValueError("文本节点 SourceAnchor 必须提供字符范围。")
            if end > len(node.text_payload.exact_text):
                raise ValueError("节点 SourceAnchor 字符范围超出 exact_text。")

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

    relationship_keys: set[tuple[str, str]] = set()
    for relationship in document_ir.relationships:
        if (
            relationship.source_node_id not in nodes_by_id
            or relationship.target_node_id not in nodes_by_id
        ):
            raise ValueError("relationship source/target 节点必须存在。")
        relationship_key = (
            relationship.source_node_id,
            relationship.relationship_id,
        )
        if relationship_key in relationship_keys:
            raise ValueError("同一 source 的 relationship ID 必须唯一。")
        relationship_keys.add(relationship_key)
    if document_ir.source.document_id != document_ir.document.document_id:
        raise ValueError("DocumentSource 与 DocumentRef 身份不一致。")
    if (
        document_ir.source.document_version_id
        != document_ir.version.document_version_id
    ):
        raise ValueError("DocumentSource 与 DocumentVersionRef 身份不一致。")
    if document_ir.source.content_sha256 != document_ir.version.content_sha256:
        raise ValueError("DocumentSource 与 DocumentVersionRef 摘要不一致。")
    if document_ir.version.document_id != document_ir.document.document_id:
        raise ValueError("DocumentVersionRef 与 DocumentRef 身份不一致。")
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
        payload["source"]["display_name"] = "<redacted>"
        payload["document"]["display_name"] = "<redacted>"
        for node in payload["nodes"]:
            text = node.get("text_payload")
            if isinstance(text, dict):
                text.pop("exact_text", None)
                text.pop("semantic_text", None)
            revision = node.get("revision_mark")
            if isinstance(revision, dict):
                revision["author"] = None
                revision["timestamp"] = None
            image = node.get("image_attributes")
            if isinstance(image, dict):
                image["display_name"] = None
                image["alt_text"] = None
            metadata = _metadata_mapping(node.get("metadata"))
            if metadata is not None:
                node["metadata"] = {
                    key: value
                    for key, value in metadata.items()
                    if key in _SAFE_NODE_METADATA_KEYS
                }
        document_metadata = _metadata_mapping(payload.get("metadata")) or {}
        payload["metadata"] = {
            key: value
            for key, value in document_metadata.items()
            if key in {"part_catalog_identity", "parsing_policy_id"}
        }
        payload["source"]["metadata"] = {}
        for relationship in payload.get("relationships", []):
            relationship["metadata"] = {}
        for issue in payload.get("parse_report", {}).get("issues", []):
            issue_metadata = _metadata_mapping(issue.get("metadata")) or {}
            issue["metadata"] = {
                key: value
                for key, value in issue_metadata.items()
                if key == "schemes"
            }
    return canonical_json(payload)


def _metadata_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        try:
            return {str(key): item for key, item in value}
        except (TypeError, ValueError):
            return None
    return None
