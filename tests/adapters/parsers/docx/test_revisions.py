from __future__ import annotations

# ruff: noqa: E501
import pytest

from rag_app.core.errors import InvalidDocument
from tests.adapters.parsers.docx.fixtures import build_package, parse_package

_BLOCKS = """
<w:p>
  <w:r><w:t>保留</w:t></w:r>
  <w:del w:author="甲" w:date="2026-01-01"><w:r><w:delText>删除</w:delText></w:r></w:del>
  <w:ins w:author="乙" w:date="2026-01-02"><w:r><w:t>新增</w:t></w:r></w:ins>
</w:p>
"""


def test_final_view_keeps_insertions_and_audits_revision_count() -> None:
    result = parse_package(build_package(_BLOCKS))
    paragraph = result.document_ir.nodes[0]

    assert paragraph.text == "保留新增"
    assert result.report.revision_count == 2
    assert paragraph.revision_mark is not None
    assert paragraph.revision_mark.kind == "ins"


def test_reject_policy_stops_on_tracked_changes() -> None:
    with pytest.raises(InvalidDocument, match="修订"):
        parse_package(
            build_package(_BLOCKS),
            tracked_changes="reject",
        )


@pytest.mark.parametrize(
    ("removed", "kept"),
    [
        ("moveFrom", "moveTo"),
        ("del", "ins"),
    ],
)
def test_block_level_revisions_keep_only_final_view(
    removed: str,
    kept: str,
) -> None:
    blocks = (
        f'<w:{removed}><w:p><w:r><w:delText>旧段落</w:delText>'
        f"</w:r></w:p></w:{removed}>"
        f'<w:{kept}><w:p><w:r><w:t>新段落</w:t>'
        f"</w:r></w:p></w:{kept}>"
    )

    result = parse_package(build_package(blocks))

    assert [node.text for node in result.document_ir.nodes if node.text] == [
        "新段落"
    ]
    assert result.report.revision_count == 2


def test_all_with_markers_audits_deleted_text_without_indexing_it() -> None:
    result = parse_package(
        build_package(_BLOCKS),
        tracked_changes="all_with_markers",
    )
    paragraph = result.document_ir.nodes[0]

    assert paragraph.text == "保留新增"
    assert dict(paragraph.metadata)["deleted_characters"] == 2


def test_missing_revision_author_is_allowed_and_audited() -> None:
    result = parse_package(
        build_package(
            '<w:p><w:ins><w:r><w:t>无作者修订</w:t></w:r></w:ins></w:p>'
        )
    )
    paragraph = result.document_ir.nodes[0]

    assert paragraph.revision_mark is not None
    assert paragraph.revision_mark.author is None
