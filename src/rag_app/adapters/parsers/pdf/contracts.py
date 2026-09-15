"""把 Paddle 两条 API 的页面结果收敛为唯一内部合同。"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import TypeGuard, cast

from rag_app.core.errors import ProviderInvalidResponse
from rag_app.core.models import (
    PdfBlock,
    PdfBlockType,
    PdfPage,
    PdfSourceInspection,
    PdfTable,
    PdfTableCell,
)

_HEADING_LABELS = frozenset(
    {"doc_title", "document_title", "paragraph_title", "section_title", "title"}
)
_PARAGRAPH_LABELS = frozenset(
    {
        "abstract",
        "algorithm",
        "aside_text",
        "content",
        "figure_title",
        "footnote",
        "paragraph",
        "reference",
        "reference_content",
        "table_caption",
        "text",
        "vision_footnote",
    }
)
_LIST_LABELS = frozenset({"list", "list_item", "numbered_list"})
_TABLE_LABELS = frozenset({"table", "table_body"})
_FORMULA_LABELS = frozenset(
    {"display_formula", "formula", "formula_number", "inline_formula"}
)
_IMAGE_LABELS = frozenset({"figure", "image", "picture"})
_CHART_LABELS = frozenset({"chart", "plot"})
_MARKDOWN_SEPARATOR = re.compile(r"^\s*:?-{3,}:?\s*$")
_MAX_PROVIDER_ID_LENGTH = 200
_MAX_BLOCK_ID_LENGTH = 128
_BBOX_COORDINATE_COUNT = 4
_BLOCK_TYPES_BY_LABEL = {
    **dict.fromkeys(_HEADING_LABELS, PdfBlockType.HEADING),
    **dict.fromkeys(_PARAGRAPH_LABELS, PdfBlockType.PARAGRAPH),
    **dict.fromkeys(_LIST_LABELS, PdfBlockType.LIST_ITEM),
    **dict.fromkeys(_TABLE_LABELS, PdfBlockType.TABLE),
    **dict.fromkeys(_FORMULA_LABELS, PdfBlockType.FORMULA),
    **dict.fromkeys(_IMAGE_LABELS, PdfBlockType.IMAGE),
    **dict.fromkeys(_CHART_LABELS, PdfBlockType.CHART),
}


def self_hosted_pages(payload: object) -> tuple[object, ...]:
    """从 `/layout-parsing` 响应中读取实际页面数组。

    Args:
        payload: 已完成 JSON 解码的 Provider 响应。

    Returns:
        保持供应商数组顺序的页面对象。

    Raises:
        ProviderInvalidResponse: 响应形状或业务错误码无效。

    """
    root = _mapping(payload, "PDF Provider 响应必须是 JSON object。")
    error_code = root.get("errorCode", root.get("error_code", 0))
    if error_code not in (None, 0, "0"):
        raise ProviderInvalidResponse(
            "PaddleOCR 文档解析返回业务错误。",
            stage="pdf.paddle.self_hosted.response",
            code="PADDLE_DOCUMENT_PARSE_FAILED",
            details={"provider_code": _safe_scalar(error_code)},
        )
    result = _mapping(root.get("result"), "PaddleOCR 响应缺少 result。")
    pages = result.get("layoutParsingResults")
    if not _is_sequence(pages):
        raise ProviderInvalidResponse(
            "PaddleOCR 响应缺少页面结果数组。",
            stage="pdf.paddle.self_hosted.response",
            code="PDF_PAGE_RESULTS_MISSING",
        )
    return tuple(pages)


def official_pages(result: object) -> tuple[object, ...]:
    """读取官方 API `DocParsingResult.pages`。

    Args:
        result: 官方 API Client 返回对象或合同测试替身。

    Returns:
        官方 API 返回的页面对象。

    Raises:
        ProviderInvalidResponse: Client 返回值不含页面数组。

    """
    pages = _value(result, "pages")
    if not _is_sequence(pages):
        raise ProviderInvalidResponse(
            "PaddleOCR 官方 API 响应缺少页面结果。",
            stage="pdf.paddle.official.response",
            code="PDF_PAGE_RESULTS_MISSING",
        )
    return tuple(pages)


def provider_job_id(result: object) -> str | None:
    """只读取可安全诊断的官方 job_id。"""
    raw = _value(result, "job_id", "jobId")
    if isinstance(raw, str) and 0 < len(raw) <= _MAX_PROVIDER_ID_LENGTH:
        return raw
    return None


def normalize_paddle_pages(
    raw_pages: Sequence[object],
    inspection: PdfSourceInspection,
    *,
    page_indices: Sequence[int] | None = None,
) -> tuple[PdfPage, ...]:
    """按已核验的原 PDF 物理顺序绑定页面并标准化块。

    Args:
        raw_pages: 自托管数组或官方 API pages。
        inspection: 本地证明的总页数和文本层状态。
        page_indices: 分页调用时对应的零基物理页；默认按数组顺序。

    Returns:
        不猜测缺失 bbox、置信度或页码的规范化页面。

    Raises:
        ProviderInvalidResponse: 页号、块或表格身份违反合同。

    """
    indices = (
        tuple(range(len(raw_pages)))
        if page_indices is None
        else tuple(page_indices)
    )
    if len(indices) != len(raw_pages) or any(
        index < 0 or index >= inspection.page_count for index in indices
    ):
        raise ProviderInvalidResponse(
            "PaddleOCR 页面绑定超出原 PDF 物理页范围。",
            stage="pdf.paddle.normalize",
            code="PDF_PAGE_BINDING_INVALID",
        )
    try:
        return tuple(
            _normalize_page(raw_page, inspection, page_index)
            for raw_page, page_index in zip(raw_pages, indices, strict=True)
        )
    except ProviderInvalidResponse:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise ProviderInvalidResponse(
            "PaddleOCR 页面或块结果违反规范化合同。",
            stage="pdf.paddle.normalize",
            code="PADDLE_RESPONSE_FORMAT_INVALID",
            details={"error_type": type(error).__name__},
        ) from None


def _normalize_page(
    raw_page: object,
    inspection: PdfSourceInspection,
    page_index: int,
) -> PdfPage:
    pruned = _value(raw_page, "pruned_result", "prunedResult")
    if pruned is None:
        pruned = raw_page
    page = _mapping(pruned, "PaddleOCR 页面结果必须是 object。")
    raw_blocks = _value(page, "parsing_res_list", "parsingResList")
    width = _positive_number(_value(page, "width", "page_width", "pageWidth"))
    height = _positive_number(
        _value(page, "height", "page_height", "pageHeight")
    )
    if (width is None) != (height is None):
        width = height = None
    blocks: list[PdfBlock] = []
    if _is_sequence(raw_blocks):
        prepared = [
            _normalize_block(item, position, page_index)
            for position, item in enumerate(raw_blocks)
        ]
        prepared.sort(
            key=lambda item: (
                item.provider_order
                if item.provider_order is not None
                else item.order,
                item.order,
            )
        )
        blocks = [
            item.model_copy(update={"order": order})
            for order, item in enumerate(prepared)
        ]
    if not blocks:
        markdown = _page_markdown(raw_page)
        if markdown.strip():
            blocks.append(
                PdfBlock(
                    block_id=_derived_block_id(
                        page_index, 0, "markdown_fallback", markdown
                    ),
                    block_type=PdfBlockType.PARAGRAPH,
                    raw_label="markdown_fallback",
                    order=0,
                    text=markdown,
                    markdown=markdown,
                )
            )
    return PdfPage(
        page_index=page_index,
        width=width,
        height=height,
        blocks=tuple(blocks),
        native_text_layer=inspection.native_text_layer[page_index],
    )


def _normalize_block(
    raw_block: object,
    position: int,
    page_index: int,
) -> PdfBlock:
    block = _mapping(raw_block, "PaddleOCR block 必须是 object。")
    raw_label = str(
        _value(block, "block_label", "blockLabel", "label", "type")
        or "unsupported"
    ).casefold()
    block_type = _block_type(raw_label)
    content = _value(block, "block_content", "blockContent", "content", "text")
    text = content if isinstance(content, str) and content else None
    provider_order = _nonnegative_int(
        _value(block, "block_order", "blockOrder", "order")
    )
    raw_id = _value(block, "block_id", "blockId", "id")
    if block_type is PdfBlockType.TABLE and not _valid_id(raw_id):
        raise ProviderInvalidResponse(
            "PaddleOCR 表格块缺少稳定 table_id。",
            stage="pdf.paddle.normalize",
            code="PDF_TABLE_ID_MISSING",
            details={"page_index": page_index},
        )
    block_id = (
        str(raw_id)
        if _valid_id(raw_id)
        else _derived_block_id(page_index, position, raw_label, text or "")
    )
    bbox = _bbox(_value(block, "block_bbox", "blockBbox", "bbox"))
    table = (
        _parse_table(block_id, text)
        if block_type is PdfBlockType.TABLE and text
        else None
    )
    return PdfBlock(
        block_id=block_id,
        block_type=block_type,
        raw_label=raw_label[:120],
        order=position,
        provider_order=provider_order,
        text=text,
        markdown=text,
        bbox=bbox,
        table=table,
    )


def _block_type(label: str) -> PdfBlockType:
    return _BLOCK_TYPES_BY_LABEL.get(label, PdfBlockType.UNSUPPORTED)


class _TableHtmlParser(HTMLParser):
    """只提取 HTML table 的实际单元格文字和 span。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._cell: list[str] | None = None
        self._row_span = 1
        self._column_span = 1

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() == "tr":
            self._row = []
        if tag.casefold() not in {"td", "th"} or self._row is None:
            return
        values = {key.casefold(): value for key, value in attrs}
        self._cell = []
        self._row_span = _span(values.get("rowspan"))
        self._column_span = _span(values.get("colspan"))

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(
                    (
                        "".join(self._cell).strip(),
                        self._row_span,
                        self._column_span,
                    )
                )
            self._cell = None
        elif normalized == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _parse_table(table_id: str, content: str) -> PdfTable | None:
    lowered = content.casefold()
    if "<table" in lowered and "</table" in lowered:
        parser = _TableHtmlParser()
        try:
            parser.feed(content)
            cells = _cells_from_rows(parser.rows)
        except (ValueError, OverflowError):
            return None
        if cells:
            return PdfTable(
                table_id=table_id,
                source_format="html",
                cells=cells,
            )
    rows = _markdown_rows(content)
    cells = _cells_from_rows(rows)
    if cells:
        return PdfTable(
            table_id=table_id,
            source_format="markdown",
            cells=cells,
        )
    return None


def _markdown_rows(content: str) -> list[list[tuple[str, int, int]]]:
    rows: list[list[tuple[str, int, int]]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or "|" not in stripped:
            continue
        values = [item.strip() for item in stripped.strip("|").split("|")]
        if values and all(
            _MARKDOWN_SEPARATOR.fullmatch(item) for item in values
        ):
            continue
        rows.append([(value, 1, 1) for value in values])
    return rows


def _cells_from_rows(
    rows: Sequence[Sequence[tuple[str, int, int]]],
) -> tuple[PdfTableCell, ...]:
    cells: list[PdfTableCell] = []
    occupied: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        column_index = 0
        for value, row_span, column_span in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            cells.append(
                PdfTableCell(
                    row_index=row_index,
                    column_index=column_index,
                    text=value,
                    row_span=row_span,
                    column_span=column_span,
                )
            )
            for row_offset in range(row_span):
                for column_offset in range(column_span):
                    occupied.add(
                        (row_index + row_offset, column_index + column_offset)
                    )
            column_index += column_span
    return tuple(cells)


def _page_markdown(raw_page: object) -> str:
    markdown = _value(raw_page, "markdown")
    text = _value(markdown, "text", "markdown_text")
    if isinstance(text, str):
        return text
    text = _value(raw_page, "markdown_text", "markdownText")
    return text if isinstance(text, str) else ""


def _mapping(value: object, message: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise ProviderInvalidResponse(
        message,
        stage="pdf.paddle.response",
        code="PADDLE_RESPONSE_FORMAT_INVALID",
    )


def _value(value: object, *names: str) -> object | None:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return cast(object, value[name])
        if value is not None and hasattr(value, name):
            return cast(object, getattr(value, name))
    return None


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, (str, int))
        and 0 < len(str(value)) <= _MAX_BLOCK_ID_LENGTH
    )


def _derived_block_id(
    page_index: int, position: int, label: str, content: str
) -> str:
    digest = hashlib.sha256(
        f"{page_index}\x1f{position}\x1f{label}\x1f{content}".encode()
    ).hexdigest()
    return f"block-{digest[:24]}"


def _positive_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        converted = float(value)
        return converted if math.isfinite(converted) and converted > 0 else None
    return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _bbox(value: object) -> tuple[float, float, float, float] | None:
    if not _is_sequence(value) or len(value) != _BBOX_COORDINATE_COUNT:
        return None
    if any(
        not isinstance(item, (int, float)) or isinstance(item, bool)
        for item in value
    ):
        return None
    coordinates = cast(Sequence[int | float], value)
    left, top, right, bottom = (float(item) for item in coordinates)
    if not all(math.isfinite(item) for item in (left, top, right, bottom)):
        return None
    if min(left, top) < 0 or right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _span(value: str | None) -> int:
    try:
        parsed = int(value or "1")
    except ValueError:
        return 1
    return min(max(parsed, 1), 1000)


def _safe_scalar(value: object) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return type(value).__name__


__all__ = [
    "normalize_paddle_pages",
    "official_pages",
    "provider_job_id",
    "self_hosted_pages",
]
