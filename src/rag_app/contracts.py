"""解析、分块和索引之间共享的稳定契约。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_SHA256_HEX_LENGTH = 64
_RFC3339_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_CHUNKER_PARAMETER_KEYS = frozenset(
    {"target_tokens", "hard_max_tokens", "overlap_tokens"}
)

DocumentStatus = Literal["active", "draft", "retired"]
AuthorityLevel = Literal["official", "verified", "unverified"]
DOCUMENT_STATUS_VALUES = frozenset({"active", "draft", "retired"})
AUTHORITY_LEVEL_VALUES = frozenset(
    {"official", "verified", "unverified"}
)

__all__ = [
    "AUTHORITY_LEVEL_VALUES",
    "DOCUMENT_STATUS_VALUES",
    "AuthorityLevel",
    "Chunk",
    "ChunkIdentity",
    "ChunkRole",
    "ChunkSourceSpan",
    "DocumentMetadata",
    "DocumentStatus",
    "Element",
    "ElementKind",
    "IndexManifest",
    "Locator",
    "OcrState",
    "Parser",
    "PipelineSpec",
    "SourceRecord",
    "allocate_source_id",
    "content_doc_version",
    "stable_chunk_id",
    "stable_doc_id",
    "validate_chunk_source_spans",
]


class ElementKind(StrEnum):
    """文档元素类型。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"


class ChunkRole(StrEnum):
    """分块在持久化索引中的结构角色。"""

    TEXT = "text"
    TABLE = "table"
    OCR = "ocr"


class OcrState(StrEnum):
    """图片 OCR 生命周期状态。"""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    LOW_CONFIDENCE = "low_confidence"
    FAILED = "failed"


class Locator(BaseModel):
    """描述不依赖页码的原文位置。"""

    model_config = ConfigDict(frozen=True)

    file_path: str
    heading_path: tuple[str, ...] = ()
    heading_index: int | None = Field(default=None, ge=1)
    paragraph_index: int | None = Field(default=None, ge=1)
    table_index: int | None = Field(default=None, ge=1)
    image_index: int | None = Field(default=None, ge=1)
    segment_index: int | None = Field(default=None, ge=1)
    fragment: str = Field(min_length=1, max_length=240)

    def display(self) -> str:
        """生成面向引用展示的稳定位置。

        Args:
            无参数。

        Returns:
            由文件、标题路径、元素序号和片段组成的位置文本。

        """
        parts = [self.file_path, *self.heading_path]
        if self.heading_index is not None:
            parts.append(f"标题{self.heading_index}")
        if self.paragraph_index is not None:
            parts.append(f"段落{self.paragraph_index}")
        if self.table_index is not None:
            parts.append(f"表格{self.table_index}")
        if self.image_index is not None:
            parts.append(f"图片{self.image_index}")
        if self.segment_index is not None:
            parts.append(f"片段{self.segment_index}")
        parts.append(self.fragment)
        return " > ".join(parts)

    def logical_key(self) -> str:
        """生成不含文件名的逻辑位置键。

        Args:
            无参数。

        Returns:
            重命名文件后保持不变的逻辑位置键。

        """
        indexes = (
            self.heading_index,
            self.paragraph_index,
            self.table_index,
            self.image_index,
            self.segment_index,
        )
        index_text = ":".join(
            "" if item is None else str(item) for item in indexes
        )
        return "\x1f".join((*self.heading_path, index_text))


class ChunkSourceSpan(BaseModel):
    """Chunk.text 中一段可精确映射回原始元素的字符范围。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    element_id: str = Field(min_length=1)
    locator: Locator
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    source_start_char: int = Field(ge=0)
    source_end_char: int = Field(gt=0)
    is_repeated: bool = False

    @model_validator(mode="after")
    def _validate_ranges(self) -> Self:
        """校验 chunk 与原始元素字符范围严格前进。

        Args:
            无参数。

        Returns:
            已通过字符范围校验的 source span。

        Raises:
            ValueError: 任一半开区间为空、倒置或长度不一致。

        """
        if self.end_char <= self.start_char:
            raise ValueError("source span 在 chunk 中必须是非空半开区间。")
        if self.source_end_char <= self.source_start_char:
            raise ValueError("source span 在原始元素中必须是非空半开区间。")
        if (
            self.end_char - self.start_char
            != self.source_end_char - self.source_start_char
        ):
            raise ValueError("source span 的 chunk/source 字符长度必须一致。")
        return self


def validate_chunk_source_spans(
    text: str,
    locators: tuple[Locator, ...],
    source_spans: tuple[ChunkSourceSpan, ...],
) -> None:
    """校验 chunk 文本、locator 与全部来源字符范围的一致性。

    Args:
        text: 持久化的 chunk 原文。
        locators: 按首次出现顺序去重的来源位置。
        source_spans: 按 chunk 字符位置排列的来源范围。

    Returns:
        无返回值。

    Raises:
        ValueError: span 越界、重叠或 locator 顺序不一致。

    """
    if not source_spans:
        raise ValueError("source spans 不能为空。")
    previous_end = 0
    for span in source_spans:
        if span.start_char < previous_end:
            raise ValueError("source spans 必须按位置有序且不得重叠。")
        if span.end_char > len(text):
            raise ValueError("source span 超出 Chunk.text 字符范围。")
        if not text[span.start_char : span.end_char]:
            raise ValueError("source span 对应文本不能为空。")
        previous_end = span.end_char
    expected_locators: list[Locator] = []
    for span in source_spans:
        if span.locator not in expected_locators:
            expected_locators.append(span.locator)
    if locators != tuple(expected_locators):
        raise ValueError(
            "Chunk.locators 必须等于 source spans locator 的有序去重结果。"
        )


@dataclass(frozen=True, slots=True)
class ChunkIdentity:
    """stable chunk ID 所需的全部结构身份。"""

    section_id: str
    neighbor_group_id: str
    chunk_role: ChunkRole
    source_spans: tuple[ChunkSourceSpan, ...]


class Element(BaseModel):
    """解析器输出的最小文档元素。"""

    model_config = ConfigDict(frozen=True)

    element_id: str = Field(min_length=1)
    kind: ElementKind
    text: str
    locator: Locator
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str | None = None
    media_name: str | None = None
    binary_data: bytes | None = Field(default=None, repr=False)
    list_level: int | None = Field(default=None, ge=0, le=8)
    ocr_state: OcrState = OcrState.NOT_APPLICABLE
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    ocr_error: str | None = None


def _require_rfc3339_input(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str) or _RFC3339_PATTERN.fullmatch(value) is None:
        raise ValueError("文档有效期必须是带 T 和明确时区的 RFC3339 字符串。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("文档有效期不是有效的 RFC3339 时间。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("文档有效期必须包含明确时区。")
    return value


def _require_ordered_dates(
    effective_from: datetime | None,
    effective_to: datetime | None,
) -> None:
    if (
        effective_from is not None
        and effective_to is not None
        and effective_from > effective_to
    ):
        raise ValueError("effective_from 不能晚于 effective_to。")


class DocumentMetadata(BaseModel):
    """进入索引前已解析完成的文档级元数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_status: DocumentStatus
    authority_level: AuthorityLevel
    effective_from: datetime | None
    effective_to: datetime | None

    @field_validator("effective_from", "effective_to", mode="before")
    @classmethod
    def _validate_rfc3339_input(cls, value: object) -> object:
        return _require_rfc3339_input(value)

    @model_validator(mode="after")
    def _validate_semantics(self) -> Self:
        _require_ordered_dates(self.effective_from, self.effective_to)
        return self


class Chunk(BaseModel):
    """可写入向量索引且可回溯原文的分块。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{32}$")
    doc_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pipeline_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    section_id: str = Field(pattern=r"^section_[0-9a-f]{32}$")
    neighbor_group_id: str = Field(pattern=r"^group_[0-9a-f]{32}$")
    chunk_role: ChunkRole
    source_spans: tuple[ChunkSourceSpan, ...] = Field(min_length=1)
    text: str = Field(min_length=1)
    embedding_text: str = Field(min_length=1)
    element_kind: ElementKind
    locators: tuple[Locator, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
    document_status: DocumentStatus
    authority_level: AuthorityLevel
    effective_from: datetime | None
    effective_to: datetime | None
    contains_ocr: bool = False
    minimum_ocr_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )

    @field_validator("text", "embedding_text")
    @classmethod
    def _validate_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chunk 文本禁止仅含空白。")
        return value

    @field_validator("effective_from", "effective_to", mode="before")
    @classmethod
    def _validate_rfc3339_input(cls, value: object) -> object:
        return _require_rfc3339_input(value)

    @model_validator(mode="after")
    def _validate_document_metadata(self) -> Self:
        _require_ordered_dates(self.effective_from, self.effective_to)
        validate_chunk_source_spans(
            self.text,
            self.locators,
            self.source_spans,
        )
        expected_kind = {
            ChunkRole.TEXT: ElementKind.PARAGRAPH,
            ChunkRole.TABLE: ElementKind.TABLE,
            ChunkRole.OCR: ElementKind.IMAGE,
        }[self.chunk_role]
        if self.element_kind != expected_kind:
            raise ValueError("chunk_role 与 element_kind 不一致。")
        return self


class PipelineSpec(BaseModel):
    """冻结影响解析、索引、检索和生成的版本输入。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    schema_version: str = Field(pattern=r"^2$")
    parser_revision: str = Field(min_length=1)
    ocr_model: str = Field(min_length=1)
    ocr_revision: str = Field(min_length=1)
    ocr_minimum_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    chunker_revision: str = Field(min_length=1)
    chunker_parameters: tuple[tuple[str, str], ...] = Field(
        min_length=3,
        max_length=3,
    )
    embedding_model: str = Field(min_length=1)
    embedding_revision: str = Field(min_length=1)
    embedding_dimension: int = Field(gt=0)
    embedding_tokenizer_sha256: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )
    document_embedding_instruction: str = ""
    sparse_model: str = Field(min_length=1)
    sparse_revision: str = Field(min_length=1)
    sparse_tokenizer: str = Field(default="multilingual", min_length=1)
    sparse_language: str = Field(default="none", min_length=1)
    index_revision: str = Field(min_length=1)
    corpus_policy_sha256: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )
    reranker_model: str = Field(min_length=1)
    reranker_revision: str = Field(min_length=1)
    llm_model: str = Field(default="", min_length=1)
    llm_revisions: tuple[tuple[str, str], ...] = Field(min_length=1)
    prompt_revision: str = Field(min_length=1)
    llm_tokenizer_sha256: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _validate_version_maps(self) -> Self:
        parameter_keys = tuple(key for key, _ in self.chunker_parameters)
        if (
            len(set(parameter_keys)) != len(parameter_keys)
            or set(parameter_keys) != _CHUNKER_PARAMETER_KEYS
            or any(not value for _, value in self.chunker_parameters)
        ):
            raise ValueError(
                "chunker_parameters 必须包含三个唯一冻结字段。"
            )
        llm_models = tuple(model for model, _ in self.llm_revisions)
        if (
            len(set(llm_models)) != len(llm_models)
            or any(
                not model or not revision
                for model, revision in self.llm_revisions
            )
        ):
            raise ValueError("llm_revisions 模型和 revision 必须非空且唯一。")
        return self

    def index_fingerprint(self) -> str:
        """计算只覆盖索引内容和兼容性的规范化指纹。

        Args:
            无参数。

        Returns:
            带算法前缀的 SHA256 索引指纹。

        """
        payload = {
            "schema_version": self.schema_version,
            "parser_revision": self.parser_revision,
            "metadata_schema": {
                "document_status": sorted(DOCUMENT_STATUS_VALUES),
                "authority_level": sorted(AUTHORITY_LEVEL_VALUES),
                "effective_dates": "rfc3339-tz-v1",
            },
            "ocr": {
                "model": self.ocr_model,
                "revision": self.ocr_revision,
                "minimum_confidence": self.ocr_minimum_confidence,
            },
            "chunker": {
                "revision": self.chunker_revision,
                "parameters": sorted(self.chunker_parameters),
            },
            "embedding": {
                "model": self.embedding_model,
                "revision": self.embedding_revision,
                "dimension": self.embedding_dimension,
                "tokenizer_sha256": self.embedding_tokenizer_sha256,
                "document_instruction": (
                    self.document_embedding_instruction
                ),
            },
            "sparse": {
                "model": self.sparse_model,
                "revision": self.sparse_revision,
                "tokenizer": self.sparse_tokenizer,
                "language": self.sparse_language,
            },
            "index_revision": self.index_revision,
            "corpus_policy_sha256": self.corpus_policy_sha256,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            "sha256:"
            f"{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        )

    def fingerprint(self) -> str:
        """返回兼容入口使用的索引指纹。

        Args:
            无参数。

        Returns:
            带算法前缀的 SHA256 索引指纹。

        """
        return self.index_fingerprint()


class SourceRecord(BaseModel):
    """manifest 中持久保存的文档来源身份。"""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(pattern=r"^src_[0-9a-f]{32}$")
    current_path: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    doc_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    active: bool = True


class IndexManifest(BaseModel):
    """冻结索引 pipeline 与来源身份的可审计清单。"""

    model_config = ConfigDict(frozen=True)

    manifest_version: str
    collection_name: str = Field(min_length=1)
    created_at: datetime
    pipeline: PipelineSpec
    pipeline_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sources: tuple[SourceRecord, ...]

    @model_validator(mode="after")
    def _validate_pipeline_fingerprint(self) -> IndexManifest:
        expected = self.pipeline.fingerprint()
        if self.pipeline_fingerprint != expected:
            raise ValueError(
                "pipeline_fingerprint 与 pipeline 规范化内容不一致。"
            )
        return self


class Parser(Protocol):
    """所有文档解析器必须满足的最小接口。"""

    version: str

    def parse(self, path: Path, *, display_path: str) -> list[Element]:
        """将一个受支持文件解析成有序元素。

        Args:
            path: 本地输入文件。
            display_path: 写入 Locator 的稳定展示路径。

        Returns:
            按文档顺序排列的元素。

        """
        ...


def stable_doc_id(content_sha256: str) -> str:
    """由文件内容摘要生成稳定文档标识。

    Args:
        content_sha256: 文件内容的十六进制 SHA256。

    Returns:
        文件重命名后不变的文档标识。

    """
    return f"doc_{content_sha256[:32]}"


def allocate_source_id(initial_path: str, content_sha256: str) -> str:
    """为首次进入 manifest 的来源分配稳定标识。

    Args:
        initial_path: 首次发现时的相对路径。
        content_sha256: 首次发现时的内容摘要。

    Returns:
        后续由 manifest 持久保存的来源标识。

    """
    payload = f"{initial_path}\x00{content_sha256}".encode()
    return f"src_{hashlib.sha256(payload).hexdigest()[:32]}"


def content_doc_version(content_sha256: str) -> str:
    """把内容摘要转换为显式文档版本。

    Args:
        content_sha256: 文件内容的十六进制 SHA256。

    Returns:
        带算法前缀的不可变文档版本。

    Raises:
        ValueError: 摘要不是 64 位小写十六进制。

    """
    if len(content_sha256) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in content_sha256
    ):
        raise ValueError("content_sha256 必须是 64 位小写十六进制。")
    return f"sha256:{content_sha256}"


def stable_chunk_id(
    source_id: str,
    identity: ChunkIdentity,
    text: str,
) -> str:
    """由 section、neighbor group、全部来源位置和内容生成稳定 ID。

    Args:
        source_id: manifest 持久保存的来源标识。
        identity: section、neighbor group、role 与全部有序 source spans。
        text: 分块原文。

    Returns:
        文件纯重命名后不变的分块标识。

    """
    span_payload = [
        {
            "element_id": span.element_id,
            "locator": span.locator.logical_key(),
            "start_char": span.start_char,
            "end_char": span.end_char,
            "source_start_char": span.source_start_char,
            "source_end_char": span.source_end_char,
            "is_repeated": span.is_repeated,
        }
        for span in identity.source_spans
    ]
    payload = {
        "source_id": source_id,
        "section_id": identity.section_id,
        "neighbor_group_id": identity.neighbor_group_id,
        "chunk_role": identity.chunk_role.value,
        "source_spans": span_payload,
        "text": text,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"chunk_{digest[:32]}"
