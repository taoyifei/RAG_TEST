"""检索、证据和回答的格式中立公共模型。"""

from __future__ import annotations

from pydantic import Field, StrictFloat, StrictInt, field_validator

from rag_app.core.models.chunk import Chunk, SourceSpan
from rag_app.core.models.common import FrozenModel, MetadataModel
from rag_app.core.models.document import KnowledgeBaseScope
from rag_app.core.models.lifecycle import IndexRevisionRef


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


class EvidenceItem(MetadataModel):
    """进入回答阶段且可回溯来源的证据。"""

    evidence_id: str = Field(min_length=1)
    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    citation_text: str = Field(min_length=1, repr=False)
    source_label: str = Field(min_length=1)
    source_spans: tuple[SourceSpan, ...] = ()
    document_id: str | None = Field(
        default=None, pattern=r"^doc_[0-9a-f]{32}$"
    )
    document_version_id: str | None = Field(
        default=None, pattern=r"^dver_[0-9a-f]{32}$"
    )
    display_name: str | None = None
    heading_path: tuple[str, ...] = ()
    section_id: str | None = None
    table_locator: str | None = None
    retrieval_origins: tuple[str, ...] = ()
    fusion_rank: StrictInt | None = Field(default=None, gt=0)
    rerank_rank: StrictInt | None = Field(default=None, gt=0)
    quality_flags: tuple[str, ...] = ()

    @property
    def support_id(self) -> str:
        """返回 P07 对外使用的 Support ID。

        Args:
            无参数；读取当前 evidence。

        Returns:
            应用分配的稳定 Support ID。

        """
        return self.evidence_id


class AnswerDraft(FrozenModel):
    """GeneratorPort 的尚未发布回答草稿。"""

    text: str = Field(min_length=1, repr=False)
    cited_evidence_ids: tuple[str, ...]


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
