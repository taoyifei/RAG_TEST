"""解析、分块和索引之间共享的稳定契约。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_HEX_LENGTH = 64

__all__ = [
    "Chunk",
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
]


class ElementKind(StrEnum):
    """文档元素类型。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"


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
    paragraph_index: int | None = Field(default=None, ge=1)
    table_index: int | None = Field(default=None, ge=1)
    image_index: int | None = Field(default=None, ge=1)
    fragment: str = Field(min_length=1, max_length=240)

    def display(self) -> str:
        """生成面向引用展示的稳定位置。

        Args:
            无参数。

        Returns:
            由文件、标题路径、元素序号和片段组成的位置文本。

        """
        parts = [self.file_path, *self.heading_path]
        if self.paragraph_index is not None:
            parts.append(f"段落{self.paragraph_index}")
        if self.table_index is not None:
            parts.append(f"表格{self.table_index}")
        if self.image_index is not None:
            parts.append(f"图片{self.image_index}")
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
            self.paragraph_index,
            self.table_index,
            self.image_index,
        )
        index_text = ":".join(
            "" if item is None else str(item) for item in indexes
        )
        return "\x1f".join((*self.heading_path, index_text))


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


class Chunk(BaseModel):
    """可写入向量索引且可回溯原文的分块。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{32}$")
    doc_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pipeline_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text: str = Field(min_length=1)
    embedding_text: str = Field(min_length=1)
    element_kind: ElementKind
    locators: tuple[Locator, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
    document_status: str = "unspecified"
    authority_level: str = "unspecified"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    contains_ocr: bool = False
    minimum_ocr_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )


class PipelineSpec(BaseModel):
    """冻结影响解析、索引、检索和生成的版本输入。"""

    model_config = ConfigDict(frozen=True)

    schema_version: str
    parser_revision: str
    ocr_model: str
    ocr_revision: str
    chunker_revision: str
    chunker_parameters: tuple[tuple[str, str], ...]
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int = Field(gt=0)
    sparse_model: str
    sparse_revision: str
    index_revision: str
    reranker_model: str
    reranker_revision: str
    llm_revisions: tuple[tuple[str, str], ...] = Field(min_length=1)
    prompt_revision: str

    def fingerprint(self) -> str:
        """计算规范化 pipeline 指纹。

        Args:
            无参数。

        Returns:
            带算法前缀的 SHA256 指纹。

        """
        payload = self.model_dump(mode="json")
        payload["chunker_parameters"] = sorted(payload["chunker_parameters"])
        payload["llm_revisions"] = sorted(payload["llm_revisions"])
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


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


def stable_chunk_id(source_id: str, locator: Locator, text: str) -> str:
    """由文档、逻辑位置和内容生成稳定分块标识。

    Args:
        source_id: manifest 持久保存的来源标识。
        locator: 不依赖页码的原文位置。
        text: 分块原文。

    Returns:
        文件重命名后不变的分块标识。

    """
    payload = "\x1e".join((source_id, locator.logical_key(), text))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"chunk_{digest[:32]}"
