from __future__ import annotations

from typing import cast

import pytest

from rag_app.application.retrieval.neighbors import NeighborExpander
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChunkRole,
    HydratedChunk,
    RankedChunk,
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


def _table_chain(
    base: int,
    *,
    document_number: int,
    display_name: str,
    length: int = 9,
) -> tuple[RankedChunk, ...]:
    """构造带真实双向链接和表格坐标的合成 chunk 链。"""
    chunk_ids = [f"chunk_{base + index:032x}" for index in range(length)]
    result = []
    for index, chunk_id in enumerate(chunk_ids):
        candidate = make_ranked_chunk(
            base + index,
            f"角色职责片段 {index}",
            role=ChunkRole.TABLE,
            document_number=document_number,
            section_id=f"section-{document_number}",
            neighbor_group_id=f"group-{document_number}",
            previous_chunk_id=chunk_ids[index - 1] if index else None,
            next_chunk_id=(
                chunk_ids[index + 1] if index + 1 < length else None
            ),
        )
        chunk = candidate.hydrated.chunk
        span = chunk.source_spans[0]
        assert span.source_anchor is not None
        path = (
            "body",
            f"tbl:{document_number}",
            f"tr:{index}",
            "tc:0",
            f"p:{index}",
        )
        anchor = span.source_anchor.model_copy(update={"structural_path": path})
        span = span.model_copy(
            update={"source_anchor": anchor, "structural_path": path}
        )
        chunk = chunk.model_copy(
            update={"chunk_id": chunk_id, "source_spans": (span,)}
        )
        result.append(
            candidate.model_copy(
                update={
                    "hydrated": candidate.hydrated.model_copy(
                        update={"chunk": chunk, "display_name": display_name}
                    )
                }
            )
        )
    return tuple(result)


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
    source = cast(EvidenceSourcePort, _NeighborSource((previous.hydrated,)))
    outcome = NeighborExpander(source).expand(
        _snapshot(), (origin,), mode, RetrievalPolicy()
    )

    assert [item.hydrated.chunk.chunk_id for item in outcome.candidates] == [
        origin.hydrated.chunk.chunk_id,
        previous.hydrated.chunk.chunk_id,
    ]
    assert outcome.candidates[1].expansion_reason == reason


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
    source = cast(EvidenceSourcePort, _NeighborSource((previous.hydrated,)))
    outcome = NeighborExpander(source).expand(
        _snapshot(), (origin,), "same_group", RetrievalPolicy()
    )

    assert outcome.candidates == (origin,)
    assert outcome.degraded_reason_codes == ("NEIGHBOR_INDEX_CORRUPT",)


def test_table_expansion_prioritizes_a_uniquely_qualified_source() -> None:
    noise_chains = tuple(
        _table_chain(
            100 * number,
            document_number=number,
            display_name=f"白鹭流程制度-{number}.docx",
        )
        for number in range(1, 6)
    )
    selected = _table_chain(
        900,
        document_number=9,
        display_name="蓝熊交付规范.docx",
        length=7,
    )
    seeds = (*[chain[0] for chain in noise_chains], selected[0])
    stored = tuple(
        item.hydrated for chain in (*noise_chains, selected) for item in chain
    )
    source = cast(EvidenceSourcePort, _NeighborSource(stored))
    expander = NeighborExpander(source)
    policy = RetrievalPolicy(fusion_candidate_limit=48, max_evidence_items=8)

    unqualified = expander.expand(_snapshot(), seeds, "table", policy)
    qualified = expander.expand(
        _snapshot(),
        seeds,
        "table",
        policy,
        source_qualifier="蓝熊规范",
    )

    selected_ids = {item.hydrated.chunk.chunk_id for item in selected}
    assert not selected_ids <= {
        item.hydrated.chunk.chunk_id for item in unqualified.candidates
    }
    assert selected_ids <= {
        item.hydrated.chunk.chunk_id for item in qualified.candidates
    }
    assert len(qualified.candidates) <= policy.fusion_candidate_limit

    other_match = _table_chain(
        700,
        document_number=7,
        display_name="蓝熊研发规范.docx",
    )
    ambiguous_chains = (other_match, *noise_chains[1:], selected)
    ambiguous_seeds = tuple(chain[0] for chain in ambiguous_chains)
    ambiguous_stored = tuple(
        item.hydrated for chain in ambiguous_chains for item in chain
    )
    ambiguous_source = cast(
        EvidenceSourcePort, _NeighborSource(ambiguous_stored)
    )
    ambiguous_expander = NeighborExpander(ambiguous_source)
    baseline = ambiguous_expander.expand(
        _snapshot(), ambiguous_seeds, "table", policy
    )
    unresolved = ambiguous_expander.expand(
        _snapshot(),
        ambiguous_seeds,
        "table",
        policy,
        source_qualifier="蓝熊规范",
    )
    assert unresolved.candidates == baseline.candidates
