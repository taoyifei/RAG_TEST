"""格式中立的来源跨度、Chunk V3 和分块报告。"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self

from pydantic import Field, StrictInt, field_validator, model_validator

from rag_app.core.models.common import FrozenModel, MetadataModel
from rag_app.core.models.document import DocumentVersionRef, SourceAnchor

_ZERO_ID = "0" * 32


class SourceSpanKind(StrEnum):
    """citation 字符的来源语义。"""

    ORIGINAL_TEXT = "original_text"
    DERIVED_NUMBERING = "derived_numbering"
    REPEATED_CONTEXT = "repeated_context"
    SEPARATOR = "separator"


class ChunkRole(StrEnum):
    """结构化 Chunk 的检索角色。"""

    TEXT = "text"
    LIST = "list"
    TABLE = "table"
    NOTE = "note"
    IMAGE_METADATA = "image_metadata"
    HEADER_FOOTER = "header_footer"
    TEXT_BOX = "text_box"
    COMMENT = "comment"


class TokenCountResult(FrozenModel):
    """一次 token 计数的可审计结果。"""

    count: StrictInt = Field(ge=0)
    tokenizer_id: str = Field(min_length=1, max_length=256)
    exact: bool
    model_compatibility: tuple[str, ...] = ()


class SourceSpan(MetadataModel):
    """citation 文本到一个格式中立来源或受控分隔符的映射。"""

    schema_version: str = Field(default="3", pattern=r"^3$")
    span_type: SourceSpanKind = SourceSpanKind.ORIGINAL_TEXT
    node_id: str | None = Field(
        default=None,
        pattern=r"^node_[0-9a-f]{32}$",
    )
    source_anchor: SourceAnchor | None = None
    structural_path: tuple[str, ...] = ()
    chunk_start_char: StrictInt = Field(ge=0)
    chunk_end_char: StrictInt = Field(gt=0)
    source_start_char: StrictInt | None = Field(default=None, ge=0)
    source_end_char: StrictInt | None = Field(default=None, ge=0)
    is_repeated: bool = False
    is_citable: bool = True

    @model_validator(mode="after")
    def _validate_ranges(self) -> Self:
        """校验来源类型、字符范围和引用语义。

        Args:
            无参数；读取当前 span。

        Returns:
            已通过校验的来源跨度。

        Raises:
            ValueError: 范围倒置，或来源类型与字段组合不一致。

        """
        if self.chunk_end_char <= self.chunk_start_char:
            raise ValueError("chunk 字符范围必须非空且前进。")
        if self.span_type is SourceSpanKind.SEPARATOR:
            _validate_separator_span(self)
            return self
        if self.node_id is None or self.source_anchor is None:
            raise ValueError("非 separator span 必须指向节点和 SourceAnchor。")
        if self.structural_path != self.source_anchor.structural_path:
            raise ValueError("span structural_path 必须匹配 SourceAnchor。")
        if self.span_type is SourceSpanKind.DERIVED_NUMBERING:
            _validate_derived_span(self)
            return self
        _validate_mapped_span(self)
        return self


def _validate_separator_span(span: SourceSpan) -> None:
    if any(
        value is not None
        for value in (
            span.node_id,
            span.source_anchor,
            span.source_start_char,
            span.source_end_char,
        )
    ):
        raise ValueError("separator span 禁止伪造原文来源。")
    if span.is_citable or span.is_repeated:
        raise ValueError("separator span 不可引用或标记重复。")


def _validate_derived_span(span: SourceSpan) -> None:
    if span.source_start_char is not None or span.source_end_char is not None:
        raise ValueError("派生编号不具有伪造的原文字符范围。")
    if span.is_repeated or not span.is_citable:
        raise ValueError("派生编号必须可引用且不能标记为重复。")


def _validate_mapped_span(span: SourceSpan) -> None:
    if span.source_start_char is None or span.source_end_char is None:
        raise ValueError("原文和重复上下文必须提供 source 字符范围。")
    if span.source_end_char <= span.source_start_char:
        raise ValueError("source 字符范围必须非空且前进。")
    if (
        span.chunk_end_char - span.chunk_start_char
        != span.source_end_char - span.source_start_char
    ):
        raise ValueError("原文映射的 chunk/source 字符长度必须一致。")
    expected_repeated = span.span_type is SourceSpanKind.REPEATED_CONTEXT
    if span.is_repeated != expected_repeated:
        raise ValueError("重复标志必须与 REPEATED_CONTEXT 类型一致。")


class ChunkingPolicy(FrozenModel):
    """`docx-structural-v3` 的冻结 provisional 参数。"""

    schema_version: str = Field(default="3", pattern=r"^3$")
    chunker_id: str = Field(
        default="docx-structural-v3",
        pattern=r"^docx-structural-v3$",
    )
    target_tokens: StrictInt = Field(default=384, gt=0)
    hard_max_tokens: StrictInt = Field(default=512, gt=0)
    overlap_cap_tokens: StrictInt = Field(default=64, ge=0)
    min_tail_tokens: StrictInt = Field(default=64, ge=0)
    include_document_title: bool = True
    include_heading_path: bool = True
    include_list_path: bool = True
    include_table_header: bool = True
    header_footer_policy: str = Field(
        default="metadata_only",
        pattern=r"^(metadata_only|separate_chunks)$",
    )
    notes_policy: str = Field(
        default="separate_child",
        pattern=r"^separate_child$",
    )
    image_policy: str = Field(
        default="metadata_only",
        pattern=r"^metadata_only$",
    )
    comments_policy: str = Field(
        default="metadata_only",
        pattern=r"^(metadata_only|separate_chunks)$",
    )
    contextual_prefix: str = Field(
        default="deterministic",
        pattern=r"^deterministic$",
    )
    estimated_token_safety_margin: float = Field(
        default=0.15,
        ge=0.0,
        lt=1.0,
    )
    provisional: bool = True
    required_embedding_slots: tuple[str, ...] = ("primary", "standby")
    max_embedding_tokens_by_slot: tuple[tuple[str, StrictInt], ...] = (
        ("primary", 32768),
        ("standby", 131072),
    )
    profile_hard_cap: StrictInt = Field(default=512, gt=0)

    @field_validator("required_embedding_slots")
    @classmethod
    def _validate_slots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not value
            or len(value) != len(set(value))
            or any(not item for item in value)
        ):
            raise ValueError("required embedding slots 必须非空且唯一。")
        return value

    @model_validator(mode="after")
    def _validate_limits(self) -> Self:
        """校验 provisional packing 与 Provider 上限合同。

        Args:
            无参数；读取当前策略。

        Returns:
            已通过边界校验的策略。

        Raises:
            ValueError: token 参数或 required slot 上限不一致。

        """
        if self.hard_max_tokens < self.target_tokens:
            raise ValueError("hard_max_tokens 不能小于 target_tokens。")
        if self.overlap_cap_tokens >= self.target_tokens:
            raise ValueError("overlap cap 必须小于 target。")
        if self.min_tail_tokens > self.target_tokens:
            raise ValueError("min tail 不能大于 target。")
        limits = dict(self.max_embedding_tokens_by_slot)
        if len(limits) != len(self.max_embedding_tokens_by_slot):
            raise ValueError("Provider token slot 禁止重复。")
        if set(limits) != set(self.required_embedding_slots):
            raise ValueError("Provider token 上限必须覆盖全部 required slots。")
        return self

    @property
    def effective_embedding_max(self) -> int:
        """返回所有 required slot 和 Profile 的最严格上限。

        Args:
            无参数；读取当前策略。

        Returns:
            可进入 embedding 阶段的最大 token 数。

        """
        return min(
            self.profile_hard_cap,
            *(limit for _, limit in self.max_embedding_tokens_by_slot),
        )


class Chunk(MetadataModel):
    """不依赖向量存储实现的 canonical Chunk V3。"""

    schema_version: str = Field(default="3", pattern=r"^3$")
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    project_id: str = Field(
        default=f"prj_{_ZERO_ID}",
        pattern=r"^prj_[0-9a-f]{32}$",
    )
    knowledge_base_id: str = Field(
        default=f"kb_{_ZERO_ID}",
        pattern=r"^kb_[0-9a-f]{32}$",
    )
    index_revision_id: str = Field(
        default=f"irev_{_ZERO_ID}",
        pattern=r"^irev_[0-9a-f]{32}$",
    )
    version: DocumentVersionRef
    chunker_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    role: ChunkRole = ChunkRole.TEXT
    parent_node_id: str | None = Field(
        default=None,
        pattern=r"^node_[0-9a-f]{32}$",
    )
    section_id: str = Field(default="root", min_length=1, max_length=160)
    neighbor_group_id: str = Field(
        default="root",
        min_length=1,
        max_length=160,
    )
    previous_chunk_id: str | None = Field(
        default=None,
        pattern=r"^chunk_[0-9a-f]{32}$",
    )
    next_chunk_id: str | None = Field(
        default=None,
        pattern=r"^chunk_[0-9a-f]{32}$",
    )
    child_group_ids: tuple[str, ...] = ()
    note_refs: tuple[str, ...] = ()
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    citation_text: str = Field(min_length=1, repr=False)
    embedding_text: str = Field(min_length=1, repr=False)
    lexical_text: str = Field(min_length=1, repr=False)
    heading_path: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    token_count: StrictInt = Field(gt=0)
    token_count_is_estimate: bool
    tokenizer_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def text(self) -> str:
        """返回旧只读属性对应的 canonical citation 文本。

        Args:
            无参数；读取当前 Chunk。

        Returns:
            `citation_text`，不维护第二份可变正文。

        """
        return self.citation_text

    @model_validator(mode="after")
    def _validate_text_and_spans(self) -> Self:
        """校验三视图、内容摘要和 citation 全覆盖。

        Args:
            无参数；读取当前 Chunk。

        Returns:
            已通过完整来源覆盖校验的 Chunk。

        Raises:
            ValueError: 文本、摘要或 span 覆盖不符合 V3 合同。

        """
        if not all(
            value.strip()
            for value in (
                self.citation_text,
                self.embedding_text,
                self.lexical_text,
            )
        ):
            raise ValueError("Chunk 三种文本视图禁止仅含空白。")
        digest = hashlib.sha256(self.citation_text.encode("utf-8")).hexdigest()
        if digest != self.content_sha256:
            raise ValueError("Chunk content_sha256 必须匹配 citation_text。")
        cursor = 0
        for span in self.source_spans:
            if span.chunk_start_char != cursor:
                raise ValueError("source spans 必须无间隙覆盖 citation_text。")
            if span.chunk_end_char > len(self.citation_text):
                raise ValueError("source span 超出 citation_text。")
            cursor = span.chunk_end_char
        if cursor != len(self.citation_text):
            raise ValueError("source spans 必须覆盖 citation_text 全部字符。")
        if len(self.child_group_ids) != len(set(self.child_group_ids)):
            raise ValueError("child group refs 禁止重复。")
        if len(self.note_refs) != len(set(self.note_refs)):
            raise ValueError("note refs 禁止重复。")
        return self


class ChunkingContext(MetadataModel):
    """ChunkerPort 使用的冻结上下文。"""

    schema_version: str = Field(default="3", pattern=r"^3$")
    chunker_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    index_revision_id: str = Field(
        default=f"irev_{_ZERO_ID}",
        pattern=r"^irev_[0-9a-f]{32}$",
    )


class ChunkingReport(FrozenModel):
    """结构覆盖、token、安全边界和来源映射聚合。"""

    schema_version: str = Field(default="3", pattern=r"^3$")
    node_count: StrictInt = Field(default=0, ge=0)
    represented_node_count: StrictInt = Field(default=0, ge=0)
    chunk_count: StrictInt = Field(ge=0)
    chunk_count_by_role: tuple[tuple[str, StrictInt], ...] = ()
    token_p50: StrictInt = Field(default=0, ge=0)
    token_p95: StrictInt = Field(default=0, ge=0)
    token_max: StrictInt = Field(default=0, ge=0)
    exact_token_count: StrictInt = Field(default=0, ge=0)
    estimated_token_count: StrictInt = Field(default=0, ge=0)
    oversize_violations: StrictInt = Field(default=0, ge=0)
    cross_boundary_violations: StrictInt = Field(default=0, ge=0)
    cross_section_violations: StrictInt = Field(default=0, ge=0)
    cross_group_violations: StrictInt = Field(default=0, ge=0)
    total_citable_source_chars: StrictInt = Field(default=0, ge=0)
    unique_covered_source_chars: StrictInt = Field(default=0, ge=0)
    missing_source_chars: StrictInt = Field(default=0, ge=0)
    source_span_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    duplicated_citable_chars: StrictInt = Field(default=0, ge=0)
    repeated_context_chars: StrictInt = Field(default=0, ge=0)
    table_row_count: StrictInt = Field(default=0, ge=0)
    represented_table_row_count: StrictInt = Field(default=0, ge=0)
    table_cell_count: StrictInt = Field(default=0, ge=0)
    represented_table_cell_count: StrictInt = Field(default=0, ge=0)
    list_label_count: StrictInt = Field(default=0, ge=0)
    represented_list_label_count: StrictInt = Field(default=0, ge=0)
    orphan_note_count: StrictInt = Field(default=0, ge=0)
    orphan_image_count: StrictInt = Field(default=0, ge=0)
    orphan_relation_count: StrictInt = Field(default=0, ge=0)
    missing_child_group_count: StrictInt = Field(default=0, ge=0)
    missing_note_ref_count: StrictInt = Field(default=0, ge=0)
    stable_id_duplicate_count: StrictInt = Field(default=0, ge=0)
    required_embedding_slots: tuple[str, ...] = ()
    max_embedding_tokens_by_slot: tuple[tuple[str, StrictInt], ...] = ()
    chunks_over_limit_by_slot: tuple[tuple[str, StrictInt], ...] = ()
    warnings: tuple[str, ...] = ()
    elapsed_seconds: float = Field(default=0.0, ge=0.0, exclude=True)

    @model_validator(mode="after")
    def _validate_metrics(self) -> Self:
        if self.represented_node_count > self.node_count:
            raise ValueError("represented node 数不能超过 node 总数。")
        if self.unique_covered_source_chars > self.total_citable_source_chars:
            raise ValueError("unique covered chars 不能超过来源总字符数。")
        if self.missing_source_chars != (
            self.total_citable_source_chars - self.unique_covered_source_chars
        ):
            raise ValueError("missing source chars 与覆盖字符数不一致。")
        expected_coverage = (
            1.0
            if self.total_citable_source_chars == 0
            else self.unique_covered_source_chars
            / self.total_citable_source_chars
        )
        if abs(self.source_span_coverage - expected_coverage) > 1e-12:
            raise ValueError("source span coverage 与字符计数不一致。")
        if self.cross_boundary_violations != (
            self.cross_section_violations + self.cross_group_violations
        ):
            raise ValueError("cross boundary 汇总与分项不一致。")
        if self.represented_table_row_count > self.table_row_count:
            raise ValueError("represented table row 数不能超过总数。")
        if self.represented_table_cell_count > self.table_cell_count:
            raise ValueError("represented table cell 数不能超过总数。")
        if self.represented_list_label_count > self.list_label_count:
            raise ValueError("represented list label 数不能超过总数。")
        return self


class ChunkingResult(FrozenModel):
    """ChunkerPort 输出的有序 chunks 与报告。"""

    chunks: tuple[Chunk, ...]
    report: ChunkingReport
