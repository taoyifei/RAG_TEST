from __future__ import annotations

from typing import cast

import pytest

from rag_app.application.retrieval.neighbors import NeighborExpander
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChunkRole,
    HydratedChunk,
    RetrievalPolicy,
)
from rag_app.core.ports import EvidenceSourcePort
from tests.application.retrieval.helpers import make_ranked_chunk


class _NeighborSource:
    def __init__(self, chunks: tuple[HydratedChunk, ...]) -> None:
        self._chunks = {item.chunk.chunk_id: item for item in chunks}

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        del snapshot
        return tuple(self._chunks[item] for item in chunk_ids)

    def section_chunk_ids(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        *,
        document_version_id: str,
        section_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        del snapshot
        return tuple(
            item.chunk.chunk_id
            for item in self._chunks.values()
            if item.chunk.version.document_version_id == document_version_id
            and item.chunk.section_id == section_id
        )[:limit]


def _snapshot() -> ActiveRevisionQuerySnapshot:
    return cast(ActiveRevisionQuerySnapshot, object())


@pytest.mark.parametrize(
    ("mode", "role", "reason"),
    (
        ("same_group", ChunkRole.TEXT, "SAME_GROUP_NEIGHBOR"),
        ("table", ChunkRole.TABLE, "TABLE_CONTINUITY"),
    ),
)
def test_neighbor_and_table_expansion_require_bidirectional_links(
    mode: str, role: ChunkRole, reason: str
) -> None:
    previous = make_ranked_chunk(
        1,
        "previous",
        role=role,
        next_chunk_id=f"chunk_{2:032x}",
    )
    origin = make_ranked_chunk(
        2,
        "origin",
        role=role,
        previous_chunk_id=previous.hydrated.chunk.chunk_id,
    )
    source = cast(
        EvidenceSourcePort, _NeighborSource((previous.hydrated,))
    )
    outcome = NeighborExpander(source).expand(
        _snapshot(), (origin,), mode, RetrievalPolicy()
    )

    assert [item.hydrated.chunk.chunk_id for item in outcome.candidates] == [
        previous.hydrated.chunk.chunk_id,
        origin.hydrated.chunk.chunk_id,
    ]
    assert outcome.candidates[0].expansion_reason == reason


def test_section_expansion_is_bounded() -> None:
    origin = make_ranked_chunk(1, "origin")
    sibling = make_ranked_chunk(2, "sibling")
    source = cast(
        EvidenceSourcePort,
        _NeighborSource((origin.hydrated, sibling.hydrated)),
    )
    outcome = NeighborExpander(source).expand(
        _snapshot(),
        (origin,),
        "section",
        RetrievalPolicy(section_chunk_limit=2),
    )

    assert len(outcome.candidates) == 2
    assert outcome.candidates[1].expansion_reason == "SECTION_SIBLING"


def test_neighbor_link_damage_degrades_without_crossing_boundary() -> None:
    previous = make_ranked_chunk(1, "previous")
    origin = make_ranked_chunk(
        2,
        "origin",
        previous_chunk_id=previous.hydrated.chunk.chunk_id,
    )
    source = cast(
        EvidenceSourcePort, _NeighborSource((previous.hydrated,))
    )
    outcome = NeighborExpander(source).expand(
        _snapshot(), (origin,), "same_group", RetrievalPolicy()
    )

    assert outcome.candidates == (origin,)
    assert outcome.degraded_reason_codes == ("NEIGHBOR_INDEX_CORRUPT",)
