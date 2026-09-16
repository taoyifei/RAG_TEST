"""旧 DOC 模板只登记目录项，不接触原件正文。"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest
from docx import Document


def _module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    directory = Path(__file__).parents[2] / "deployment" / "wanshitong"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "legacy_template_catalog", directory / "catalog_legacy_template.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载旧模板目录项工具。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_legacy_doc_catalog_uses_filename_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _module(monkeypatch)
    relative = "04 开发中心/01 OPC模式开发流程模板/发布报告.doc"
    source = tmp_path / "corpus" / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b"PRIVATE-DOC-BODY-NOT-INDEXABLE")
    artifact = tmp_path / "catalog.docx"
    result = catalog.prepare_catalog(tmp_path / "corpus", relative, artifact)
    assert result["original_bytes_read"] == 0
    assert result["catalog_source_relative_path"].endswith("发布报告.docx")
    content = artifact.read_bytes()
    assert b"PRIVATE-DOC-BODY-NOT-INDEXABLE" not in content
    text = "\n".join(
        paragraph.text for paragraph in Document(io.BytesIO(content)).paragraphs
    )
    assert "发布报告（原件 .doc）" in text
    assert "参考原始模板" in text


def test_non_template_doc_and_docx_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _module(monkeypatch)
    for path in (
        "04 开发中心/01 业务流程/发布报告.doc",
        "04 开发中心/01 OPC模式开发流程模板/发布报告.docx",
    ):
        with pytest.raises(catalog.ImportContractError):
            catalog.catalog_identity(path)
