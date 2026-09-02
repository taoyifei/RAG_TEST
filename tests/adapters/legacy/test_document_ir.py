from __future__ import annotations

from pathlib import Path

from rag_app.adapters.legacy.document_ir import (
    document_ir_to_legacy_elements,
)
from rag_app.adapters.legacy.stores import InMemoryBlobStore
from rag_app.adapters.parsers.legacy_docx_ir import LegacyDocxIrParser
from rag_app.core.models import ParseSource
from rag_app.core.policies import ParsingPolicy
from rag_app.parsers.docx import DocxParser
from tests.adapters.parsers.docx_fixtures import (
    HEADING,
    PARAGRAPH,
    TABLE,
    build_docx,
)


def _policy() -> ParsingPolicy:
    return ParsingPolicy(
        metadata=(
            ("project_id", f"prj_{'1' * 32}"),
            ("knowledge_base_id", f"kb_{'2' * 32}"),
            ("document_id", f"doc_{'3' * 32}"),
        )
    )


def test_old_parser_and_ir_round_trip_preserve_text_order_and_locator(
    tmp_path: Path,
) -> None:
    content = build_docx(HEADING + PARAGRAPH + TABLE)
    path = tmp_path / "basic.docx"
    path.write_bytes(content)
    old_elements, _ = DocxParser().parse_with_audit(
        path,
        display_path=path.name,
    )
    store = InMemoryBlobStore()
    result = LegacyDocxIrParser(store).parse(
        ParseSource(
            media_type="application/octet-stream",
            display_name=path.name,
            content=content,
        ),
        _policy(),
    )

    round_trip, report = document_ir_to_legacy_elements(
        result.document_ir,
        store,
    )

    assert [(item.kind, item.text) for item in round_trip] == [
        (item.kind, item.text) for item in old_elements
    ]
    assert [item.locator.logical_key() for item in round_trip] == [
        item.locator.logical_key() for item in old_elements
    ]
    assert report.converted_count == len(old_elements)
    assert report.skipped_count == 0
    assert {issue.code for issue in report.issues} == {
        "IR_TABLE_EXPORTED_AS_FLAT_TEXT"
    }
