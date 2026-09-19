"""已通过访问边界的候选身份观察，不包含原文、向量或秘密。"""

from __future__ import annotations

from collections.abc import Sequence

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import ChannelHit, FusedCandidate, RankedChunk

_MAX_OBSERVED_CANDIDATES = 800


def candidate_observation(
    candidates: Sequence[ChannelHit | FusedCandidate | RankedChunk],
    *,
    selected_ids: frozenset[str] | None = None,
    drop_reason: str = "STAGE_CAP",
    decisions: tuple[tuple[str, str], ...] = (),
) -> dict[str, object]:
    """输出完整有界集合、总数和摘要；超过上限时明确标记截断。

    Args:
        candidates: 已授权且按当前阶段排名排列的候选。
        selected_ids: 下一阶段保留的 ID；空值表示当前集合全部保留。
        drop_reason: 未提供单项原因时使用的裁剪原因。
        decisions: 单项身份对应的保留或裁剪原因。

    Returns:
        不含正文的身份、通道、原因、总数和集合摘要。

    """
    items: list[dict[str, object]] = []
    chunk_ids: list[str] = []
    by_reason = dict(decisions)
    for rank, candidate in enumerate(candidates, 1):
        if isinstance(candidate, RankedChunk):
            chunk_id = candidate.hydrated.chunk.chunk_id
            version = candidate.hydrated.chunk.version.document_version_id
        else:
            chunk_id = candidate.chunk_id
            version = candidate.document_version_id
        chunk_ids.append(chunk_id)
        if rank > _MAX_OBSERVED_CANDIDATES:
            continue
        selected = selected_ids is None or chunk_id in selected_ids
        items.append(
            {
                "chunk_id": chunk_id,
                "document_version_id": version,
                "rank": rank,
                "selected": selected,
                "reason_code": by_reason.get(
                    chunk_id, "KEPT" if selected else drop_reason
                ),
                "origins": tuple(
                    origin.model_dump(mode="json")
                    for origin in candidate.retrieval_origins
                ),
                "retention_reasons": (
                    ()
                    if isinstance(candidate, ChannelHit)
                    else candidate.retention_reasons
                ),
            }
        )
    return {
        "total": len(candidates),
        "truncated": len(candidates) > _MAX_OBSERVED_CANDIDATES,
        "candidate_chunk_ids": tuple(chunk_ids[:_MAX_OBSERVED_CANDIDATES]),
        "candidate_set_sha256": canonical_sha256(sorted(set(chunk_ids))),
        "candidates": tuple(items),
    }


__all__ = ["candidate_observation"]
