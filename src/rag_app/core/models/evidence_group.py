"""结构化证据组的格式中立身份和原文映射。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, StrictInt, model_validator

from rag_app.core.models.chunk import SourceSpan
from rag_app.core.models.common import FrozenModel


class EvidenceGroupKind(StrEnum):
    """允许进入同一通用检索链的结构类型。"""

    PARAGRAPH_GROUP = "PARAGRAPH_GROUP"
    SECTION_GROUP = "SECTION_GROUP"
    LIST_GROUP = "LIST_GROUP"
    TABLE_ROW_GROUP = "TABLE_ROW_GROUP"
    PROCEDURE_GROUP = "PROCEDURE_GROUP"
    CATALOG_ENTRY = "CATALOG_ENTRY"


class GroupSourceMap(FrozenModel):
    """一个成员 Chunk 的原文和完整来源跨度。"""

    chunk_id: str = Field(pattern=r"^chunk_[0-9a-f]{32}$")
    citation_text: str = Field(min_length=1, repr=False)
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    structural_coordinates: tuple[str, ...] = ()


class EvidenceGroup(FrozenModel):
    """一组可原子排序、打包且不改变引用正文的结构证据。"""

    group_id: str = Field(pattern=r"^egrp_[0-9a-f]{32}$")
    kind: EvidenceGroupKind
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    section_id: str = Field(min_length=1)
    heading_path: tuple[str, ...] = ()
    member_chunk_ids: tuple[str, ...] = ()
    member_source_maps: tuple[GroupSourceMap, ...] = ()
    structural_coordinates: tuple[str, ...] = ()
    complete: bool
    incomplete_reasons: tuple[str, ...] = ()
    token_cost: StrictInt = Field(ge=0)
    catalog_title: str | None = None
    category_path: tuple[str, ...] = ()
    reference_object: str | None = None

    @model_validator(mode="after")
    def _validate_members(self) -> Self:
        """保持成员身份、映射与完整性标记一致。"""
        mapped = tuple(item.chunk_id for item in self.member_source_maps)
        if self.kind is EvidenceGroupKind.CATALOG_ENTRY:
            if self.member_chunk_ids or self.member_source_maps:
                raise ValueError("Catalog 组不能冒充已入库正文。")
            if not self.catalog_title:
                raise ValueError("Catalog 组必须保留标题。")
        elif not self.member_chunk_ids or mapped != self.member_chunk_ids:
            raise ValueError("证据组成员必须与原文映射一一对应。")
        if len(self.member_chunk_ids) != len(set(self.member_chunk_ids)):
            raise ValueError("证据组成员不能重复。")
        if self.complete and self.incomplete_reasons:
            raise ValueError("完整证据组不能携带缺失原因。")
        if not self.complete and not self.incomplete_reasons:
            raise ValueError("不完整证据组必须解释缺失原因。")
        return self
