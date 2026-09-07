"""unit_synthetic：冻结邻块身份、排列与结构隔离对照。"""

from __future__ import annotations

import itertools
from typing import cast

import pytest

from rag_app.application.retrieval.neighbors import (
    ExpansionOutcome,
    NeighborExpander,
)
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChunkRole,
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.ports import EvidenceSourcePort
from tests.application.retrieval.helpers import make_ranked_chunk


class _Source:
    def __init__(self, chunks: tuple[HydratedChunk, ...]) -> None:
        self.chunks = chunks

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        del snapshot
        return tuple(x for x in self.chunks if x.chunk.chunk_id in chunk_ids)

    def section_chunk_ids(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        *,
        document_version_id: str,
        section_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        del snapshot, document_version_id, section_id
        return tuple(x.chunk.chunk_id for x in self.chunks)[:limit]


def _chain(role: ChunkRole = ChunkRole.TEXT) -> tuple[RankedChunk, ...]:
    return tuple(
        make_ranked_chunk(
            number,
            f"流程第{number}步由工作人员受理。",
            role=role,
            channel="dense:primary",
            previous_chunk_id=f"chunk_{number - 1:032x}"
            if number > 1
            else None,
            next_chunk_id=f"chunk_{number + 1:032x}" if number < 3 else None,
            must_keep=number == 2,
        ).model_copy(
            update={
                "rerank_rank": 4 - number,
                "rerank_score": 0.5 + number / 10,
            }
        )
        for number in (1, 2, 3)
    )


def _expand(
    seeds: tuple[RankedChunk, ...],
    stored: tuple[HydratedChunk, ...],
    mode: str,
    **limits: int,
) -> ExpansionOutcome:
    return NeighborExpander(cast(EvidenceSourcePort, _Source(stored))).expand(
        cast(ActiveRevisionQuerySnapshot, object()),
        seeds,
        mode,
        RetrievalPolicy(**limits),
    )


@pytest.mark.parametrize("mode", ("same_group", "table", "section"))
@pytest.mark.parametrize("order", tuple(itertools.permutations(range(3))))
@pytest.mark.parametrize("reverse_store", (False, True))
def test_all_direct_candidates_keep_complete_identity_and_order(
    mode: str, order: tuple[int, ...], reverse_store: bool
) -> None:
    chain = _chain(ChunkRole.TABLE if mode == "table" else ChunkRole.TEXT)
    seeds = tuple(chain[index] for index in order)
    stored = tuple(item.hydrated for item in chain)
    if reverse_store:
        stored = tuple(reversed(stored))

    outcome = _expand(seeds, stored, mode, section_chunk_limit=3)

    assert outcome.degraded_reason_codes == ()
    assert outcome.candidates == seeds
    assert all(
        actual is seed
        for actual, seed in zip(outcome.candidates, seeds, strict=True)
    )


@pytest.mark.parametrize("mode", ("same_group", "table", "section"))
@pytest.mark.parametrize("reverse_seeds", (False, True))
def test_shared_context_has_all_seeds_and_no_native_retrieval_identity(
    mode: str, reverse_seeds: bool
) -> None:
    chain = _chain(ChunkRole.TABLE if mode == "table" else ChunkRole.TEXT)
    seeds = (chain[0], chain[2])
    if reverse_seeds:
        seeds = tuple(reversed(seeds))
    outcome = _expand(
        seeds,
        tuple(item.hydrated for item in chain),
        mode,
        section_chunk_limit=3,
    )

    assert outcome.candidates[:2] == seeds
    context = outcome.candidates[2]
    assert context.hydrated == chain[1].hydrated
    assert context.contributions == ()
    assert context.rerank_rank is None
    assert context.rerank_score is None
    assert not context.must_keep
    assert context.fusion_rank > max(seed.fusion_rank for seed in seeds)
    assert context.expansion_reason
    assert context.expansion_seed_ids == tuple(
        sorted(seed.hydrated.chunk.chunk_id for seed in seeds)
    )


@pytest.mark.parametrize("mode", ("same_group", "table", "section", "none"))
def test_identical_duplicate_direct_candidate_is_stably_deduplicated(
    mode: str,
) -> None:
    chain = _chain(ChunkRole.TABLE)
    outcome = _expand(
        (chain[1], chain[1], chain[0], chain[2]),
        tuple(item.hydrated for item in chain),
        mode,
        section_chunk_limit=3,
    )
    assert outcome.candidates == (chain[1], chain[0], chain[2])
    assert outcome.degraded_reason_codes == ()


@pytest.mark.parametrize("mode", ("same_group", "table", "section", "none"))
@pytest.mark.parametrize("conflict", ("document", "hash", "rank"))
def test_conflicting_duplicate_seed_explicitly_degrades(
    mode: str, conflict: str
) -> None:
    original = _chain(ChunkRole.TABLE)[0]
    if conflict == "rank":
        changed = original.model_copy(update={"rerank_score": 0.123})
    else:
        changed = make_ranked_chunk(
            1,
            "不同原文" if conflict == "hash" else original.hydrated.chunk.text,
            document_number=8 if conflict == "document" else 2,
            role=ChunkRole.TABLE,
        )
    outcome = _expand((original, changed), (), mode)
    assert outcome.degraded_reason_codes == ("NEIGHBOR_INDEX_CORRUPT",)
    assert outcome.candidates == ()


@pytest.mark.parametrize("mode", ("same_group", "table", "section"))
@pytest.mark.parametrize(
    "boundary",
    (
        "knowledge_base_id",
        "project_id",
        "index_revision_id",
        "section_id",
        "neighbor_group_id",
        "version",
    ),
)
def test_expansion_rejects_structural_and_scope_boundary(
    mode: str, boundary: str
) -> None:
    first, second, _ = _chain(ChunkRole.TABLE)
    value = {
        "knowledge_base_id": f"kb_{'9' * 32}",
        "project_id": f"prj_{'9' * 32}",
        "index_revision_id": f"irev_{'9' * 32}",
        "section_id": "other-section",
        "neighbor_group_id": "other-group",
        "version": make_ranked_chunk(
            4, "其他文档", document_number=9
        ).hydrated.chunk.version,
    }[boundary]
    bad_chunk = second.hydrated.chunk.model_copy(update={boundary: value})
    bad = second.hydrated.model_copy(update={"chunk": bad_chunk})
    outcome = _expand((first,), (bad,), mode)
    assert outcome.candidates == (first,)
    assert outcome.degraded_reason_codes == ("NEIGHBOR_INDEX_CORRUPT",)


@pytest.mark.parametrize("mode", ("same_group", "table", "section"))
def test_hydration_cannot_replace_original_same_id_content(mode: str) -> None:
    first, second, _ = _chain(ChunkRole.TABLE)
    impostor = make_ranked_chunk(2, "篡改原文", role=ChunkRole.TABLE)
    outcome = _expand((first, second), (impostor.hydrated,), mode)
    assert outcome.degraded_reason_codes == ("NEIGHBOR_INDEX_CORRUPT",)


def test_zero_budget_preserves_direct_candidates_without_expansion() -> None:
    chain = _chain()
    stored = tuple(item.hydrated for item in chain)
    for mode, limits in (
        ("same_group", {"neighbor_count": 0}),
        ("section", {"section_chunk_limit": 0}),
    ):
        assert _expand((chain[1],), stored, mode, **limits).candidates == (
            chain[1],
        )


@pytest.mark.parametrize("mode", ("same_group", "table", "section"))
@pytest.mark.parametrize("reverse_order", (False, True))
def test_document_order_never_reassigns_a_direct_candidates_rank(
    mode: str, reverse_order: bool
) -> None:
    chain = _chain(ChunkRole.TABLE)
    unrelated = make_ranked_chunk(
        8,
        "另一份文件中的值班安排。",
        document_number=20,
        role=ChunkRole.TABLE,
    ).model_copy(update={"rerank_rank": 1, "rerank_score": 0.98})
    seeds = (chain[2], unrelated, chain[0], chain[1])
    if reverse_order:
        seeds = tuple(reversed(seeds))
    # 此对照只供应相关文档，避免测试源伪造跨文档 section 结果。
    stored = tuple(item.hydrated for item in chain) if mode != "section" else ()

    outcome = _expand(seeds, stored, mode)

    assert outcome.candidates == seeds
    assert outcome.degraded_reason_codes == ()


@pytest.mark.parametrize("mode", ("same_group", "table", "section"))
def test_mixed_channel_direct_candidate_keeps_each_native_contribution(
    mode: str,
) -> None:
    first, second, third = _chain(ChunkRole.TABLE)
    lexical = make_ranked_chunk(2, second.hydrated.chunk.text)
    mixed = second.model_copy(
        update={
            "contributions": (*second.contributions, *lexical.contributions)
        }
    )
    seeds = (first, mixed, third)

    outcome = _expand(
        seeds,
        tuple(item.hydrated for item in seeds),
        mode,
        section_chunk_limit=3,
    )

    assert outcome.candidates == seeds
    assert outcome.candidates[1] is mixed
