"""闭库纠错只回读已命中文档的有界同章节上下文。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from rag_app.application.retrieval.service import RetrievalService
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import RetrievalPolicy
from rag_app.core.models.query_plan import (
    AtomCandidateLink,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
)
from tests.application.retrieval.helpers import make_ranked_chunk


def _service(source: Mock) -> RetrievalService:
    service = object.__new__(RetrievalService)
    service._source = source
    service._policy = RetrievalPolicy()
    return service


def _matrix() -> AtomSupportMatrix:
    return AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.PARTIAL),)
    )


def _link(chunk_id: str) -> AtomCandidateLink:
    return AtomCandidateLink(
        atom_id="A1",
        chunk_id=chunk_id,
        channels=("lexical",),
        best_rank=1,
        score=1.0,
    )


def test_correction_reads_at_most_twelve_chunks_and_four_groups() -> None:
    anchor = make_ranked_chunk(1, "甲提交材料。")
    neighbors = tuple(
        make_ranked_chunk(index, f"同章节条款{index}。")
        for index in range(2, 20)
    )
    source = Mock()
    source.section_chunk_ids.return_value = (
        anchor.hydrated.chunk.chunk_id,
        *(item.hydrated.chunk.chunk_id for item in neighbors),
    )
    source.hydrate_chunks.return_value = tuple(
        item.hydrated for item in neighbors[:12]
    )
    service = _service(source)

    candidates, groups, added, document_id, section_id = (
        service._corrective_retrieval(
            snapshot=object(),  # type: ignore[arg-type]
            candidates=(anchor,),
            groups=(),
            links=(_link(anchor.hydrated.chunk.chunk_id),),
            matrix=_matrix(),
        )
    )

    assert added == 12
    assert len(candidates) == 13
    assert len(groups) <= 4
    assert document_id == anchor.hydrated.chunk.version.document_id
    assert section_id == anchor.hydrated.chunk.section_id
    assert len(source.hydrate_chunks.call_args.args[1]) == 12
    source.section_chunk_ids.assert_called_once()


def test_correction_rejects_cross_document_hydration() -> None:
    anchor = make_ranked_chunk(1, "甲提交材料。", document_number=2)
    wrong = make_ranked_chunk(2, "乙审核材料。", document_number=3)
    source = Mock()
    source.section_chunk_ids.return_value = (
        anchor.hydrated.chunk.chunk_id,
        wrong.hydrated.chunk.chunk_id,
    )
    source.hydrate_chunks.return_value = (wrong.hydrated,)
    service = _service(source)

    with pytest.raises(IndexCorrupt):
        service._corrective_retrieval(
            snapshot=object(),  # type: ignore[arg-type]
            candidates=(anchor,),
            groups=(),
            links=(_link(anchor.hydrated.chunk.chunk_id),),
            matrix=_matrix(),
        )
