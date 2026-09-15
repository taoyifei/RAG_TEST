"""WB-07 使用的 46 项 DOCX-only 控制清单 schema。"""

from __future__ import annotations

import html
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from rag_app.core.models.common import FrozenModel
from rag_app.wanshitong.document_metadata import normalize_source_relative_path
from rag_app.wanshitong.errors import AdminFacadeError
from rag_app.wanshitong.upload_validation import DOCX_MEDIA_TYPE

DOCX_MANIFEST_DOCUMENT_COUNT = 46
EXPECTED_SPACES = frozenset({"01 科管", "02 人力", "03 综合", "04 开发中心"})


class DocxInventoryCounts(FrozenModel):
    """原始清单的格式数量边界。"""

    all_document_rows: Literal[139]
    docx_included: Literal[46]
    pdf_deferred: Literal[68]
    doc_deferred: Literal[12]
    xlsx_excluded: Literal[7]
    xls_excluded: Literal[4]
    zip_deferred: Literal[2]
    empty_category_placeholders: Literal[6]


class DocxDefaultScope(FrozenModel):
    """当前 Demo 必须保持的单 Scope 与空筛选合同。"""

    single_hidden_project: Literal[True]
    single_hidden_knowledge_base: Literal[True]
    visibility_scope: Literal["all_internal"]
    departments: tuple[str, ...] = Field(max_length=0)
    shortcut_id: None = None
    department_selector_visible: Literal[False]
    shortcut_selector_visible: Literal[False]


class DocxManifestDocument(FrozenModel):
    """单个未来导入项；校验元数据但不读取文档字节。"""

    document_id: str = Field(pattern=r"^DOCX-[0-9]{3}$")
    excel_row: int = Field(gt=0)
    space: Literal["01 科管", "02 人力", "03 综合", "04 开发中心"]
    knowledge_base: str = Field(min_length=1, max_length=255)
    source_relative_path: str = Field(min_length=1, max_length=4096)
    display_name: str = Field(min_length=1, max_length=512)
    media_type: Literal[
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ]
    visibility_scope: Literal["all_internal"]
    default_search: Literal[True]
    department_filter_enabled: Literal[False]
    shortcut_filter_enabled: Literal[False]
    pilot: bool

    @model_validator(mode="after")
    def _validate_docx_path(self) -> DocxManifestDocument:
        try:
            normalized_path, basename = normalize_source_relative_path(
                self.source_relative_path
            )
        except AdminFacadeError as error:
            raise ValueError("manifest source_relative_path 无效。") from error
        if normalized_path.split("/", maxsplit=1)[0] != self.space:
            raise ValueError("manifest space 与相对路径第一级目录不一致。")
        normalized_display = unicodedata.normalize(
            "NFKC", self.display_name.strip()
        )
        if html.unescape(basename) != normalized_display:
            raise ValueError(
                "manifest display_name 与相对路径 basename 不一致。"
            )
        if not basename.casefold().endswith(".docx"):
            raise ValueError("manifest 只能包含 .docx。")
        if self.media_type != DOCX_MEDIA_TYPE:
            raise ValueError("manifest DOCX media type 不一致。")
        return self


class DocxOnlyManifest(FrozenModel):
    """未来 WB-07 导入前必须完整通过的 46 项控制清单。"""

    schema_version: Literal["wanshitong-docx-demo-v1"]
    source_inventory: str = Field(min_length=1, max_length=512)
    inventory_counts: DocxInventoryCounts
    default_scope: DocxDefaultScope
    documents: tuple[DocxManifestDocument, ...] = Field(
        min_length=DOCX_MANIFEST_DOCUMENT_COUNT,
        max_length=DOCX_MANIFEST_DOCUMENT_COUNT,
    )

    @model_validator(mode="after")
    def _validate_complete_manifest(self) -> DocxOnlyManifest:
        identifiers = tuple(item.document_id for item in self.documents)
        paths = tuple(item.source_relative_path for item in self.documents)
        if len(set(identifiers)) != DOCX_MANIFEST_DOCUMENT_COUNT:
            raise ValueError("manifest document_id 必须唯一。")
        if len(set(paths)) != DOCX_MANIFEST_DOCUMENT_COUNT:
            raise ValueError("manifest source_relative_path 必须唯一。")
        if {item.space for item in self.documents} != EXPECTED_SPACES:
            raise ValueError("manifest 必须覆盖全部四个空间。")
        expected_identifiers = tuple(
            f"DOCX-{index:03d}"
            for index in range(1, DOCX_MANIFEST_DOCUMENT_COUNT + 1)
        )
        if tuple(sorted(identifiers)) != expected_identifiers:
            raise ValueError("manifest 必须完整包含 DOCX-001 到 DOCX-046。")
        return self


def load_docx_only_manifest(path: str | Path) -> DocxOnlyManifest:
    """只读取并校验 JSON 清单，不访问其中指向的 DOCX 文件。"""
    manifest_path = Path(path)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("DOCX manifest 必须是普通 JSON 文件。")
    payload = manifest_path.read_text(encoding="utf-8")
    if len(payload.encode("utf-8")) > 1024 * 1024:
        raise ValueError("DOCX manifest 超过 1 MiB 上限。")
    return DocxOnlyManifest.model_validate_json(payload)


__all__ = [
    "DOCX_MANIFEST_DOCUMENT_COUNT",
    "EXPECTED_SPACES",
    "DocxDefaultScope",
    "DocxInventoryCounts",
    "DocxManifestDocument",
    "DocxOnlyManifest",
    "load_docx_only_manifest",
]
