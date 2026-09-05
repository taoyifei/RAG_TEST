"""Evaluation V3 的实际检索分阶段观测；不从 Evidence 反推候选。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from evaluation.v2.models import (
    CaseObservation,
    DatasetDocument,
    EvaluationCase,
)
from rag_app.application.answering.validation import validate_extractive_draft
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    AnswerDraft,
    EvidenceItem,
    ProviderCallCount,
    SearchAnswerResult,
)
from rag_app.core.models.chunk import Chunk, SourceSpanKind


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """实际 active inventory 与运行身份，来源于索引和当前查询。"""

    chunks: dict[str, Chunk]
    documents: tuple[DatasetDocument, ...]
    expected_revision: str
    expected_vectors: tuple[tuple[str, str], ...]
    variant_id: str
    lane: str
    evidence_token_budget: int


def observe_case_result(
    case: EvaluationCase,
    result: SearchAnswerResult,
    context: ObservationContext,
    *,
    latency_ms: float,
) -> CaseObservation:
    """将实际响应与独立标签转换为 V3 观测。

    Args:
        case: 查询前固定的独立标签与 SourceSpan 范围。
        result: 本次实际调用结果，必须带 RetrievalDiagnostics。
        context: 查询前读取的 active inventory 和模型槽身份。
        latency_ms: 本次查询实际耗时。

    Returns:
        分别记录 fusion、rerank、Evidence 与引用范围的观测。

    Raises:
        ValueError: 响应缺失实际检索诊断。

    """
    diagnostics = result.diagnostics
    if diagnostics is None:
        raise ValueError("Evaluation V3 需要完整 RetrievalDiagnostics。")
    evidence = result.evidence
    evidence_by_chunk: dict[str, EvidenceItem] = {}
    for item in evidence:
        evidence_by_chunk.setdefault(item.chunk_id, item)
    ranked_evidence = tuple(item.chunk_id for item in diagnostics.reranked)
    retrieved_documents = tuple(
        context.chunks[identifier].version.document_id
        for identifier in ranked_evidence
        if identifier in context.chunks
    )
    retrieved_chunks = ranked_evidence
    cited_ids, citation_valid, unsupported = _validate_answer(result)
    cited = tuple(item for item in evidence if item.evidence_id in cited_ids)
    predicted_ranges = tuple(
        (
            item.document_id,
            span.node_id,
            span.source_start_char,
            span.source_end_char,
        )
        for item in evidence
        for span in item.source_spans
        if item.document_id is not None
        and span.node_id is not None
        and span.source_start_char is not None
        and span.source_end_char is not None
    )
    expected_ranges = tuple(
        (
            item.document_id,
            item.node_id,
            item.source_start_char,
            item.source_end_char,
        )
        for item in case.expected.required_source_ranges
    )
    matched = sum(
        any(
            _range_covers(predicted, expected) for predicted in predicted_ranges
        )
        for expected in expected_ranges
    )
    relevant_predictions = sum(
        any(_range_covers(predicted, expected) for expected in expected_ranges)
        for predicted in predicted_ranges
    )
    scope_by_document = {
        item.document_id: (item.project_id, item.knowledge_base_id)
        for item in context.documents
    }
    wrong_scope = sum(
        scope_by_document.get(identifier)
        != (case.project_id, case.knowledge_base_id)
        for identifier in retrieved_documents
    )
    expected_revision = context.expected_revision
    wrong_revision = sum(
        context.chunks[identifier].index_revision_id != expected_revision
        for identifier in retrieved_chunks
        if identifier in context.chunks
    ) + sum(identifier not in context.chunks for identifier in retrieved_chunks)
    expected_vectors = dict(context.expected_vectors)
    wrong_vector = int(
        result.selected_embedding_slot is not None
        and expected_vectors.get(result.selected_embedding_slot)
        != result.selected_vector_name
    )
    call_counts = {item.operation: item for item in diagnostics.provider_calls}
    embedding_calls, embedding_retries = _operation_counts(call_counts, "embed")
    reranker_calls, reranker_retries = _operation_counts(call_counts, "rerank")
    provider_calls = sum(item.call_count for item in call_counts.values())
    provider_retries = sum(item.retry_count for item in call_counts.values())
    origins_by_chunk: dict[str, list[str]] = defaultdict(list)
    for channel, chunk_ids in diagnostics.channel_chunk_ids:
        for chunk_id in chunk_ids:
            origins_by_chunk[chunk_id].append(channel)
    return CaseObservation(
        case_id=case.case_id,
        split=case.split,
        group_id=case.group_id,
        category=case.category,
        failure_severity=case.failure_severity,
        variant_id=context.variant_id,
        lane=context.lane,
        status=result.status.value,
        reason_code=result.reason_code,
        active_index_revision_id=result.active_index_revision_id,
        index_fingerprint=result.index_fingerprint,
        serving_fingerprint=result.serving_fingerprint,
        selected_embedding_slot=result.selected_embedding_slot,
        selected_vector_name=result.selected_vector_name,
        route_reason_code=result.route_reason_code,
        rerank_mode=result.rerank_execution_mode,
        channel_chunk_ids=diagnostics.channel_chunk_ids,
        fused_chunk_ids=diagnostics.fused_chunk_ids,
        reranked_chunk_ids=tuple(
            item.chunk_id for item in diagnostics.reranked
        ),
        expanded_chunk_ids=tuple(
            item.chunk_id for item in diagnostics.expanded
        ),
        evidence_document_ids=tuple(
            item.document_id
            for item in evidence
            if item.document_id is not None
        ),
        evidence_chunk_ids=tuple(item.chunk_id for item in evidence),
        retrieved_document_ids=retrieved_documents,
        retrieved_chunk_ids=retrieved_chunks,
        retrieval_origins=tuple(
            tuple(origins_by_chunk.get(identifier, ()))
            for identifier in ranked_evidence
        ),
        cited_document_ids=tuple(
            item.document_id for item in cited if item.document_id is not None
        ),
        cited_chunk_ids=tuple(item.chunk_id for item in cited),
        matched_source_range_count=matched,
        required_source_range_count=len(case.expected.required_source_ranges),
        predicted_source_range_count=len(predicted_ranges),
        relevant_predicted_source_range_count=relevant_predictions,
        citation_present=bool(cited),
        citation_valid=citation_valid,
        quote_publishable=bool(cited)
        and all(
            item.source_spans
            and all(
                span.is_citable
                and span.span_type is not SourceSpanKind.SEPARATOR
                for span in item.source_spans
            )
            for item in cited
        ),
        unsupported_claim_count=unsupported,
        evidence_budget_overflow_count=int(
            sum(max(1, (len(item.citation_text) + 3) // 4) for item in evidence)
            > context.evidence_token_budget
        ),
        wrong_scope_hit_count=wrong_scope,
        wrong_revision_hit_count=wrong_revision,
        wrong_vector_space_attempt_count=wrong_vector,
        latency_ms=latency_ms,
        provider_call_count=provider_calls,
        provider_retry_count=provider_retries,
        embedding_call_count=embedding_calls,
        embedding_retry_count=embedding_retries,
        reranker_call_count=reranker_calls,
        reranker_retry_count=reranker_retries,
        stage_elapsed_ms=tuple(
            (item.stage, item.elapsed_ms) for item in diagnostics.stage_timings
        ),
        evidence_count=len(evidence),
        evidence_tokens=sum(
            max(1, (len(item.citation_text) + 3) // 4) for item in evidence
        ),
        cache_hit=diagnostics.cache_hit,
        degraded_reason_codes=diagnostics.degraded_reason_codes,
    )


def _range_covers(
    predicted: tuple[str, str | None, int | None, int | None],
    expected: tuple[str, str | None, int | None, int | None],
) -> bool:
    if predicted[:2] != expected[:2]:
        return False
    predicted_start, predicted_end = predicted[2:]
    expected_start, expected_end = expected[2:]
    return (
        predicted_start is not None
        and predicted_end is not None
        and expected_start is not None
        and expected_end is not None
        and predicted_start <= expected_start
        and predicted_end >= expected_end
    )


def _operation_counts(
    calls: dict[str, ProviderCallCount], marker: str
) -> tuple[int, int]:
    matching = [
        item
        for operation, item in calls.items()
        if marker in operation.casefold()
    ]
    return (
        sum(item.call_count for item in matching),
        sum(item.retry_count for item in matching),
    )


def _validate_answer(
    result: SearchAnswerResult,
) -> tuple[tuple[str, ...], bool, int]:
    if result.answer is None:
        return (), False, 0
    claims = tuple(
        line.strip() for line in result.answer.splitlines() if line.strip()
    )
    by_quote = {item.citation_text.strip(): item for item in result.evidence}
    cited_ids = tuple(
        dict.fromkeys(
            by_quote[claim].evidence_id for claim in claims if claim in by_quote
        )
    )
    unsupported = sum(claim not in by_quote for claim in claims)
    try:
        validate_extractive_draft(
            AnswerDraft(
                text=result.answer,
                cited_evidence_ids=cited_ids,
            ),
            result.evidence,
        )
    except (ValidationFailed, ValueError):
        return cited_ids, False, unsupported
    return cited_ids, True, unsupported
