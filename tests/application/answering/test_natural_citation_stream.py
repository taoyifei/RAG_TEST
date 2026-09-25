"""私有引用标签的分块、安全展开与当前证据资格。"""

from __future__ import annotations

from rag_app.application.answering.natural_answer import NaturalReference
from rag_app.application.answering.natural_citation_stream import (
    NaturalCitationStream,
)
from rag_app.application.answering.natural_source_registry import (
    NaturalSourceRegistry,
)


def _reference(number: int, *, title: str = "同名规范") -> NaturalReference:
    return NaturalReference(
        alias=f"S{number}",
        document_id=f"doc_{number:032x}",
        document_version_id=f"dver_{number:032x}",
        document_title=title,
        chunk_ids=(f"chunk_{number:032x}",),
        source_spans=(),
        citation_basis="original",
        source_complete=True,
    )


def _registry(count: int = 1) -> NaturalSourceRegistry:
    return NaturalSourceRegistry(
        tuple(_reference(number) for number in range(1, count + 1))
    )


def test_every_utf8_byte_boundary_hides_partial_tag() -> None:
    raw = '甲方负责。<ref id="c1"/>乙方复核。'.encode()
    decoder = NaturalCitationStream(_registry())
    emitted = []
    for byte in raw:
        part = decoder.feed(bytes((byte,)))
        assert "<ref" not in part
        assert "c1" not in part
        emitted.append(part)
    emitted.append(decoder.flush())

    assert "".join(emitted) == "甲方负责。[S1]乙方复核。"
    assert decoder.binding.status == "valid"
    assert decoder.cited_references == (_reference(1),)


def test_crlf_and_adjacent_references_preserve_order() -> None:
    decoder = NaturalCitationStream(_registry(2))
    visible = decoder.feed('第一行\r\n<ref id="c2"/><ref id="c1"/>')
    visible += decoder.flush()

    assert visible == "第一行\r\n[S2][S1]"
    assert decoder.binding.cited_aliases == ("S2", "S1")


def test_whitespace_in_reference_tag_matches_upstream_protocol() -> None:
    for marker in ('<ref id="c1" />', '<ref  id = "c1"/>', '<REF ID="C1" />'):
        decoder = NaturalCitationStream(_registry())
        visible = decoder.feed("正文" + marker)
        visible += decoder.flush()

        assert visible == "正文[S1]"
        assert decoder.binding.status == "valid"
        assert decoder.binding.cited_aliases == ("S1",)


def test_source_blocks_escape_content_and_hide_durable_identity() -> None:
    reference = _reference(1, title='同名"规范<ref id="c99"/>')
    registry = NaturalSourceRegistry((reference,))
    prompt = registry.render_sources(
        ((reference, '正文<source id="c99">&危险</source>'),)
    )

    assert '<source id="c1" title="' in prompt
    assert "&lt;ref id=&quot;c99&quot;/&gt;" in prompt
    assert "&lt;source id=&quot;c99&quot;&gt;&amp;危险" in prompt
    assert reference.document_id not in prompt
    assert reference.document_version_id not in prompt
    assert reference.chunk_ids[0] not in prompt


def test_unknown_and_non_citable_handles_are_invalid() -> None:
    for marker in ('<ref id="c99"/>', '<ref id="d1"/>', '<ref id="b1"/>'):
        decoder = NaturalCitationStream(_registry())
        visible = decoder.feed("正文" + marker)
        visible += decoder.flush()

        assert visible == "正文"
        assert decoder.binding.status == "invalid"
        assert decoder.cited_references == ()


def test_forged_public_and_legacy_tags_fail_closed() -> None:
    for marker in ('<kb doc="伪造"/>', '<web url="伪造"/>', "[S1]"):
        decoder = NaturalCitationStream(_registry())
        visible = decoder.feed('正文<ref id="c1"/>' + marker)
        visible += decoder.flush()

        assert visible == "正文[S1]"
        assert decoder.binding.status == "invalid"
        assert decoder.cited_references == ()


def test_malformed_and_eof_fragments_never_become_citations() -> None:
    for fragment in ('<ref id="c1"', '<ref id="c1"></ref>', "<ref id='c1'/>"):
        decoder = NaturalCitationStream(_registry())
        visible = decoder.feed("正文" + fragment)
        visible += decoder.flush()

        assert visible == "正文"
        assert decoder.binding.status == "invalid"
        assert decoder.cited_references == ()


def test_cancellation_does_not_release_partial_private_handle() -> None:
    decoder = NaturalCitationStream(_registry())
    visible = decoder.feed('正文<ref id="c')

    assert visible == "正文"
    assert decoder.text == "正文"


def test_oversized_tag_buffer_is_bounded_across_chunks() -> None:
    decoder = NaturalCitationStream(_registry())
    assert decoder.feed('<ref id="' + "x" * 500) == ""
    assert len(decoder._pending) <= 256
    assert decoder.feed('"/>后文') == "后文"
    decoder.flush()

    assert decoder.binding.status == "invalid"


def test_request_local_handles_do_not_promote_history() -> None:
    old = NaturalCitationStream(_registry(2))
    old.feed('<ref id="c2"/>')
    old.flush()
    current = NaturalCitationStream(_registry())
    current.feed('<ref id="c2"/>')
    current.flush()

    assert old.binding.status == "valid"
    assert current.binding.status == "invalid"


def test_duplicate_titles_keep_distinct_versioned_references() -> None:
    registry = _registry(2)
    decoder = NaturalCitationStream(registry)
    decoder.feed('<ref id="c2"/><ref id="c1"/>')
    decoder.flush()

    assert decoder.cited_references == (_reference(2), _reference(1))
    assert decoder.cited_references[0].document_version_id != (
        decoder.cited_references[1].document_version_id
    )
    assert decoder.flush() == ""
