"""按完整来源组选择阅读材料，并记录组级预算拒绝原因。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.application.retrieval.context_reader import (
    ContextReadGroup,
    ContextReadPiece,
    ContextReadResult,
)
from rag_app.core.tokenization import estimate_provider_input_tokens

_Coordinate = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class ContextPackingResult:
    """保持原组边界的选择结果及仅针对来源正文的预算估算。"""

    selected: tuple[ContextReadGroup, ...]
    rejected: tuple[tuple[str, str], ...]
    estimated_tokens: int


def pack_context_groups(
    result: ContextReadResult,
    *,
    token_budget: int,
    max_groups: int,
    max_items: int,
) -> ContextPackingResult:
    """按相关性稳定选择来源组，不裁剪组内片段。

    相同文档版本中的同一来源坐标只计一次；部分来源组若有片段，
    原样保留其不完整标记。调用方仍需检查最终发送包的完整输入预算。

    Args:
        result: 来源读取器返回的组，跳过的 seed 仍由原结果记录。
        token_budget: 来源正文估算的最大输入 Token 数。
        max_groups: 最多入选的来源组数。
        max_items: 最多入选的不同来源坐标数。

    Returns:
        整组入选、整组拒绝及已入选来源正文的估算 Token 数。

    Raises:
        ValueError: 任一预算不是正整数。

    """
    if any(
        type(limit) is not int or limit <= 0
        for limit in (token_budget, max_groups, max_items)
    ):
        raise ValueError("来源组预算必须为正整数。")

    selected: list[ContextReadGroup] = []
    rejected: list[tuple[str, str]] = []
    source_texts: dict[_Coordinate, str] = {}
    selected_group_ids: set[str] = set()
    estimated_tokens = 0

    for group in sorted(result.groups, key=_group_rank_key):
        if not group.pieces:
            rejected.append((group.group_id, "SOURCE_GROUP_EMPTY"))
            continue
        if group.group_id in selected_group_ids:
            rejected.append((group.group_id, "GROUP_DUPLICATE"))
            continue

        group_texts: dict[_Coordinate, str] = {}
        for piece in group.pieces:
            coordinate = _source_coordinate(piece)
            text = piece.text
            if (
                coordinate in group_texts and group_texts[coordinate] != text
            ) or (
                coordinate in source_texts and source_texts[coordinate] != text
            ):
                rejected.append((group.group_id, "SOURCE_COORDINATE_CONFLICT"))
                break
            group_texts.setdefault(coordinate, text)
        else:
            if len(selected) >= max_groups:
                rejected.append((group.group_id, "GROUP_LIMIT"))
                continue
            new_texts = {
                coordinate: text
                for coordinate, text in group_texts.items()
                if coordinate not in source_texts
            }
            if not new_texts:
                rejected.append((group.group_id, "GROUP_DUPLICATE"))
                continue
            if len(source_texts) + len(new_texts) > max_items:
                rejected.append((group.group_id, "ITEM_LIMIT"))
                continue
            candidate_tokens = _estimate_texts(
                (*source_texts.values(), *new_texts.values())
            )
            if candidate_tokens > token_budget:
                rejected.append((group.group_id, "GROUP_TOKEN_BUDGET"))
                continue
            selected.append(group)
            selected_group_ids.add(group.group_id)
            source_texts.update(new_texts)
            estimated_tokens = candidate_tokens

    return ContextPackingResult(
        selected=tuple(selected),
        rejected=tuple(rejected),
        estimated_tokens=estimated_tokens,
    )


def _group_rank_key(group: ContextReadGroup) -> tuple[bool, int, float, str]:
    """完整组优先；组内原始 seed 的名次决定来源组顺序。"""
    seeds = set(group.seed_chunk_ids)
    ranked = tuple(
        candidate
        for candidate in group.candidates
        if candidate.hydrated.chunk.chunk_id in seeds
    )
    candidates = ranked or group.candidates
    rank = min(
        (
            candidate.rerank_rank or candidate.fusion_rank
            for candidate in candidates
        ),
        default=2**31,
    )
    score = max(
        (
            candidate.rerank_score
            for candidate in candidates
            if candidate.rerank_score is not None
        ),
        default=float("-inf"),
    )
    return (not group.source_complete, rank, -score, group.group_id)


def _source_coordinate(piece: ContextReadPiece) -> _Coordinate:
    """以活动索引、文档版本和物理来源位置区分重复内容。"""
    chunk = piece.candidate.hydrated.chunk
    span = piece.span
    scope: _Coordinate = (
        chunk.project_id,
        chunk.knowledge_base_id,
        chunk.index_revision_id,
        chunk.version.document_id,
        chunk.version.document_version_id,
        chunk.version.content_sha256,
    )
    anchor = span.source_anchor
    if (
        anchor is None
        or span.node_id is None
        or span.source_start_char is None
        or span.source_end_char is None
    ):
        return (
            "chunk",
            *scope,
            chunk.chunk_id,
            span.chunk_start_char,
            span.chunk_end_char,
        )
    return (
        "source",
        *scope,
        span.node_id,
        anchor.part_uri,
        anchor.story_kind,
        anchor.structural_path,
        span.source_start_char,
        span.source_end_char,
    )


def _estimate_texts(texts: tuple[str, ...]) -> int:
    """按唯一坐标串接后的正文估算，不包含生成模板开销。"""
    return estimate_provider_input_tokens("\n".join(texts)) if texts else 0


__all__ = ["ContextPackingResult", "pack_context_groups"]
