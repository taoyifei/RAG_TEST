"""WB-06 文档分类解析、路径安全与稳定 Key 门禁。"""

from __future__ import annotations

import json

import pytest

from rag_app.wanshitong.document_metadata import (
    ALL_INTERNAL,
    DOCUMENT_METADATA_REVISION,
    WanshitongUploadMetadata,
    normalize_source_relative_path,
    parse_upload_metadata,
    resolve_document_metadata,
    stable_department_key,
)
from rag_app.wanshitong.errors import AdminFacadeError
from rag_app.wanshitong.public_stream import render_public_final
from tests.wanshitong.support import synthetic_answer


def test_directory_path_parses_ordered_department_and_category() -> None:
    metadata = resolve_document_metadata(
        WanshitongUploadMetadata(
            source_relative_path=(" 01 科管\\02 科研项目管理\\制度汇编.docx ")
        )
    )

    assert metadata.department_name == "01 科管"
    assert metadata.department_key == "01-科管-94102e0b95"
    assert metadata.category_path == ("02 科研项目管理",)
    assert metadata.document_title == "制度汇编"
    assert (
        metadata.source_relative_path == "01 科管/02 科研项目管理/制度汇编.docx"
    )
    assert metadata.visibility_scope == ALL_INTERNAL
    assert metadata.allowed_roles == ()
    assert metadata.allowed_groups == ()
    assert metadata.metadata_revision == DOCUMENT_METADATA_REVISION


def test_double_hyphen_filename_is_supported_without_directories() -> None:
    metadata = resolve_document_metadata(
        WanshitongUploadMetadata(
            source_relative_path=("开发中心--OPC理念--OPC模式开发流程规范.docx")
        )
    )

    assert metadata.department_name == "开发中心"
    assert metadata.category_path == ("OPC理念",)
    assert metadata.document_title == "OPC模式开发流程规范"


def test_explicit_values_override_path_inference() -> None:
    metadata = resolve_document_metadata(
        WanshitongUploadMetadata(
            source_relative_path="01 科管/02 科研项目管理/原始标题.docx",
            department_name="平台管理",
            category_path=("显式分类", "二级分类"),
            document_title="管理员标题",
            topic_keys=("Policy", "policy", "流程"),
        )
    )

    assert metadata.department_name == "平台管理"
    assert metadata.category_path == ("显式分类", "二级分类")
    assert metadata.document_title == "管理员标题"
    assert metadata.topic_keys == ("policy", "流程")
    assert (
        metadata.source_relative_path == "01 科管/02 科研项目管理/原始标题.docx"
    )


def test_display_title_decodes_html_entity_but_source_path_is_unchanged() -> (
    None
):
    source_path = "04 开发中心/复盘/版本&amp;迭代复盘报告.docx"
    metadata = resolve_document_metadata(
        WanshitongUploadMetadata(source_relative_path=source_path)
    )

    assert metadata.document_title == "版本&迭代复盘报告"
    assert metadata.source_relative_path == source_path


@pytest.mark.parametrize(
    "source_path",
    [
        "../越界.docx",
        "%2e%2e/编码越界.docx",
        "%252e%252e/二次编码越界.docx",
        "&#46;&#46;/实体越界.docx",
        "%26%2346%3B%26%2346%3B/组合编码越界.docx",
        "/absolute.docx",
        "C:/Users/文件.docx",
        "\\\\server\\share\\文件.docx",
        "目录//文件.docx",
        "目录/./文件.docx",
        "目录/\x00文件.docx",
        "目录/\x01文件.docx",
        "目录/．．/全角越界.docx",
        "目录/   /文件.docx",
        "目录/" + "超" * 256 + ".docx",
    ],
)
def test_unsafe_relative_paths_are_rejected(source_path: str) -> None:
    with pytest.raises(AdminFacadeError) as raised:
        normalize_source_relative_path(source_path)

    assert raised.value.code == "INVALID_RELATIVE_PATH"


def test_department_key_is_stable_and_not_python_hash_based() -> None:
    assert stable_department_key("01 科管") == "01-科管-94102e0b95"
    assert stable_department_key("  01 科管  ") == "01-科管-94102e0b95"
    assert stable_department_key("０１ 科管") == "01-科管-94102e0b95"


def test_upload_contract_rejects_client_controlled_acl() -> None:
    payload = json.dumps(
        {
            "source_relative_path": "01 科管/制度.docx",
            "visibility_scope": "public",
            "allowed_roles": ["admin"],
        },
        ensure_ascii=False,
    )

    with pytest.raises(AdminFacadeError) as raised:
        parse_upload_metadata(
            relative_path=None,
            source_relative_path=None,
            metadata_json=payload,
        )

    assert raised.value.code == "INVALID_DOCUMENT_METADATA"


def test_public_citation_displays_safe_document_metadata() -> None:
    rendered = render_public_final(synthetic_answer("trace_" + "1" * 32))
    citation = rendered["citations"][0]

    assert citation["department_name"] == "政务服务部"
    assert citation["category_path"] == ("办事服务", "材料办理")
    assert citation["document_title"] == "湾事通办事指南"
    assert (
        citation["source_relative_path"]
        == "政务服务部/办事服务/湾事通办事指南.docx"
    )
    assert not str(citation["source_relative_path"]).startswith(("/", "\\"))
