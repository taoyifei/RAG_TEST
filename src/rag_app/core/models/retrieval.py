"""检索、证据和回答的格式中立公共模型。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from rag_app.core.models.chunk import Chunk, SourceSpan, SourceSpanKind
from rag_app.core.models.common import FrozenModel, MetadataModel
from rag_app.core.models.document import KnowledgeBaseScope
from rag_app.core.models.generation_packet import PreparedGenerationPacket
from rag_app.core.models.lifecycle import IndexRevisionRef
from rag_app.core.models.provider import ProviderCall


class SearchQuery(MetadataModel):
    """一个受知识库边界约束的搜索请求。"""

    scope: KnowledgeBaseScope
    text: str = Field(min_length=1, repr=False)
    limit: StrictInt = Field(default=10, gt=0, le=200)

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("search query 禁止仅含空白。")
        return value


class SearchHit(MetadataModel):
    """不暴露 Qdrant ScoredPoint 的检索命中。"""

    chunk: Chunk
    score: StrictFloat
    rank: StrictInt = Field(gt=0)
    channels: tuple[str, ...]


class OcrVerificationState(StrEnum):
    """最终答案中高风险视觉文字的复核状态。"""

    VERIFIED = "VERIFIED"
    CONFLICT = "CONFLICT"
    UNVERIFIED = "UNVERIFIED"


class EvidenceItem(MetadataModel):
    """进入回答阶段且可回溯来源的证据。"""

    evidence_id: str = Field(min_length=1)
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    citation_text: str = Field(min_length=1, repr=False)
    source_label: str = Field(min_length=1)
    source_spans: tuple[SourceSpan, ...] = ()
    document_id: str | None = Field(default=None, pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str | None = Field(
        default=None, pattern=r"^dver_[0-9a-f]{32}$"
    )
    display_name: str | None = None
    heading_path: tuple[str, ...] = ()
    section_id: str | None = None
    table_locator: str | None = None
    table_context: bool = False
    page_index: StrictInt | None = Field(default=None, ge=0)
    source_kind: SourceSpanKind | None = None
    pdf_block_id: str | None = Field(default=None, min_length=1, max_length=128)
    pdf_table_id: str | None = Field(default=None, min_length=1, max_length=128)
    ocr_verification_state: OcrVerificationState | None = None
    selection_reason: str = Field(default="retrieval_candidate", min_length=1)
    publishable: bool = True
    retrieval_origins: tuple[str, ...] = ()
    fusion_rank: StrictInt | None = Field(default=None, gt=0)
    rerank_rank: StrictInt | None = Field(default=None, gt=0)
    quality_flags: tuple[str, ...] = ()
    source_identity_scope: tuple[str, ...] = Field(default=(), exclude=True)

    @property
    def support_id(self) -> str:
        """返回 P07 对外使用的 Support ID。

        Args:
            无参数；读取当前 evidence。

        Returns:
            应用分配的稳定 Support ID。

        """
        return self.evidence_id


class OcrClaimVerification(FrozenModel):
    """一次有界 PP-OCRv6 复核的脱敏结果。"""

    state: OcrVerificationState
    support_states: tuple[tuple[str, OcrVerificationState], ...]
    provider_calls: tuple[ProviderCall, ...] = ()
    reason_code: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def _validate_supports(self) -> Self:
        support_ids = [item[0] for item in self.support_states]
        if not support_ids or any(not item for item in support_ids):
            raise ValueError("OCR 复核必须绑定至少一个 Support ID。")
        if len(support_ids) != len(set(support_ids)):
            raise ValueError("OCR 复核 Support ID 禁止重复。")
        if any(state is not self.state for _, state in self.support_states):
            raise ValueError("本次 OCR 复核的 Support 状态必须一致。")
        return self


class ClaimSupport(FrozenModel):
    """单条事实引用的服务端证据 ID 与逐字支持片段。"""

    support_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=6000, repr=False)


class AnswerClaim(FrozenModel):
    """供应用层进行对象、数值、否定和来源支持校验的事实。"""

    text: str = Field(min_length=1, max_length=6000, repr=False)
    supports: tuple[ClaimSupport, ...] = Field(min_length=1, max_length=128)


class NaturalClaim(FrozenModel):
    """模型给出自然事实，并逐字摘取每个所选证据的最小引文。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    text: str = Field(min_length=1, max_length=6000, repr=False)
    supports: tuple[ClaimSupport, ...] = Field(min_length=1, max_length=128)


class GroundedWireClaim(FrozenModel):
    """模型线只表达事实、所属 Atom 与阅读单元短引用。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    text: str = Field(min_length=1, max_length=6000, repr=False)
    refs: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _reject_duplicate_refs(self) -> Self:
        if len(self.refs) != len(set(self.refs)):
            raise ValueError("WIRE_DUPLICATE_REF")
        return self


class GroundedWireDiagnostic(FrozenModel):
    """不含模型正文或输入值的逐项 Wire 失败诊断。"""

    item_index: StrictInt = Field(ge=0)
    atom_id: str | None = Field(default=None, pattern=r"^A[1-4]$")
    failure_stage: Literal["wire_schema", "evidence_binding"]
    failure_code: str = Field(min_length=1, max_length=120)
    json_path: str = Field(min_length=1, max_length=200)
    expected_type: str | None = Field(default=None, max_length=80)
    observed_type: str | None = Field(default=None, max_length=80)


class TableFactSelection(FrozenModel):
    """模型只选择服务端登记的物理表格事实，不自行拼装表头和值。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    fact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PhysicalTableHeader(FrozenModel):
    """一个由解析器认证的表头单元格及其实际覆盖列。"""

    row_index: StrictInt = Field(ge=0)
    column_indexes: tuple[StrictInt, ...] = Field(min_length=1)
    support_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        if (
            len(self.column_indexes) != len(set(self.column_indexes))
            or any(column < 0 for column in self.column_indexes)
            or len(self.support_ids) != len(set(self.support_ids))
        ):
            raise ValueError("表头列和来源 ID 必须是非负且无重复的。")
        return self


class PhysicalTableFact(FrozenModel):
    """与问句无关的真实表格坐标、值片段及结构依赖。"""

    fact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    table_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    table_node_id: str = Field(pattern=r"^node_[0-9a-f]{32}$")
    row_index: StrictInt = Field(ge=0)
    row_label_column_index: StrictInt = Field(ge=0)
    value_column_index: StrictInt = Field(ge=0)
    row_label_support_ids: tuple[str, ...] = Field(min_length=1)
    value_support_ids: tuple[str, ...] = Field(min_length=1)
    headers: tuple[PhysicalTableHeader, ...] = Field(min_length=1)

    @property
    def header_support_ids(self) -> tuple[str, ...]:
        """返回按规范表头层次排列的实际来源别名。"""
        return tuple(
            support_id
            for header in self.headers
            for support_id in header.support_ids
        )

    @property
    def all_support_ids(self) -> tuple[str, ...]:
        """返回一个事实不可拆分的行名、表头和值来源。"""
        return tuple(
            dict.fromkeys(
                (
                    *self.value_support_ids,
                    *self.row_label_support_ids,
                    *self.header_support_ids,
                )
            )
        )

    @model_validator(mode="after")
    def _validate_dependencies(self) -> Self:
        groups = (
            self.row_label_support_ids,
            self.value_support_ids,
            self.header_support_ids,
        )
        if self.row_label_column_index == self.value_column_index:
            raise ValueError("表格事实的行名列和值列不能相同。")
        if any(len(values) != len(set(values)) for values in groups):
            raise ValueError("表格事实的来源 ID 不允许重复。")
        if set(self.row_label_support_ids) & set(self.value_support_ids):
            raise ValueError("行名来源和值来源不能复用。")
        if not any(
            self.value_column_index in header.column_indexes
            for header in self.headers
        ):
            raise ValueError("表格事实缺少覆盖值列的规范表头。")
        return self


class AtomFactBinding(FrozenModel):
    """当前 Atom 到物理事实的语义候选，不改变任何来源坐标。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    fact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    relation_status: Literal["SUPPORTED", "UNDETERMINED"] = "UNDETERMINED"
    requested_target: str = Field(min_length=1, max_length=500)
    requested_relation: str = Field(min_length=1, max_length=500)
    requested_stage_labels: tuple[str, ...] = ()
    requested_conditions: tuple[str, ...] = ()


class GeneratedAtomCoverage(FrozenModel):
    """模型报告的覆盖状态；最终状态由应用根据已校验事实决定。"""

    atom_id: str = Field(min_length=1)
    status: str = Field(pattern=r"^(SUPPORTED|PARTIAL|MISSING|CONTRADICTORY)$")


class AnswerDraft(FrozenModel):
    """GeneratorPort 的尚未发布回答草稿。"""

    text: str = Field(min_length=1, repr=False)
    cited_evidence_ids: tuple[str, ...]
    claims: tuple[AnswerClaim, ...] = Field(default=(), max_length=24)
    natural_claims: tuple[NaturalClaim, ...] = Field(default=(), max_length=24)
    wire_claims: tuple[GroundedWireClaim, ...] = Field(
        default=(), max_length=24, exclude=True, repr=False
    )
    wire_diagnostics: tuple[GroundedWireDiagnostic, ...] = Field(
        default=(), max_length=24, exclude=True, repr=False
    )
    atom_coverage: tuple[GeneratedAtomCoverage, ...] = ()
    missing_atoms: tuple[str, ...] = ()
    provider_calls: tuple[ProviderCall, ...] = ()
    generation_mode: str = Field(
        default="extractive", pattern=r"^(extractive|llm|natural)$"
    )
    reason_code: str | None = None
    prepared_packet: PreparedGenerationPacket | None = Field(
        default=None, exclude=True, repr=False
    )
    previous_prepared_packets: tuple[PreparedGenerationPacket, ...] = Field(
        default=(), exclude=True, repr=False
    )


class AnswerResult(FrozenModel):
    """宿主程序可消费的最小回答外壳。"""

    answer: str = Field(min_length=1, repr=False)
    evidence: tuple[EvidenceItem, ...]
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    reason_code: str = Field(min_length=1)


class VectorWriteRequest(FrozenModel):
    """显式绑定 slot 和 named vector 的向量写入请求。"""

    revision: IndexRevisionRef
    slot_id: str
    vector_name: str
    chunks: tuple[Chunk, ...]
    vectors: tuple[tuple[StrictFloat, ...], ...] = Field(repr=False)


class VectorSearchRequest(FrozenModel):
    """禁止 Store 猜默认向量空间的 Dense 查询。"""

    revision: IndexRevisionRef
    slot_id: str
    vector_name: str
    query_vector: tuple[StrictFloat, ...] = Field(min_length=1, repr=False)
    limit: StrictInt = Field(gt=0)


class LexicalSearchRequest(FrozenModel):
    """词法 Store 的格式中立查询。"""

    revision: IndexRevisionRef
    query: str = Field(min_length=1, repr=False)
    limit: StrictInt = Field(gt=0)
