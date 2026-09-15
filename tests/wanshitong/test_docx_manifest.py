"""WB-07 前置 46 项 DOCX-only 控制清单 schema 门禁。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rag_app.wanshitong.docx_manifest import (
    DOCX_MANIFEST_DOCUMENT_COUNT,
    EXPECTED_SPACES,
    DocxOnlyManifest,
    load_docx_only_manifest,
)
from rag_app.wanshitong.upload_validation import DOCX_MEDIA_TYPE

_MANIFEST_PATH = Path(__file__).parents[2] / "DOCX_ONLY_MANIFEST_46.json"


def _payload() -> dict[str, object]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_supplied_manifest_has_exact_docx_only_scope() -> None:
    manifest = load_docx_only_manifest(_MANIFEST_PATH)

    assert len(manifest.documents) == DOCX_MANIFEST_DOCUMENT_COUNT
    assert {item.space for item in manifest.documents} == EXPECTED_SPACES
    assert all(
        item.source_relative_path.casefold().endswith(".docx")
        and item.media_type == DOCX_MEDIA_TYPE
        and item.default_search is True
        and item.department_filter_enabled is False
        and item.shortcut_filter_enabled is False
        for item in manifest.documents
    )
    assert manifest.default_scope.departments == ()
    assert manifest.default_scope.shortcut_id is None
    assert manifest.default_scope.department_selector_visible is False
    assert manifest.default_scope.shortcut_selector_visible is False
    assert manifest.default_scope.visibility_scope == "all_internal"
    entity_case = next(
        item for item in manifest.documents if item.document_id == "DOCX-035"
    )
    assert "&amp;" in entity_case.source_relative_path
    assert "&" in entity_case.display_name


def test_manifest_requires_all_46_entries() -> None:
    payload = _payload()
    documents_value = payload["documents"]
    assert isinstance(documents_value, list)
    documents = list(documents_value)
    payload["documents"] = documents[:-1]

    with pytest.raises(ValidationError):
        DocxOnlyManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_relative_path", "01 科管/非法.pdf"),
        ("default_search", False),
        ("department_filter_enabled", True),
        ("shortcut_filter_enabled", True),
    ],
)
def test_manifest_rejects_non_docx_or_enabled_filters(
    field: str, value: object
) -> None:
    payload = copy.deepcopy(_payload())
    documents = payload["documents"]
    assert isinstance(documents, list)
    first = documents[0]
    assert isinstance(first, dict)
    first[field] = value

    with pytest.raises(ValidationError):
        DocxOnlyManifest.model_validate(payload)


def test_manifest_requires_all_four_spaces() -> None:
    payload = copy.deepcopy(_payload())
    documents = payload["documents"]
    assert isinstance(documents, list)
    for item in documents:
        assert isinstance(item, dict)
        if item["space"] == "02 人力":
            item["space"] = "01 科管"
            item["source_relative_path"] = str(
                item["source_relative_path"]
            ).replace("02 人力/", "01 科管/", 1)

    with pytest.raises(ValidationError):
        DocxOnlyManifest.model_validate(payload)
