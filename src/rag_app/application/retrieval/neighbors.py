"""Rerank 后的 same-group、table 与 section 有界扩展。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.application.retrieval.semantics import source_qualifier_matches
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChunkRole,
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
    SourceSpan,
)
from rag_app.core.models.chunk import Chunk
from rag_app.core.ports import EvidenceSourcePort


@dataclass(frozen=True, slots=True)
class ExpansionOutcome:
    """扩展候选和安全降级原因。"""

    candidates: tuple[RankedChunk, ...]
    degraded_reason_codes: tuple[str, ...] = ()


class NeighborExpander:
    """只在 canonical revision 内做 bounded 结构扩展。"""

    def __init__(self, source: EvidenceSourcePort) -> None:
        self._source = source

    def expand(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        mode: str,
        policy: RetrievalPolicy,
        *,
        source_qualifier: str | None = None,
    ) -> ExpansionOutcome:
        """验证双向链接并拒绝跨 document/section/group。

        Args:
            snapshot: 请求级 immutable Active Revision。
            candidates: 已重排或明确 bypass 的 canonical 候选。
            mode: none、same_group、table 或 section。
            policy: 邻居数量和 section 上限。
            source_qualifier: 原问中的可选来源限定，仅用于安排结构扩展优先级。

        Returns:
            扩展候选及可审计的安全降级原因。

        """
        if not candidates:
            return ExpansionOutcome(candidates)
        try:
            originals = _original_candidates(candidates)
        except IndexCorrupt:
            # 原命中自身冲突时不能把不一致对象交给后续去重器任选其一。
            return ExpansionOutcome((), ("NEIGHBOR_INDEX_CORRUPT",))
        candidates = tuple(originals.values())
        reasons: tuple[str, ...] = ()
        try:
            if mode == "none":
                expanded = candidates
            elif mode == "section" and _has_table_coordinates(candidates):
                table_outcome = self._expand_table_context(
                    snapshot,
                    candidates,
                    policy,
                    source_qualifier=source_qualifier,
                )
                expanded = table_outcome.candidates
                reasons = table_outcome.degraded_reason_codes
                # 文本和列表仍沿原章节语义扩展；表格不借章节首块充当表头。
                prose = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.hydrated.chunk.role is not ChunkRole.TABLE
                )
                if prose:
                    sections = self._expand_sections(snapshot, prose, policy)
                    known = {item.hydrated.chunk.chunk_id for item in expanded}
                    expanded = (
                        *expanded,
                        *(
                            item
                            for item in sections
                            if item.hydrated.chunk.chunk_id not in known
                        ),
                    )
            elif mode == "section":
                expanded = self._expand_sections(snapshot, candidates, policy)
            elif (
                mode in {"same_group", "table"}
                and policy.neighbor_count
                and _has_table_coordinates(candidates)
            ):
                table_outcome = self._expand_table_context(
                    snapshot,
                    candidates,
                    policy,
                    source_qualifier=source_qualifier,
                )
                expanded = table_outcome.candidates
                reasons = table_outcome.degraded_reason_codes
            else:
                expanded = self._expand_links(
                    snapshot, candidates, mode, policy
                )
            return ExpansionOutcome(expanded, reasons)
        except IndexCorrupt:
            return ExpansionOutcome(candidates, ("NEIGHBOR_INDEX_CORRUPT",))

    def close_structure(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
    ) -> ExpansionOutcome:
        """批量沿双向链接补齐列表和表格，限制数据库往返次数。

        Args:
            snapshot: 请求固定的活动索引版本。
            candidates: 一次重排及普通邻居扩展后的候选。
            policy: 单组成员和总候选预算。

        Returns:
            原候选及有界结构邻居；损坏链接只产生降级标记。

        """
        try:
            originals = _original_candidates(candidates)
        except IndexCorrupt:
            return ExpansionOutcome((), ("NEIGHBOR_INDEX_CORRUPT",))
        roles = {ChunkRole.LIST, ChunkRole.TABLE}
        seeds = tuple(
            candidate
            for candidate in originals.values()
            if candidate.hydrated.chunk.role in roles
        )[: policy.rerank_candidate_limit]
        if not seeds:
            return ExpansionOutcome(candidates)
        cap = policy.rerank_candidate_limit * policy.group_member_chunk_limit
        known = dict(originals)
        context: dict[str, RankedChunk] = {}
        frontier = seeds
        try:
            for _ in range(policy.group_member_chunk_limit):
                if len(known) >= cap or not frontier:
                    break
                neighbor_ids = tuple(
                    dict.fromkeys(
                        neighbor_id
                        for candidate in frontier
                        for neighbor_id in (
                            candidate.hydrated.chunk.previous_chunk_id,
                            candidate.hydrated.chunk.next_chunk_id,
                        )
                        if neighbor_id is not None and neighbor_id not in known
                    )
                )[: cap - len(known)]
                if not neighbor_ids:
                    break
                hydrated = _hydrated_candidates(
                    self._source.hydrate_chunks(snapshot, neighbor_ids),
                    originals,
                )
                next_frontier: list[RankedChunk] = []
                for candidate in frontier:
                    origin = candidate.hydrated.chunk
                    for neighbor_id in (
                        origin.previous_chunk_id,
                        origin.next_chunk_id,
                    ):
                        item = hydrated.get(neighbor_id or "")
                        if item is None or item.chunk.role is not origin.role:
                            continue
                        _validate_neighbor(origin, item.chunk)
                        if item.chunk.chunk_id in known:
                            continue
                        _add_context(
                            originals,
                            context,
                            item,
                            seed_id=origin.chunk_id,
                            reason="STRUCTURE_CONTINUITY",
                        )
                        expanded = context[item.chunk.chunk_id]
                        known[item.chunk.chunk_id] = expanded
                        next_frontier.append(expanded)
                frontier = tuple(next_frontier)
        except IndexCorrupt:
            return ExpansionOutcome(candidates, ("NEIGHBOR_INDEX_CORRUPT",))
        return ExpansionOutcome((*candidates, *context.values()))

    def close_table_context(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
        *,
        source_qualifier: str | None = None,
    ) -> ExpansionOutcome:
        """从已命中的规范表格坐标有界补齐目标行和原始表头。

        该闭合不依赖查询分析器选择的 ``neighbor_mode``。字段解析需要先
        看见真实 schema，因此只要召回结果已经给出表格坐标，就使用索引
        提供的规范关系单元读取器补齐同一物理行；读取器仍负责版本、表身份
        和成员数量上限，不能退化为整文档扫描。

        Args:
            snapshot: 请求固定的活动索引版本。
            candidates: 已通过排序和来源边界校验的候选。
            policy: 表格关系单元的成员及总候选预算。
            source_qualifier: 可选来源限定，仅用于多来源时的稳定优先级。

        Returns:
            原候选及规范表头、目标行成员；损坏或超预算时带原因码降级。

        """
        if not candidates or not _has_table_coordinates(candidates):
            return ExpansionOutcome(candidates)
        try:
            return self._expand_table_context(
                snapshot,
                candidates,
                policy,
                source_qualifier=source_qualifier,
            )
        except IndexCorrupt:
            return ExpansionOutcome(candidates, ("TABLE_INDEX_CORRUPT",))

    def close_source_nodes(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
    ) -> ExpansionOutcome:
        """补齐正文或表格单元格中跨 canonical Chunk 的同一原文节点。"""
        try:
            originals = _original_candidates(candidates)
            seeds = tuple(
                candidate
                for candidate in sorted(
                    originals.values(),
                    key=lambda item: item.rerank_rank or 2**31,
                )
                if candidate.rerank_rank is not None
                and candidate.hydrated.chunk.role
                in {ChunkRole.TEXT, ChunkRole.TABLE}
                and candidate.hydrated.chunk.next_chunk_id is not None
                and candidate.hydrated.chunk.next_chunk_id not in originals
            )[: policy.rerank_candidate_limit]
            if not seeds:
                return ExpansionOutcome(candidates)
            ids = tuple(
                dict.fromkeys(
                    identifier
                    for seed in seeds
                    if (identifier := seed.hydrated.chunk.next_chunk_id)
                    is not None
                )
            )
            hydrated = _hydrated_candidates(
                self._source.hydrate_chunks(snapshot, ids), originals
            )
            context: dict[str, RankedChunk] = {}
            for seed in seeds:
                origin = seed.hydrated.chunk
                neighbor = hydrated.get(origin.next_chunk_id or "")
                if neighbor is None or neighbor.chunk.role is not origin.role:
                    continue
                _validate_neighbor(origin, neighbor.chunk)
                origin_nodes = {
                    span.node_id
                    for span in origin.source_spans
                    if span.is_citable and not span.is_repeated and span.node_id
                }
                neighbor_nodes = {
                    span.node_id
                    for span in neighbor.chunk.source_spans
                    if span.is_citable and not span.is_repeated and span.node_id
                }
                if origin_nodes.isdisjoint(neighbor_nodes):
                    continue
                if origin.role is ChunkRole.TABLE and not any(
                    left.node_id == right.node_id
                    and left.source_end_char is not None
                    and left.source_end_char == right.source_start_char
                    and left.structural_path == right.structural_path
                    for left in origin.source_spans
                    for right in neighbor.chunk.source_spans
                    if left.is_citable
                    and right.is_citable
                    and not left.is_repeated
                    and not right.is_repeated
                ):
                    continue
                _add_context(
                    originals,
                    context,
                    neighbor,
                    seed_id=origin.chunk_id,
                    reason="SOURCE_NODE_CONTINUATION",
                )
            return ExpansionOutcome((*candidates, *context.values()))
        except IndexCorrupt:
            return ExpansionOutcome(candidates, ("SOURCE_NODE_INDEX_CORRUPT",))

    def _expand_table_context(  # noqa: PLR0912
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
        *,
        source_qualifier: str | None,
    ) -> ExpansionOutcome:
        """按真实表身份整单元补齐规范表头和目标行，预算不足不截半行。"""
        originals = _original_candidates(candidates)
        context: dict[str, RankedChunk] = {}
        reasons: list[str] = []
        limit = max(
            len(candidates),
            policy.fusion_candidate_limit,
            policy.max_evidence_items,
        )
        seeds = _prioritize_uniquely_qualified_source(
            candidates, source_qualifier
        )
        reader = getattr(self._source, "table_context_chunk_ids", None)
        visited: set[tuple[str, str, int]] = set()
        for seed in seeds:
            chunk = seed.hydrated.chunk
            if chunk.role is not ChunkRole.TABLE:
                continue
            rows = _table_row_keys(chunk)
            if reader is not None and rows:
                for table_node, row in sorted(rows):
                    key = (chunk.version.document_version_id, table_node, row)
                    if key in visited:
                        continue
                    visited.add(key)
                    identity = _table_source_identities(chunk, table_node)
                    if len(identity) != 1:
                        reasons.append("TABLE_IDENTITY_UNDETERMINED")
                        continue
                    ids = reader(
                        snapshot,
                        document_version=chunk.version,
                        table_node_id=table_node,
                        row_indices=(row,),
                        limit=min(200, policy.group_member_chunk_limit),
                    )
                    if ids is None:
                        reasons.append("TABLE_RELATION_UNIT_BUDGET_EXCEEDED")
                        continue
                    items = tuple(
                        _hydrated_candidates(
                            self._source.hydrate_chunks(snapshot, ids),
                            originals,
                        ).values()
                    )
                    for item in items:
                        _validate_boundary(
                            chunk, item.chunk, require_group=False
                        )
                        if _table_source_identities(
                            item.chunk, table_node
                        ) != identity or (
                            (table_node, row) not in _table_row_keys(item.chunk)
                            and (table_node, 0)
                            not in _table_row_keys(item.chunk)
                            and not _has_original_header(item.chunk, table_node)
                        ):
                            raise IndexCorrupt(
                                "目标表上下文的 part/story 或表身份不一致。",
                                stage="retrieval.neighbors",
                            )
                    if not any(
                        _has_original_header(item.chunk, table_node)
                        for item in items
                    ):
                        reasons.append("TABLE_HEADER_MISSING")
                    new_items = tuple(
                        item
                        for item in items
                        if item.chunk.chunk_id not in originals
                        and item.chunk.chunk_id not in context
                    )
                    if len(originals) + len(context) + len(new_items) > limit:
                        reasons.append("TABLE_RELATION_UNIT_BUDGET_EXCEEDED")
                        continue
                    for item in items:
                        _add_context(
                            originals,
                            context,
                            item,
                            seed_id=chunk.chunk_id,
                            reason="TABLE_CANONICAL_RELATION_UNIT",
                        )
                continue
            # 旧来源未提供规范 atom 映射时只保留真实双向邻居，不能猜表头。
            expanded: tuple[RankedChunk, ...] = (seed,)
            for _ in range(min(policy.max_evidence_items, 8)):
                additional = self._expand_links(
                    snapshot, expanded, "table", policy
                )
                if len(additional) == len(expanded):
                    break
                expanded = additional[: policy.max_evidence_items + 1]
            prioritized = _prioritize_same_table_row(seed, expanded)
            for candidate in prioritized:
                _add_context(
                    originals,
                    context,
                    candidate.hydrated,
                    seed_id=seed.hydrated.chunk.chunk_id,
                    reason="TABLE_CONTINUITY",
                )
                if len(originals) + len(context) >= limit:
                    break
            if len(originals) + len(context) >= limit:
                break
        return ExpansionOutcome(
            (*candidates, *context.values()), tuple(dict.fromkeys(reasons))
        )

    def _expand_links(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        mode: str,
        policy: RetrievalPolicy,
    ) -> tuple[RankedChunk, ...]:
        ids: list[str] = []
        originals = _original_candidates(candidates)
        if policy.neighbor_count == 0:
            return candidates
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            if mode == "table" and chunk.role.value != "table":
                continue
            ids.extend(
                item
                for item in (chunk.previous_chunk_id, chunk.next_chunk_id)
                if item is not None
            )
        hydrated = self._source.hydrate_chunks(
            snapshot, tuple(dict.fromkeys(ids))
        )
        by_id = _hydrated_candidates(hydrated, originals)
        by_id.update(
            {chunk_id: item.hydrated for chunk_id, item in originals.items()}
        )
        context: dict[str, RankedChunk] = {}
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            if mode == "table" and chunk.role.value != "table":
                continue
            for neighbor_id in (chunk.previous_chunk_id, chunk.next_chunk_id):
                if neighbor_id not in by_id:
                    continue
                hydrated_item = by_id[neighbor_id]
                _validate_neighbor(chunk, hydrated_item.chunk)
                _add_context(
                    originals,
                    context,
                    hydrated_item,
                    seed_id=chunk.chunk_id,
                    reason=(
                        "TABLE_CONTINUITY"
                        if mode == "table"
                        else "SAME_GROUP_NEIGHBOR"
                    ),
                )
        return (*candidates, *context.values())

    def _expand_sections(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
    ) -> tuple[RankedChunk, ...]:
        if policy.section_chunk_limit == 0:
            return candidates
        originals = _original_candidates(candidates)
        context: dict[str, RankedChunk] = {}
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            ids = self._source.section_chunk_ids(
                snapshot,
                document_version_id=chunk.version.document_version_id,
                section_id=chunk.section_id,
                limit=policy.section_search_limit,
            )
            hydrated = self._source.hydrate_chunks(snapshot, ids)
            section_items = tuple(
                _hydrated_candidates(hydrated, originals).values()
            )
            selected = section_items[: policy.section_chunk_limit]
            expansion_reason = "SECTION_SIBLING"
            if chunk.role is ChunkRole.LIST:
                origin_ordinal = _source_ordinal(chunk)
                if origin_ordinal is not None:
                    predecessors = tuple(
                        sorted(
                            (
                                item
                                for item in section_items
                                if item.chunk.role is ChunkRole.LIST
                                and item.chunk.neighbor_group_id
                                != chunk.neighbor_group_id
                                and (
                                    item_ordinal := _source_ordinal(item.chunk)
                                )
                                is not None
                                and 0
                                < origin_ordinal - item_ordinal
                                <= policy.section_predecessor_max_gap
                            ),
                            key=lambda item: (
                                -(_source_ordinal(item.chunk) or 0),
                                item.chunk.chunk_id,
                            ),
                        )
                    )
                    if predecessors:
                        selected = predecessors[: policy.section_chunk_limit]
                        expansion_reason = "SECTION_PREDECESSOR"
            for item in selected:
                _validate_boundary(chunk, item.chunk, require_group=False)
                _add_context(
                    originals,
                    context,
                    item,
                    seed_id=chunk.chunk_id,
                    reason=expansion_reason,
                )
        return (*candidates, *context.values())


def _source_ordinal(chunk: Chunk) -> int | None:
    """读取可引用来源节点的最早序号，忽略重复上下文。"""
    ordinals = (
        span.source_anchor.ordinal
        for span in chunk.source_spans
        if span.is_citable
        and not span.is_repeated
        and span.source_anchor is not None
    )
    return min(ordinals, default=None)


def _original_candidates(
    candidates: tuple[RankedChunk, ...],
) -> dict[str, RankedChunk]:
    """在任何扩展之前冻结原命中；冲突身份或评分不静默选取。"""
    originals: dict[str, RankedChunk] = {}
    for candidate in candidates:
        chunk_id = candidate.hydrated.chunk.chunk_id
        existing = originals.get(chunk_id)
        if existing is not None and existing != candidate:
            raise IndexCorrupt(
                "同 ID 的原始候选身份或排名不一致。",
                stage="retrieval.neighbors",
            )
        if existing is None:
            originals[chunk_id] = candidate
    return originals


def _has_table_coordinates(candidates: tuple[RankedChunk, ...]) -> bool:
    return any(
        any(part.startswith("tbl:") for part in span.structural_path)
        and any(part.startswith("tr:") for part in span.structural_path)
        for candidate in candidates
        if candidate.hydrated.chunk.role.value == "table"
        for span in candidate.hydrated.chunk.source_spans
    )


def _prioritize_same_table_row(
    seed: RankedChunk,
    candidates: tuple[RankedChunk, ...],
) -> tuple[RankedChunk, ...]:
    """把与种子共享 canonical table row 的分段稳定移到最前。"""
    target_rows = _table_row_keys(seed.hydrated.chunk)
    if not target_rows:
        return candidates
    same_row: list[RankedChunk] = []
    remaining: list[RankedChunk] = []
    for candidate in candidates:
        bucket = (
            same_row
            if target_rows & _table_row_keys(candidate.hydrated.chunk)
            else remaining
        )
        bucket.append(candidate)
    return (*same_row, *remaining)


def _table_row_keys(chunk: Chunk) -> frozenset[tuple[str, int]]:
    """读取 chunk atom 中不会被 repeated context 污染的表格行身份。"""
    atoms = dict(chunk.metadata).get("atoms", [])
    if not isinstance(atoms, list):
        return frozenset()
    rows: set[tuple[str, int]] = set()
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        metadata = atom.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        table_node_id = metadata.get("table_node_id")
        row_index = metadata.get("row_index")
        if (
            isinstance(table_node_id, str)
            and table_node_id
            and isinstance(row_index, int)
            and not isinstance(row_index, bool)
            and row_index >= 0
        ):
            rows.add((table_node_id, row_index))
    return frozenset(rows)


def _verified_vertical_inheritance(
    span: SourceSpan, atoms: object, table_node_id: str
) -> bool:
    """重复片段仅在规范映射指向上方原始行时可参加表闭合。"""
    anchor = span.source_anchor
    if anchor is None or type(anchor.row_index) is not int:
        return False
    if not isinstance(atoms, (list, tuple)):
        return False
    for atom in atoms:
        metadata = atom.get("metadata") if isinstance(atom, dict) else None
        if not isinstance(metadata, dict):
            continue
        logical_row = metadata.get("row_index")
        mapping = metadata.get("cell_source_node_ids")
        if (
            metadata.get("table_node_id") != table_node_id
            or type(logical_row) is not int
            or logical_row <= anchor.row_index
            or not isinstance(mapping, dict)
        ):
            continue
        if any(
            isinstance(values, (list, tuple)) and span.node_id in values
            for values in mapping.values()
        ):
            return True
    return False


def _table_source_identities(
    chunk: Chunk, table_node_id: str, *, header_only: bool = False
) -> frozenset[tuple[object, ...]]:
    """以 atom 的真实节点映射连接表身份与原始 part/story/path。"""
    atoms = dict(chunk.metadata).get("atoms", [])
    if not isinstance(atoms, (list, tuple)):
        return frozenset()
    nodes: set[str] = set()
    for atom in atoms:
        metadata = atom.get("metadata") if isinstance(atom, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("table_node_id") != table_node_id
        ):
            continue
        if header_only and metadata.get("header_strategy") != "tblHeader":
            continue
        mapping = metadata.get("cell_source_node_ids")
        if isinstance(mapping, dict):
            nodes.update(
                node
                for values in mapping.values()
                if isinstance(values, (list, tuple))
                for node in values
                if isinstance(node, str)
            )
    identities: set[tuple[object, ...]] = set()
    for span in chunk.source_spans:
        anchor = span.source_anchor
        if (
            not span.is_citable
            or (header_only and span.is_repeated)
            or span.node_id not in nodes
            or anchor is None
        ):
            continue
        if span.is_repeated and not _verified_vertical_inheritance(
            span, atoms, table_node_id
        ):
            continue
        table_positions = [
            index
            for index, part in enumerate(span.structural_path)
            if part.startswith("tbl:")
        ]
        if table_positions:
            identities.add(
                (
                    anchor.part_uri,
                    anchor.story_kind,
                    span.structural_path[: table_positions[-1] + 1],
                    table_node_id,
                )
            )
    return frozenset(identities)


def _has_original_header(chunk: Chunk, table_node_id: str) -> bool:
    return bool(
        _table_source_identities(chunk, table_node_id, header_only=True)
    )


def _prioritize_uniquely_qualified_source(
    candidates: tuple[RankedChunk, ...], source_qualifier: str | None
) -> tuple[RankedChunk, ...]:
    """让唯一来源限定的种子先使用表格闭合预算。

    限定未命中或同时命中多个文档版本时保持原次序，避免有限扩展窗口
    隐藏真实来源歧义。这里只调整上下文扩展顺序，不改变直接候选排名。
    """
    if source_qualifier is None:
        return candidates
    matching = tuple(
        candidate
        for candidate in candidates
        if source_qualifier_matches(
            candidate.hydrated.display_name,
            candidate.hydrated.chunk.heading_path,
            source_qualifier,
        )
    )
    document_versions = {
        candidate.hydrated.chunk.version.document_version_id
        for candidate in matching
    }
    if len(document_versions) != 1:
        return candidates
    matching_ids = {candidate.hydrated.chunk.chunk_id for candidate in matching}
    return (
        *matching,
        *(
            candidate
            for candidate in candidates
            if candidate.hydrated.chunk.chunk_id not in matching_ids
        ),
    )


def _hydrated_candidates(
    hydrated: tuple[HydratedChunk, ...],
    originals: dict[str, RankedChunk],
) -> dict[str, HydratedChunk]:
    result: dict[str, HydratedChunk] = {}
    for item in hydrated:
        chunk_id = item.chunk.chunk_id
        existing = result.get(chunk_id)
        original = originals.get(chunk_id)
        if (existing is not None and existing.chunk != item.chunk) or (
            original is not None and original.hydrated.chunk != item.chunk
        ):
            raise IndexCorrupt(
                "Hydration 返回同 ID 的不一致 canonical Chunk。",
                stage="retrieval.neighbors",
            )
        result[chunk_id] = item
    return result


def _add_context(
    originals: dict[str, RankedChunk],
    context: dict[str, RankedChunk],
    item: HydratedChunk,
    *,
    seed_id: str,
    reason: str,
) -> None:
    """原命中优先；纯上下文只记录扩展来源，不继承召回身份。"""
    chunk_id = item.chunk.chunk_id
    if chunk_id in originals:
        return
    existing = context.get(chunk_id)
    if existing is not None:
        if existing.hydrated.chunk != item.chunk:
            raise IndexCorrupt(
                "不同种子扩展出的同 ID canonical Chunk 不一致。",
                stage="retrieval.neighbors",
            )
        context[chunk_id] = existing.model_copy(
            update={
                "expansion_seed_ids": tuple(
                    sorted({*existing.expansion_seed_ids, seed_id})
                )
            }
        )
        return
    # 兼容必填正整数：纯上下文序号位于全部融合名次之后，非种子分数。
    context[chunk_id] = RankedChunk(
        hydrated=item,
        fusion_rank=max(seed.fusion_rank for seed in originals.values())
        + len(context)
        + 1,
        expansion_reason=reason,
        expansion_seed_ids=(seed_id,),
    )


def _validate_neighbor(origin_chunk: Chunk, neighbor_chunk: Chunk) -> None:
    _validate_boundary(origin_chunk, neighbor_chunk)
    linked = (
        origin_chunk.previous_chunk_id == neighbor_chunk.chunk_id
        and neighbor_chunk.next_chunk_id == origin_chunk.chunk_id
    ) or (
        origin_chunk.next_chunk_id == neighbor_chunk.chunk_id
        and neighbor_chunk.previous_chunk_id == origin_chunk.chunk_id
    )
    if not linked:
        raise IndexCorrupt(
            "Neighbor 双向链接不一致。", stage="retrieval.neighbors"
        )


def _validate_boundary(
    origin_chunk: Chunk, neighbor_chunk: Chunk, *, require_group: bool = True
) -> None:
    if (
        neighbor_chunk.version != origin_chunk.version
        or neighbor_chunk.project_id != origin_chunk.project_id
        or neighbor_chunk.knowledge_base_id != origin_chunk.knowledge_base_id
        or neighbor_chunk.index_revision_id != origin_chunk.index_revision_id
        or neighbor_chunk.section_id != origin_chunk.section_id
        or (
            require_group
            and neighbor_chunk.neighbor_group_id
            != origin_chunk.neighbor_group_id
        )
    ):
        raise IndexCorrupt(
            "Neighbor 跨越 canonical 结构边界。",
            stage="retrieval.neighbors",
        )


__all__ = ["ExpansionOutcome", "NeighborExpander"]
