from __future__ import annotations

import pytest

from rag_app.application.retrieval.fusion import reciprocal_rank_fusion
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import ChannelHit

_REVISION = f"irev_{'1' * 32}"


def _hit(
    chunk_number: int,
    channel: str,
    rank: int,
    *,
    must_keep: bool = False,
    content: str = "a",
) -> ChannelHit:
    return ChannelHit(
        revision_id=_REVISION,
        chunk_id=f"chunk_{chunk_number:032x}",
        document_id=f"doc_{'2' * 32}",
        document_version_id=f"dver_{'3' * 32}",
        role="text",
        section_id="section",
        content_sha256=content * 64,
        channel=channel,
        rank=rank,
        raw_score=float(rank),
        must_keep=must_keep,
    )


def test_rrf_uses_rank_contributions_and_stable_ties() -> None:
    fused = reciprocal_rank_fusion(
        {
            "lexical": (_hit(1, "lexical:fts5", 1), _hit(2, "lexical:fts5", 2)),
            "dense": (_hit(2, "dense:primary", 1), _hit(1, "dense:primary", 2)),
        },
        expected_revision_id=_REVISION,
        k=60,
    )

    assert [item.chunk_id for item in fused] == [
        f"chunk_{1:032x}",
        f"chunk_{2:032x}",
    ]
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert len(fused[0].contributions) == 2


def test_rrf_deduplicates_a_channel_and_prefers_must_keep_on_tie() -> None:
    fused = reciprocal_rank_fusion(
        {
            "exact": (
                _hit(2, "exact", 1, must_keep=True),
                _hit(2, "exact", 2, must_keep=True),
            ),
            "lexical": (_hit(1, "lexical:fts5", 1),),
        },
        expected_revision_id=_REVISION,
    )

    assert fused[0].must_keep
    assert len(fused[0].contributions) == 1


def test_rrf_rejects_cross_channel_identity_drift() -> None:
    with pytest.raises(IndexCorrupt):
        reciprocal_rank_fusion(
            {
                "lexical": (_hit(1, "lexical:fts5", 1),),
                "dense": (_hit(1, "dense:primary", 1, content="b"),),
            },
            expected_revision_id=_REVISION,
        )
