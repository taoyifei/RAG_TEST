"""格式中立的来源跨度、Chunk 和分块报告。"""

from __future__ import annotations

from typing import Self

from pydantic import Field, StrictInt, model_validator

from rag_app.core.models.common import FrozenModel, MetadataModel
from rag_app.core.models.document import DocumentVersionRef


class SourceSpan(MetadataModel):
    """Chunk 文本到一个格式中立文档节点的映射。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    node_id: str = Field(pattern=r"^node_[0-9a-f]{32}$")
    structural_path: tuple[str, ...]
    chunk_start_char: StrictInt = Field(ge=0)
    chunk_end_char: StrictInt = Field(gt=0)
    source_start_char: StrictInt = Field(ge=0)
    source_end_char: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _validate_ranges(self) -> Self:
        if self.chunk_end_char <= self.chunk_start_char:
            raise ValueError("chunk 字符范围必须非空且前进。")
        if self.source_end_char <= self.source_start_char:
            raise ValueError("source 字符范围必须非空且前进。")
        return self


class Chunk(MetadataModel):
    """不依赖向量存储实现的可检索分块。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    version: DocumentVersionRef
    chunker_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    citation_text: str = Field(min_length=1, repr=False)
    embedding_text: str = Field(min_length=1, repr=False)


class ChunkingContext(MetadataModel):
    """ChunkerPort 使用的冻结上下文。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    chunker_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ChunkingReport(FrozenModel):
    """分块计数与非敏感 warning。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    chunk_count: StrictInt = Field(ge=0)
    warnings: tuple[str, ...] = ()


class ChunkingResult(FrozenModel):
    """ChunkerPort 输出的有序 chunks 与报告。"""

    chunks: tuple[Chunk, ...]
    report: ChunkingReport
