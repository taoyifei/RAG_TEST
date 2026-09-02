from __future__ import annotations

# ruff: noqa: E501
from rag_app.core.models import NodeKind
from tests.adapters.parsers.docx.fixtures import build_package, parse_package


def test_visible_text_fields_breaks_and_hidden_runs_are_policy_driven() -> None:
    blocks = """
<w:p>
  <w:r><w:t>A</w:t><w:tab/><w:noBreakHyphen/><w:softHyphen/><w:t>B</w:t></w:r>
  <w:r><w:rPr><w:vanish/></w:rPr><w:t>机密</w:t></w:r>
  <w:fldSimple w:instr=" REF target "><w:r><w:t>引用结果</w:t></w:r></w:fldSimple>
  <w:r><w:br w:type="page"/></w:r>
</w:p>
<w:p><w:bookmarkStart w:id="1" w:name="target"/><w:r><w:t>目标</w:t></w:r></w:p>
"""
    result = parse_package(build_package(blocks))
    paragraphs = [
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.PARAGRAPH
    ]
    first = paragraphs[0]
    metadata = dict(first.metadata)

    assert first.text == "A\t‑B引用结果"
    assert first.text_payload is not None
    assert first.text_payload.exact_text == "A\t‑\u00adB引用结果"
    assert first.text_payload.semantic_text == "A\t‑B引用结果"
    assert metadata["field_types"] == ["REF"]
    assert metadata["hidden_runs"] == 1
    assert any(
        node.kind is NodeKind.BREAK
        and dict(node.metadata)["break_type"] == "page"
        for node in result.document_ir.nodes
    )
    assert "DOCX_HIDDEN_TEXT_EXCLUDED" in result.report.warnings
    assert any(
        relation.relationship_type == "document-bookmark"
        for relation in result.document_ir.relationships
    )


def test_soft_hyphen_can_be_preserved_in_semantic_text() -> None:
    result = parse_package(
        build_package('<w:p><w:r><w:t>A</w:t><w:softHyphen/><w:t>B</w:t></w:r></w:p>'),
        preserve_soft_hyphen=True,
    )
    paragraph = result.document_ir.nodes[0]

    assert paragraph.text_payload is not None
    assert paragraph.text_payload.semantic_text == "A\u00adB"


def test_complex_field_state_machine_uses_result_and_hashes_instruction() -> None:
    blocks = """
<w:p>
  <w:r><w:fldChar w:fldCharType="begin"/><w:instrText> PAGEREF target </w:instrText></w:r>
  <w:r><w:fldChar w:fldCharType="separate"/><w:t>第 7 页</w:t></w:r>
  <w:r><w:fldChar w:fldCharType="end"/></w:r>
</w:p>
"""

    result = parse_package(build_package(blocks))
    paragraph = result.document_ir.nodes[0]
    metadata = dict(paragraph.metadata)

    assert paragraph.text == "第 7 页"
    assert metadata["field_types"] == ["PAGEREF"]
    assert len(metadata["field_instruction_hashes"][0]) == 64
    assert "PAGEREF target" not in str(metadata)
