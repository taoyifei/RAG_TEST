"""独立 circuit、严格响应校验和稳定 bypass 的 P07 reranking。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from itertools import zip_longest

from rag_app.application.embedding_router import failure_category
from rag_app.application.provider_health import (
    CircuitKey,
    EgressGuard,
    ProviderCircuitBreaker,
)
from rag_app.application.retrieval.contextual_text import contextual_rerank_text
from rag_app.application.retrieval.retention import STRUCTURAL_SEED_LIMIT
from rag_app.core.errors import PolicyDenied, ProviderInvalidResponse, RagError
from rag_app.core.models import (
    ProviderCall,
    ProviderFailureCategory,
    RankedChunk,
    RerankItem,
    RerankRequest,
    RetrievalPolicy,
)
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import RerankerPort


@dataclass(frozen=True, slots=True)
class RerankingOutcome:
    """重排或显式旁路后的候选与实际模式。"""

    candidates: tuple[RankedChunk, ...]
    mode: str
    reason_code: str
    provider_calls: tuple[ProviderCall, ...] = ()
    failure_category: ProviderFailureCategory | None = None
    input_candidates: tuple[RankedChunk, ...] = ()
    preselection_chunk_ids: tuple[str, ...] = ()
    retention_decisions: tuple[tuple[str, str], ...] = ()
    rerank_windows: tuple[tuple[str, int, int], ...] = ()


class CircuitAwareReranker:
    """Jina rerank circuit 与 embedding circuit 完全独立。"""

    def __init__(
        self,
        reranker: RerankerPort,
        *,
        circuit: ProviderCircuitBreaker | None = None,
    ) -> None:
        self._reranker = reranker
        self._circuit = circuit or ProviderCircuitBreaker()

    def rerank(  # noqa: PLR0913
        self,
        query: str,
        candidates: tuple[RankedChunk, ...],
        egress: EgressPolicy,
        policy: RetrievalPolicy,
        *,
        enabled: bool,
        result_limit: int,
        required_candidate_ids: frozenset[str] = frozenset(),
        natural_view: bool = False,
    ) -> RerankingOutcome:
        """重排 bounded fusion prefix 或稳定保留 RRF 顺序。

        Args:
            query: normalized query。
            candidates: RRF 顺序的 canonical candidates。
            egress: 默认拒绝的 rerank 出网策略。
            policy: rerank 数量、文本和 bypass 策略。
            enabled: Planner 是否要求 rerank。
            result_limit: 用户请求的最终候选数。
            required_candidate_ids: 必须保留到结构证据闭合的候选 ID。
            natural_view: 是否使用候选自然链的独立排序阅读视图。

        Returns:
            实际 Provider 或明确 bypass 后的候选与模式。

        """
        rrf_limited, preliminary, decisions = _select_input(
            candidates,
            policy=policy,
            required_candidate_ids=required_candidate_ids,
        )
        output_limit = min(result_limit, len(rrf_limited))

        def observed(
            outcome: RerankingOutcome, *, sent: bool = False
        ) -> RerankingOutcome:
            """把同一次选择的真实输入观察绑定到重排结果。

            Args:
                outcome: 本次 Provider 或受控旁路结果。
                sent: 是否实际调用了重排端口。

            Returns:
                带真实输入集合及选择原因的不可变结果。

            """
            return replace(
                outcome,
                input_candidates=rrf_limited if sent else (),
                preselection_chunk_ids=preliminary,
                retention_decisions=decisions,
            )

        if not enabled or not rrf_limited:
            return observed(
                _bypass(
                    _restore_protected(
                        rrf_limited[:output_limit],
                        rrf_limited,
                        limit=output_limit,
                        must_keep_limit=policy.must_keep_limit,
                        required_candidate_ids=required_candidate_ids,
                    ),
                    "RERANK_DISABLED_BY_PLAN",
                )
            )
        descriptor = self._reranker.descriptor
        key = CircuitKey(descriptor.name, "reranking", descriptor.version)
        if descriptor.capabilities.permits_network:
            try:
                EgressGuard.require_reranking(egress, descriptor.name)
            except PolicyDenied:
                if not policy.bypass_policy_denied:
                    raise
                return observed(
                    _bypass(
                        _restore_protected(
                            rrf_limited[:output_limit],
                            rrf_limited,
                            limit=output_limit,
                            must_keep_limit=policy.must_keep_limit,
                            required_candidate_ids=required_candidate_ids,
                        ),
                        "RERANK_BYPASSED_POLICY_DENIED",
                    )
                )
        if not self._circuit.allow_call(key):
            return observed(
                _bypass(
                    _restore_protected(
                        rrf_limited[:output_limit],
                        rrf_limited,
                        limit=output_limit,
                        must_keep_limit=policy.must_keep_limit,
                        required_candidate_ids=required_candidate_ids,
                    ),
                    "RERANK_BYPASSED_CIRCUIT_OPEN",
                    category=_circuit_failure_category(
                        self._circuit.snapshot(key).reason_code
                    ),
                )
            )
        limited = rrf_limited
        views = tuple(
            (
                item.hydrated.chunk.chunk_id,
                _natural_bounded_text(
                    item, query, policy.rerank_text_char_limit
                )
                if natural_view
                else (
                    _bounded_text(
                        item,
                        policy.rerank_text_char_limit,
                        contextual=policy.contextual_rerank_mode == "active",
                    ),
                    (0, 0),
                ),
            )
            for item in limited
        )
        request = RerankRequest(
            query=query,
            candidates=tuple((chunk_id, view[0]) for chunk_id, view in views),
            # 取回实际发送集合的全部分数，输出保护只使用真实回包的分数。
            limit=len(limited),
        )
        try:
            result = self._reranker.rerank(request)
            ordered = _validate_and_order(result.items, limited, len(limited))
        except (RagError, ValueError) as error:
            category = (
                failure_category(error)
                if isinstance(error, RagError)
                else ProviderFailureCategory.RESPONSE_CONTRACT
            )
            self._circuit.record_failure(key, category)
            return observed(
                _bypass(
                    _restore_protected(
                        rrf_limited[:output_limit],
                        rrf_limited,
                        limit=output_limit,
                        must_keep_limit=policy.must_keep_limit,
                        required_candidate_ids=required_candidate_ids,
                    ),
                    "RERANK_BYPASSED_PROVIDER_UNAVAILABLE",
                    category=category,
                ),
                sent=True,
            )
        self._circuit.record_success(key)
        protected = _restore_protected(
            ordered,
            ordered,
            limit=output_limit,
            must_keep_limit=policy.must_keep_limit,
            required_candidate_ids=required_candidate_ids,
        )
        return observed(
            RerankingOutcome(
                candidates=tuple(
                    item.model_copy(update={"rerank_rank": rank})
                    for rank, item in enumerate(protected, start=1)
                ),
                mode=result.mode.value,
                reason_code="RERANK_EXECUTED",
                provider_calls=result.calls,
                rerank_windows=tuple(
                    (chunk_id, *view[1]) for chunk_id, view in views
                )
                if natural_view
                else (),
            ),
            sent=True,
        )


def _bounded_text(
    candidate: RankedChunk, limit: int, *, contextual: bool = False
) -> str:
    chunk = candidate.hydrated.chunk
    if contextual:
        value = contextual_rerank_text(candidate).rerank_text
    else:
        display_name = candidate.hydrated.display_name
        heading = " / ".join(chunk.heading_path)
        labels = "\n".join(value for value in (display_name, heading) if value)
        value = f"{labels}\n{chunk.citation_text}"
    if len(value) <= limit:
        return value
    head = int(limit * 0.7)
    return f"{value[:head]}\n[…]\n{value[-(limit - head - 5) :]}"


def _natural_bounded_text(
    candidate: RankedChunk, query: str, limit: int
) -> tuple[str, tuple[int, int]]:
    """保留可信标题，并在长文本中选择包含问句词项的连续正文。"""
    value = contextual_rerank_text(candidate).rerank_text
    if len(value) <= limit:
        return value, (0, len(value))
    prefix = value[: min(160, limit // 4)]
    room = max(1, limit - len(prefix) - 6)
    terms = re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9_./:-]{2,}", query)
    probes = tuple(
        term[index : index + 4]
        for term in terms
        for index in range(max(1, len(term) - 3))
    )
    positions = (
        (len(probe), value.find(probe))
        for probe in (*terms, *probes)
        if probe and value.find(probe) >= 0
    )
    best = max(positions, default=(0, len(value) // 2))
    start = min(max(0, best[1] - room // 3), len(value) - room)
    end = min(len(value), start + room)
    if start <= len(prefix):
        return value[:limit], (0, limit)
    return f"{prefix}\n[…]\n{value[start:end]}", (start, end)


def _diversified_candidates(
    candidates: tuple[RankedChunk, ...], *, limit: int
) -> tuple[RankedChunk, ...]:
    """按逻辑通道轮询形成重排池，避免重复通道挤掉语义候选。

    Args:
        candidates: 已按 RRF 排序的完整有界候选。
        limit: 发送给重排器的最大候选数。

    Returns:
        保留各召回家族覆盖且家族内维持 RRF 顺序的候选。

    """
    if len(candidates) <= limit:
        return candidates
    by_family: dict[str, list[RankedChunk]] = {}
    for candidate in candidates:
        candidate_families = {
            _channel_family(channel) for channel in candidate.retrieval_channels
        }
        for family in candidate_families:
            by_family.setdefault(family, []).append(candidate)
    if len(by_family) <= 1:
        return _source_diverse_candidates(candidates)[:limit]

    selected: list[RankedChunk] = []
    selected_ids: set[str] = set()
    families = tuple(sorted(by_family))
    for row in zip_longest(
        *(
            _source_diverse_candidates(tuple(by_family[family]))
            for family in families
        ),
        fillvalue=None,
    ):
        for row_candidate in row:
            if row_candidate is None:
                continue
            chunk_id = row_candidate.hydrated.chunk.chunk_id
            if chunk_id in selected_ids:
                continue
            selected.append(row_candidate)
            selected_ids.add(chunk_id)
            if len(selected) == limit:
                return tuple(selected)
    for candidate in candidates:
        if len(selected) == limit:
            break
        chunk_id = candidate.hydrated.chunk.chunk_id
        if chunk_id not in selected_ids:
            selected.append(candidate)
            selected_ids.add(chunk_id)
    return tuple(selected)


def _source_diverse_candidates(
    candidates: tuple[RankedChunk, ...],
) -> tuple[RankedChunk, ...]:
    """稀有文档和真实表锚点各先得到一个名额，再按 RRF 填充。"""
    first_documents: list[RankedChunk] = []
    first_tables: list[RankedChunk] = []
    rest: list[RankedChunk] = []
    seen: set[tuple[object, ...]] = set()
    seen_documents: set[str] = set()
    for candidate in candidates:
        chunk = candidate.hydrated.chunk
        anchor = next(
            (
                span.source_anchor
                for span in chunk.source_spans
                if span.source_anchor is not None
            ),
            None,
        )
        table_identity: tuple[object, ...] = ()
        if chunk.role.value == "table" and anchor is not None:
            table_path = next(
                (
                    anchor.structural_path[: index + 1]
                    for index in reversed(range(len(anchor.structural_path)))
                    if anchor.structural_path[index].startswith("tbl:")
                    and anchor.structural_path[index][4:].isdigit()
                ),
                (),
            )
            coordinate: tuple[object, ...] = table_path
            if not coordinate and anchor.pdf_table_id:
                coordinate = (anchor.pdf_table_id,)
            if not coordinate and type(anchor.table_index) is int:
                coordinate = (anchor.table_index,)
            if coordinate:
                table_identity = (
                    anchor.part_uri,
                    anchor.story_kind,
                    coordinate,
                )
        identity = (chunk.version.document_version_id, *table_identity)
        if chunk.version.document_version_id not in seen_documents:
            first_documents.append(candidate)
            seen_documents.add(chunk.version.document_version_id)
            seen.add(identity)
        elif identity in seen:
            rest.append(candidate)
        else:
            first_tables.append(candidate)
            seen.add(identity)
    return (*first_documents, *first_tables, *rest)


def _select_input(
    candidates: tuple[RankedChunk, ...],
    *,
    policy: RetrievalPolicy,
    required_candidate_ids: frozenset[str],
) -> tuple[
    tuple[RankedChunk, ...], tuple[str, ...], tuple[tuple[str, str], ...]
]:
    """共享上限内先分配通道、exact、结构与各 Unit 名额，再填充排名。"""
    limit = policy.rerank_candidate_limit
    preliminary = _diversified_candidates(candidates, limit=limit)
    reasons: dict[str, str] = {}
    families: set[str] = set()
    units: dict[str, list[RankedChunk]] = {}
    structural: list[RankedChunk] = []
    for candidate in candidates:
        chunk_id = candidate.hydrated.chunk.chunk_id
        current_families = {
            _channel_family(channel) for channel in candidate.retrieval_channels
        }
        if current_families - families:
            reasons.setdefault(chunk_id, "LOGICAL_CHANNEL_QUOTA")
            families.update(current_families)
        unit_reasons = tuple(
            reason
            for reason in candidate.retention_reasons
            if reason.startswith("UNIT_SEED:")
        )
        for reason in unit_reasons:
            units.setdefault(reason, []).append(candidate)
        if "STRUCTURAL_SEED" in candidate.retention_reasons or (
            chunk_id in required_candidate_ids and not unit_reasons
        ):
            structural.append(candidate)
    for candidate in tuple(item for item in candidates if item.must_keep)[
        : policy.must_keep_limit
    ]:
        reasons.setdefault(candidate.hydrated.chunk.chunk_id, "EXACT_QUOTA")
    for candidate in structural[:STRUCTURAL_SEED_LIMIT]:
        reasons.setdefault(
            candidate.hydrated.chunk.chunk_id, "STRUCTURAL_QUOTA"
        )
    unit_names = sorted(
        units, key=lambda name: (name != "UNIT_SEED:ROOT", name)
    )
    for row in zip_longest(
        *(
            units[name][
                : policy.unit_root_seed_limit
                if name == "UNIT_SEED:ROOT"
                else policy.unit_atom_seed_limit
            ]
            for name in unit_names
        ),
        fillvalue=None,
    ):
        for unit_candidate in row:
            if unit_candidate is not None:
                reasons.setdefault(
                    unit_candidate.hydrated.chunk.chunk_id, "QUERY_UNIT_QUOTA"
                )
    protected = frozenset(tuple(reasons)[:limit])
    selected = _restore_protected(
        preliminary,
        candidates,
        limit=len(preliminary),
        must_keep_limit=0,
        required_candidate_ids=protected,
    )
    selected_ids = {item.hydrated.chunk.chunk_id for item in selected}
    decisions = tuple(
        (
            item.hydrated.chunk.chunk_id,
            reasons.get(item.hydrated.chunk.chunk_id, "DIVERSIFIED_RRF_FILL")
            if item.hydrated.chunk.chunk_id in selected_ids
            else "RETENTION_QUOTA_EXHAUSTED"
            if item.hydrated.chunk.chunk_id in reasons or item.retention_reasons
            else "RERANK_INPUT_CAP",
        )
        for item in candidates
    )
    return (
        selected,
        tuple(item.hydrated.chunk.chunk_id for item in preliminary),
        decisions,
    )


def _channel_family(channel: str) -> str:
    """把原问、改写和 Provider 槽归并为同一逻辑召回家族。"""
    for family in ("lexical", "structural", "dense"):
        if channel.startswith(f"{family}:"):
            return family
    return channel


def _validate_and_order(
    items: tuple[RerankItem, ...],
    candidates: tuple[RankedChunk, ...],
    limit: int,
) -> tuple[RankedChunk, ...]:
    expected_ids = {
        item.hydrated.chunk.chunk_id: index
        for index, item in enumerate(candidates)
    }
    if len(items) != limit:
        raise ProviderInvalidResponse(
            "Reranker 返回候选数量不完整。", stage="retrieval.rerank"
        )
    candidate_ids = [item.candidate_id for item in items]
    if len(set(candidate_ids)) != len(candidate_ids) or any(
        item not in expected_ids for item in candidate_ids
    ):
        raise ProviderInvalidResponse(
            "Reranker 返回重复或越界候选。", stage="retrieval.rerank"
        )
    scores = {str(item.candidate_id): float(item.score) for item in items}
    if any(not math.isfinite(score) for score in scores.values()):
        raise ProviderInvalidResponse(
            "Reranker 返回非有限分数。", stage="retrieval.rerank"
        )
    selected = [
        candidate
        for candidate in candidates
        if candidate.hydrated.chunk.chunk_id in scores
    ]
    selected.sort(
        key=lambda candidate: (
            -scores[candidate.hydrated.chunk.chunk_id],
            expected_ids[candidate.hydrated.chunk.chunk_id],
        )
    )
    return tuple(
        candidate.model_copy(
            update={"rerank_score": scores[candidate.hydrated.chunk.chunk_id]}
        )
        for candidate in selected
    )


def _restore_protected(
    selected: tuple[RankedChunk, ...],
    all_candidates: tuple[RankedChunk, ...],
    *,
    limit: int,
    must_keep_limit: int,
    required_candidate_ids: frozenset[str],
) -> tuple[RankedChunk, ...]:
    """先恢复结构闭合成员，再恢复既有 exact must-keep。"""
    result = list(selected[:limit])
    present = {item.hydrated.chunk.chunk_id for item in result}
    required = [
        item
        for item in all_candidates
        if item.hydrated.chunk.chunk_id in required_candidate_ids
    ][:limit]
    required_ids = {item.hydrated.chunk.chunk_id for item in required}
    protected = [
        *required,
        *[
            item
            for item in all_candidates
            if item.must_keep
            and item.hydrated.chunk.chunk_id not in required_ids
        ][:must_keep_limit],
    ]
    protected_ids = {item.hydrated.chunk.chunk_id for item in protected}
    for candidate in protected:
        if candidate.hydrated.chunk.chunk_id in present:
            continue
        replace = next(
            (
                index
                for index in range(len(result) - 1, -1, -1)
                if result[index].hydrated.chunk.chunk_id not in protected_ids
            ),
            None,
        )
        if replace is None:
            break
        present.discard(result[replace].hydrated.chunk.chunk_id)
        result[replace] = candidate
        present.add(candidate.hydrated.chunk.chunk_id)
    return tuple(result)


def _bypass(
    candidates: tuple[RankedChunk, ...],
    reason_code: str,
    *,
    category: ProviderFailureCategory | None = None,
) -> RerankingOutcome:
    return RerankingOutcome(
        candidates=candidates,
        mode=reason_code.casefold(),
        reason_code=reason_code,
        provider_calls=(),
        failure_category=category,
    )


def _circuit_failure_category(
    reason_code: str,
) -> ProviderFailureCategory | None:
    try:
        return ProviderFailureCategory(reason_code.casefold())
    except ValueError:
        return None


__all__ = [
    "CircuitAwareReranker",
    "RerankingOutcome",
]
