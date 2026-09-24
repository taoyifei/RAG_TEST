"""来源阅读组按坐标和预算整组选择的回归。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.context_packing import (
    ContextPackingResult,
    pack_context_groups,
)
from rag_app.application.retrieval.context_reader import (
    ContextReadGroup,
    ContextReadPiece,
    ContextReadResult,
)
from rag_app.core.models import RankedChunk
from rag_app.core.tokenization import estimate_provider_input_tokens
from tests.application.retrieval.helpers import make_ranked_chunk


def _piece(candidate: RankedChunk) -> ContextReadPiece:
    return ContextReadPiece(
        candidate=candidate,
        span=candidate.hydrated.chunk.source_spans[0],
    )


def _group(
    group_id: str,
    *pieces: ContextReadPiece,
    complete: bool = True,
) -> ContextReadGroup:
    node_ids = tuple(
        dict.fromkeys(
            piece.span.node_id
            for piece in pieces
            if piece.span.node_id is not None
        )
    )
    return ContextReadGroup(
        group_id=group_id,
        kind="source_node",
        seed_chunk_ids=(
            (pieces[0].candidate.hydrated.chunk.chunk_id,) if pieces else ()
        ),
        pieces=pieces,
        required_node_ids=node_ids,
        missing_node_ids=() if complete else ("missing-node",),
        source_complete=complete,
        reason_codes=() if complete else ("SOURCE_NODE_INCOMPLETE",),
    )


def _pack(
    *groups: ContextReadGroup,
    token_budget: int = 20,
    max_groups: int = 10,
    max_items: int = 10,
) -> ContextPackingResult:
    return pack_context_groups(
        ContextReadResult(groups),
        token_budget=token_budget,
        max_groups=max_groups,
        max_items=max_items,
    )


def test_token_boundary_keeps_every_piece_or_rejects_whole_group() -> None:
    group = _group(
        "row-1",
        _piece(make_ranked_chunk(1, "甲")),
        _piece(make_ranked_chunk(2, "乙")),
    )
    exact_cost = estimate_provider_input_tokens("甲\n乙")

    accepted = _pack(group, token_budget=exact_cost)
    rejected = _pack(group, token_budget=exact_cost - 1)

    assert accepted.selected == (group,)
    assert accepted.selected[0].pieces == group.pieces
    assert accepted.estimated_tokens == exact_cost
    assert rejected.selected == ()
    assert rejected.rejected == (("row-1", "GROUP_TOKEN_BUDGET"),)
    assert rejected.estimated_tokens == 0


def test_same_source_coordinate_counts_once_across_distinct_chunks() -> None:
    original = make_ranked_chunk(1, "甲")
    duplicate = make_ranked_chunk(2, "甲")
    duplicate_chunk = duplicate.hydrated.chunk.model_copy(
        update={"source_spans": original.hydrated.chunk.source_spans}
    )
    duplicate = duplicate.model_copy(
        update={
            "hydrated": duplicate.hydrated.model_copy(
                update={"chunk": duplicate_chunk}
            )
        }
    )
    distinct = make_ranked_chunk(3, "乙")
    first = _group("row-1", _piece(original))
    second = _group("row-2", _piece(duplicate), _piece(distinct))

    outcome = _pack(
        first,
        second,
        token_budget=estimate_provider_input_tokens("甲\n乙"),
        max_items=2,
    )

    assert outcome.selected == (first, second)
    assert outcome.rejected == ()
    assert outcome.estimated_tokens == estimate_provider_input_tokens("甲\n乙")


def test_equal_text_from_different_source_remains_a_separate_item() -> None:
    first = _group("first", _piece(make_ranked_chunk(1, "相同")))
    second = _group("second", _piece(make_ranked_chunk(2, "相同")))

    outcome = _pack(first, second, max_items=1)

    assert outcome.selected == (first,)
    assert outcome.rejected == (("second", "ITEM_LIMIT"),)


def test_complete_groups_sort_by_seed_rank_before_partial_groups() -> None:
    partial = _group(
        "partial",
        _piece(make_ranked_chunk(1, "一")),
        complete=False,
    )
    second = _group("second", _piece(make_ranked_chunk(3, "三")))
    first = _group("first", _piece(make_ranked_chunk(2, "二")))

    outcome = _pack(partial, second, first)

    assert outcome.selected == (first, second, partial)
    assert outcome.selected[-1].source_complete is False
    assert outcome.selected[-1].reason_codes == ("SOURCE_NODE_INCOMPLETE",)


def test_group_count_and_empty_source_have_explicit_reasons() -> None:
    first = _group("first", _piece(make_ranked_chunk(1, "甲")))
    second = _group("second", _piece(make_ranked_chunk(2, "乙")))
    empty = _group("empty", complete=False)

    outcome = _pack(second, empty, first, max_groups=1)

    assert outcome.selected == (first,)
    assert outcome.rejected == (
        ("second", "GROUP_LIMIT"),
        ("empty", "SOURCE_GROUP_EMPTY"),
    )


def test_conflicting_text_at_same_coordinate_rejects_the_later_group() -> None:
    original = make_ranked_chunk(1, "甲")
    conflicting = make_ranked_chunk(2, "乙")
    conflicting_chunk = conflicting.hydrated.chunk.model_copy(
        update={"source_spans": original.hydrated.chunk.source_spans}
    )
    conflicting = conflicting.model_copy(
        update={
            "hydrated": conflicting.hydrated.model_copy(
                update={"chunk": conflicting_chunk}
            )
        }
    )
    first = _group("first", _piece(original))
    second = _group("second", _piece(conflicting))

    outcome = _pack(first, second)

    assert outcome.selected == (first,)
    assert outcome.rejected == (("second", "SOURCE_COORDINATE_CONFLICT"),)


@pytest.mark.parametrize(
    ("token_budget", "max_groups", "max_items"),
    ((0, 1, 1), (1, 0, 1), (1, 1, 0), (True, 1, 1)),
)
def test_budget_limits_must_be_positive_integers(
    token_budget: int,
    max_groups: int,
    max_items: int,
) -> None:
    with pytest.raises(ValueError, match="来源组预算必须为正整数"):
        _pack(
            token_budget=token_budget,
            max_groups=max_groups,
            max_items=max_items,
        )
