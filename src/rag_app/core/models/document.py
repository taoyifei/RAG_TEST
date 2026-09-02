"""格式中立的文档身份与 Document IR 骨架。"""

from __future__ import annotations

from pydantic import Field, StrictInt, field_validator

from rag_app.core.models.common import FrozenModel, MetadataModel


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
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentNode(MetadataModel):
    """不假设 DOCX 字段的最小文档节点。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    node_id: str = Field(pattern=r"^node_[0-9a-f]{32}$")
    node_type: str = Field(min_length=1, max_length=80)
    structural_path: tuple[str, ...]
    text: str = Field(default="", repr=False)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentIR(MetadataModel):
    """ParserPort 输出的格式中立 V1 文档骨架。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    document: DocumentRef
    version: DocumentVersionRef
    nodes: tuple[DocumentNode, ...]


class ParseReport(FrozenModel):
    """解析计数与非敏感 warning。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    node_count: StrictInt = Field(ge=0)
    warnings: tuple[str, ...] = ()


class ParseSource(FrozenModel):
    """ParserPort 的受控字节输入及格式元数据。"""

    media_type: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=512)
    content: bytes = Field(repr=False)


class ParsePolicy(MetadataModel):
    """格式中立、冻结的解析策略。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    policy_id: str = Field(min_length=1)


class ParseResult(FrozenModel):
    """ParserPort 的 IR 与报告结果。"""

    document_ir: DocumentIR
    report: ParseReport

    @field_validator("report")
    @classmethod
    def _validate_count(cls, value: ParseReport) -> ParseReport:
        return value
