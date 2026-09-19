"""对已授权、有界候选分配保留名额，不读取评测身份或正文。"""

from __future__ import annotations

from collections.abc import Mapping

from rag_app.core.models import ChannelHit, FusedCandidate

STRUCTURAL_SEED_LIMIT = 6


def structural_seed_ids(
    channels: Mapping[str, tuple[ChannelHit, ...]],
    *,
    limit: int = STRUCTURAL_SEED_LIMIT,
) -> tuple[str, ...]:
    """按真实结构命中和版本多样性选有限锚点，组成员由后续闭合。

    Args:
        channels: 已授权的有界通道候选。
        limit: 结构种子可使用的最大名额。

    Returns:
        优先覆盖不同来源并保持确定顺序的种子 ID。

    """
    allowed = {
        "STRUCTURAL_STAGE_HEADING",
        "STRUCTURAL_GLOSSARY_ROW",
        "STRUCTURAL_TABLE_ROW",
        "STRUCTURAL_SECTION_HEADING_BODY",
        "STRUCTURAL_CONTIGUOUS_LIST",
    }
    best_hits: dict[str, ChannelHit] = {}
    for name, items in channels.items():
        if not name.startswith("structural"):
            continue
        for hit in items:
            previous = best_hits.get(hit.chunk_id)
            if hit.match_type in allowed and (
                previous is None
                or (hit.rank, hit.channel) < (previous.rank, previous.channel)
            ):
                best_hits[hit.chunk_id] = hit
    hits = sorted(
        best_hits.values(),
        key=lambda hit: (hit.rank, hit.chunk_id),
    )
    diverse: list[ChannelHit] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        key = (hit.document_version_id, hit.section_id)
        if key not in seen:
            seen.add(key)
            diverse.append(hit)
    return tuple(dict.fromkeys(hit.chunk_id for hit in (*diverse, *hits)))[
        :limit
    ]


def family_seed_ids(candidates: tuple[FusedCandidate, ...]) -> tuple[str, ...]:
    """每个真实逻辑通道只保留一个初始名额，不把变体重复投票。

    Args:
        candidates: 按融合排名排列且保留原始通道来源的候选。

    Returns:
        共同覆盖各逻辑通道的最小首位种子集合。

    """
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        families = {
            item.logical_channel for item in candidate.retrieval_origins
        }
        if families - seen:
            result.append(candidate.chunk_id)
            seen.update(families)
    return tuple(result)


def retain_before_limit(
    candidates: tuple[FusedCandidate, ...],
    required_ids: tuple[str, ...],
    *,
    limit: int,
) -> tuple[FusedCandidate, ...]:
    """先分配有限保留名额，再按原分数次序裁剪，不改写 RRF 分数。

    Args:
        candidates: 按融合排名排列的完整有界候选。
        required_ids: 已按保护优先级排列的有限种子。
        limit: 与普通候选共享的总容量。

    Returns:
        在总容量内保留种子、顺序仍遵循原排名的候选。

    """
    by_id = {item.chunk_id: item for item in candidates}
    selected = tuple(key for key in dict.fromkeys(required_ids) if key in by_id)
    selected_ids = set(selected[:limit])
    for candidate in candidates:
        if len(selected_ids) >= limit:
            break
        selected_ids.add(candidate.chunk_id)
    return tuple(item for item in candidates if item.chunk_id in selected_ids)


__all__ = [
    "STRUCTURAL_SEED_LIMIT",
    "family_seed_ids",
    "retain_before_limit",
    "structural_seed_ids",
]
