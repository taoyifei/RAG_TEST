"""PaddleOCR 双路径共享的 PDF 页面、块和进度合同。"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

from pydantic import Field, StrictInt, model_validator

from rag_app.core.models.common import FrozenModel

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^sha256:[0-9a-f]{64}$"


class PdfParserMode(StrEnum):
    """PDF 文档解析的两个受支持执行位置。"""

    SELF_HOSTED = "paddle_self_hosted"
    OFFICIAL_API = "paddle_official_api"


class PdfBlockType(StrEnum):
    """供应商块映射后的有限结构类别。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    FORMULA = "formula"
    IMAGE = "image"
    CHART = "chart"
    UNSUPPORTED = "unsupported"


class PdfSourceInspection(FrozenModel):
    """发送到 Provider 前由本地 PDF 读取器证明的属性。"""

    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    page_count: StrictInt = Field(gt=0)
    native_text_layer: tuple[bool, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_pages(self) -> Self:
        if len(self.native_text_layer) != self.page_count:
            raise ValueError("PDF 文本层标志必须逐页覆盖。")
        return self


class PdfTableCell(FrozenModel):
    """从 Provider 表格内容中可证明的单元格。"""

    row_index: StrictInt = Field(ge=0)
    column_index: StrictInt = Field(ge=0)
    text: str = Field(repr=False)
    row_span: StrictInt = Field(default=1, gt=0)
    column_span: StrictInt = Field(default=1, gt=0)


class PdfTable(FrozenModel):
    """绑定页面块身份的表格结构；不跨页合并。"""

    table_id: str = Field(min_length=1, max_length=128)
    source_format: str = Field(pattern=r"^(html|markdown)$")
    cells: tuple[PdfTableCell, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_cells(self) -> Self:
        coordinates = [
            (cell.row_index, cell.column_index) for cell in self.cells
        ]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("PDF 表格单元格坐标禁止重复。")
        return self


class PdfBlock(FrozenModel):
    """按页面阅读顺序排列的一个 PaddleOCR 文档块。"""

    block_id: str = Field(min_length=1, max_length=128)
    block_type: PdfBlockType
    raw_label: str = Field(min_length=1, max_length=120)
    order: StrictInt = Field(ge=0)
    provider_order: StrictInt | None = Field(default=None, ge=0)
    text: str | None = Field(default=None, repr=False)
    markdown: str | None = Field(default=None, repr=False)
    bbox: tuple[float, float, float, float] | None = None
    table: PdfTable | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.bbox is not None:
            left, top, right, bottom = self.bbox
            if (
                not all(
                    math.isfinite(item) for item in (left, top, right, bottom)
                )
                or min(left, top) < 0
                or right <= left
                or bottom <= top
            ):
                raise ValueError("PDF block bbox 必须是非负且前进的矩形。")
        if self.table is not None and self.block_type is not PdfBlockType.TABLE:
            raise ValueError("只有 TABLE block 可以包含 table。")
        return self


class PdfPage(FrozenModel):
    """绑定原始 PDF 物理顺序的一页解析结果。"""

    page_index: StrictInt = Field(ge=0)
    width: float | None = Field(default=None, gt=0.0)
    height: float | None = Field(default=None, gt=0.0)
    blocks: tuple[PdfBlock, ...] = ()
    native_text_layer: bool = False
    page_image_artifact: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_blocks(self) -> Self:
        if (self.width is None) != (self.height is None):
            raise ValueError("PDF 页面宽高必须同时提供或同时省略。")
        if (
            self.width is not None
            and self.height is not None
            and not all(
                math.isfinite(item) for item in (self.width, self.height)
            )
        ):
            raise ValueError("PDF 页面宽高必须是有限数值。")
        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("同一 PDF 页面内 block_id 禁止重复。")
        if [block.order for block in self.blocks] != list(
            range(len(self.blocks))
        ):
            raise ValueError("PDF 页面 block order 必须从零连续。")
        for block in self.blocks:
            if block.bbox is None or self.width is None or self.height is None:
                continue
            if block.bbox[2] > self.width or block.bbox[3] > self.height:
                raise ValueError("PDF block bbox 超出页面边界。")
        return self


class PdfParseResult(FrozenModel):
    """本地和官方 Paddle Adapter 的唯一规范化输出。"""

    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_mode: PdfParserMode
    parser_model: str = Field(min_length=1, max_length=160)
    parser_revision: str = Field(min_length=1, max_length=80)
    parser_options_identity: str = Field(pattern=_IDENTITY_PATTERN)
    page_count: StrictInt = Field(gt=0)
    pages: tuple[PdfPage, ...] = Field(min_length=1)
    provider_job_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_complete_pages(self) -> Self:
        if len(self.pages) != self.page_count:
            raise ValueError("PDF 规范化结果必须覆盖全部物理页。")
        if [page.page_index for page in self.pages] != list(
            range(self.page_count)
        ):
            raise ValueError("PDF 页面索引必须按物理顺序从零连续。")
        if len(self.provider_job_ids) != len(set(self.provider_job_ids)):
            raise ValueError("Provider job_id 禁止重复。")
        return self


class PdfPageProgress(FrozenModel):
    """供持久 Job 页面展示的非敏感 PDF 解析进度。"""

    parser_mode: PdfParserMode
    parser_model: str = Field(min_length=1, max_length=160)
    total_pages: StrictInt = Field(gt=0)
    parsed_pages: StrictInt = Field(ge=0)
    failed_page_indices: tuple[StrictInt, ...] = ()
    truncated: bool = False

    @model_validator(mode="after")
    def _validate_progress(self) -> Self:
        if self.parsed_pages > self.total_pages:
            raise ValueError("PDF 已解析页数不能超过总页数。")
        if any(
            page < 0 or page >= self.total_pages
            for page in self.failed_page_indices
        ):
            raise ValueError("PDF 失败页索引超出总页数。")
        if len(self.failed_page_indices) != len(set(self.failed_page_indices)):
            raise ValueError("PDF 失败页索引禁止重复。")
        return self


__all__ = [
    "PdfBlock",
    "PdfBlockType",
    "PdfPage",
    "PdfPageProgress",
    "PdfParseResult",
    "PdfParserMode",
    "PdfSourceInspection",
    "PdfTable",
    "PdfTableCell",
]
