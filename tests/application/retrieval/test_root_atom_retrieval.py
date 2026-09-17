"""Root 与 Atom 分层融合的有界配额和来源合同。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.query_plan_retrieval import (
    QueryUnit,
    QueryUnitRetrieval,
    fuse_query_units,
)
from rag_app.core.models import (
    ChannelHit,
    KnowledgeBaseScope,
    RetrievalPolicy,
    SearchRequest,
)

_REVISION = f"irev_{'1' * 32}"
_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'2' * 32}",
    knowledge_base_id=f"kb_{'3' * 32}",
)


def _hit(number: int, rank: int, *, channel: str = "lexical") -> ChannelHit:
    return ChannelHit(
        revision_id=_REVISION,
        chunk_id=f"chunk_{number:032x}",
        document_id=f"doc_{'4' * 32}",
        document_version_id=f"dver_{'5' * 32}",
        role="text",
        section_id="synthetic-section",
        content_sha256="6" * 64,
        channel=channel,
        rank=rank,
        raw_score=float(rank),
    )


def _unit(
    unit_id: str,
    hits: tuple[ChannelHit, ...],
    *,
    atom_count: int,
) -> QueryUnitRetrieval:
    question = "甲与乙分别做什么？"
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text=question)
    )
    return QueryUnitRetrieval(
        QueryUnit(
            unit_id=unit_id,
            atom_id=None if unit_id == "ROOT" else unit_id,
            text=question if unit_id == "ROOT" else f"{unit_id}职责",
            analysis=analysis,
            weight=1.0 if unit_id == "ROOT" else 1.0 / atom_count,
        ),
        {"lexical": hits},
    )


def test_root_recall_survives_wrong_atom_candidates() -> None:
    root = _unit("ROOT", (_hit(1, 1),), atom_count=2)
    wrong_first = _unit("A1", (_hit(2, 1),), atom_count=2)
    wrong_second = _unit("A2", (_hit(3, 1),), atom_count=2)

    outcome = fuse_query_units(
        (root, wrong_first, wrong_second),
        revision_id=_REVISION,
        policy=RetrievalPolicy(),
    )

    assert outcome.seed_chunk_ids == tuple(
        f"chunk_{number:032x}" for number in (1, 2, 3)
    )
    assert any(
        link.unit_id == "ROOT" and link.atom_id is None
        for link in outcome.links
    )
    assert {candidate.chunk_id for candidate in outcome.candidates} == {
        f"chunk_{number:032x}" for number in (1, 2, 3)
    }


def test_each_atom_has_two_seed_places_after_six_root_places() -> None:
    root = _unit(
        "ROOT", tuple(_hit(number, number) for number in range(1, 9)),
        atom_count=3,
    )
    atoms = tuple(
        _unit(
            f"A{index}",
            tuple(
                _hit(index * 10 + offset, offset)
                for offset in range(1, 4)
            ),
            atom_count=3,
        )
        for index in range(1, 4)
    )

    outcome = fuse_query_units(
        (root, *atoms),
        revision_id=_REVISION,
        policy=RetrievalPolicy(),
    )

    assert outcome.seed_chunk_ids[:6] == tuple(
        f"chunk_{number:032x}" for number in range(1, 7)
    )
    assert set(outcome.seed_chunk_ids[6:]) == {
        f"chunk_{number:032x}"
        for number in (11, 12, 21, 22, 31, 32)
    }
    assert len(outcome.candidates) <= 32


def test_repeated_atom_hit_does_not_gain_linear_score() -> None:
    def score(atom_count: int) -> float:
        root = _unit("ROOT", (_hit(1, 1),), atom_count=atom_count)
        atoms = tuple(
            _unit(f"A{index}", (_hit(2, 1),), atom_count=atom_count)
            for index in range(1, atom_count + 1)
        )
        result = fuse_query_units(
            (root, *atoms),
            revision_id=_REVISION,
            policy=RetrievalPolicy(),
        )
        return next(
            item.score
            for item in result.candidates
            if item.chunk_id == f"chunk_{2:032x}"
        )

    assert score(1) == pytest.approx(score(3))
    assert score(3) == pytest.approx(1 / 61)
