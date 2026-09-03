"""使用生产 P06/P07 合同执行 P08 离线合成评测。"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evaluation.v2.dataset import LoadedDataset
from evaluation.v2.fixtures import fixture_bytes, fixture_sha256
from evaluation.v2.models import (
    CaseObservation,
    ComponentIdentity,
    DatasetDocument,
    ErrorRecord,
    EvaluationCase,
    ProviderRunIdentity,
    SourceRangeExpectation,
)
from evaluation.v2.variants import EvaluationVariant
from rag_app.application.answering.validation import validate_extractive_draft
from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import P07Runtime, build_p07_runtime
from rag_app.composition.profiles import ComponentsProfile, RagProfile
from rag_app.core.errors import ValidationFailed
from rag_app.core.identifiers import (
    canonical_sha256,
    document_version_id,
)
from rag_app.core.models import (
    AnswerDraft,
    DocumentRef,
    EvidenceItem,
    KnowledgeBaseScope,
    ProviderCallCount,
    SearchAnswerResult,
    SearchRequest,
)
from rag_app.core.models.chunk import Chunk, SourceSpanKind
from rag_app.core.policies import EgressPolicy, ParsingPolicy, StoryPolicy

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_MINIMUM_SHARED_DOCUMENTS = 2
_RootCause = Literal[
    "Parser",
    "Chunker",
    "Table structure",
    "Identity/version",
    "Exact tokenizer",
    "FTS tokenizer",
    "Embedding primary",
    "Embedding standby",
    "RRF",
    "Reranker",
    "Neighbor/evidence",
    "Confidence/refusal",
    "Dataset label",
    "Infrastructure",
]


@dataclass(frozen=True, slots=True)
class VariantExecution:
    """一个候选的实际结果和 Manifest 身份材料。"""

    variant: EvaluationVariant
    cases: tuple[EvaluationCase, ...]
    observations: tuple[CaseObservation, ...]
    errors: tuple[ErrorRecord, ...]
    revision_ids: tuple[str, ...]
    index_fingerprints: tuple[str, ...]
    serving_fingerprints: tuple[str, ...]
    providers: tuple[ProviderRunIdentity, ...]
    parser: ComponentIdentity
    chunker: ComponentIdentity
    parsing_policy: ParsingPolicy
    tokenizer_identities: tuple[str, ...]
    build_elapsed_ms: float
    chunk_count: int


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    variant: EvaluationVariant
    runtime: P07Runtime
    revisions: dict[tuple[str, str], str]
    chunks: dict[str, Chunk]
    documents: tuple[DatasetDocument, ...]


def validate_fixture_identities(dataset: LoadedDataset) -> dict[str, int]:
    """证明 dver 绑定 document_id 和 bytes，显示名不参与。

    Args:
        dataset: 已通过 Group Split 和 Schema 校验的数据集。

    Returns:
        验证的文档、版本、共享字节和内容变化计数。

    Raises:
        ValueError: 任一已接受身份不变量不成立。

    """
    versions = 0
    changed = 0
    by_digest: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for document in dataset.manifest.documents:
        observed_by_digest: dict[str, str] = {}
        for version in document.versions:
            digest = fixture_sha256(version.fixture_id)
            identifier = document_version_id(document.document_id, digest)
            prior = observed_by_digest.setdefault(digest, identifier)
            if prior != identifier:
                raise ValueError("相同 document_id 和 bytes 产生不同 dver。")
            by_digest[digest].append((document.document_id, identifier))
            versions += 1
        if len(observed_by_digest) > 1:
            changed += 1
            if len(set(observed_by_digest.values())) != len(observed_by_digest):
                raise ValueError("同一 document_id 内容变化未产生新 dver。")
    shared_pairs = 0
    for items in by_digest.values():
        by_document = dict(items)
        if len(by_document) < _MINIMUM_SHARED_DOCUMENTS:
            continue
        shared_pairs += 1
        if len(set(by_document.values())) != len(by_document):
            raise ValueError("不同 document_id 的相同 bytes 共享了逻辑 dver。")
    return {
        "documents": len(dataset.manifest.documents),
        "versions": versions,
        "content_change_documents": changed,
        "shared_byte_groups": shared_pairs,
    }


def execute_offline_variant(
    dataset: LoadedDataset,
    cases: tuple[EvaluationCase, ...],
    variant: EvaluationVariant,
    *,
    requested_profile: RagProfile,
    data_directory: Path,
) -> VariantExecution:
    """构建临时不可变 Revision 并执行一个离线候选。

    Args:
        dataset: 版本化合成数据集。
        cases: 当前允许读取标签的 split。
        variant: 单变量检索或 Chunk 候选。
        requested_profile: 用户显式指定的无网络 Profile。
        data_directory: 本候选独占的临时状态目录。

    Returns:
        实际观测、错误分析和完整运行身份。

    Raises:
        ValueError: Profile 可能联网或 baseline 标签已漂移。

    """
    profile = _offline_persistent_profile(requested_profile, variant)
    build_started = time.perf_counter()
    with build_p07_runtime(
        profile,
        data_dir=data_directory,
        policy=variant.retrieval_policy,
    ) as runtime:
        revisions, active_chunks = _build_corpus(dataset, runtime)
        build_elapsed_ms = (time.perf_counter() - build_started) * 1000.0
        effective_cases = _effective_cases(
            cases,
            active_chunks,
            require_fixed_labels=variant.variant_id == "baseline",
        )
        context = _ExecutionContext(
            variant=variant,
            runtime=runtime,
            revisions=revisions,
            chunks=active_chunks,
            documents=dataset.manifest.documents,
        )
        observations = tuple(
            _execute_case(case, context)
            for case in effective_cases
        )
        errors = tuple(
            record
            for case, observation in zip(
                effective_cases, observations, strict=True
            )
            if (record := _error_record(case, observation)) is not None
        )
        components = runtime.persistence.components
        topology = components.embedding_topology
        providers = tuple(
            ProviderRunIdentity(
                provider=slot.provider_id,
                model=slot.model,
                slot=slot.slot_id,
                vector_name=slot.vector_name,
                request_policy_identity=canonical_sha256(
                    slot.query_request_policy
                ),
                adapter_revision=slot.adapter_revision,
            )
            for slot in topology.slots
        )
        parser = ComponentIdentity(
            component_id=components.parser.descriptor.name,
            version=components.parser.descriptor.version,
            fingerprint=canonical_sha256(
                components.parsing_policy.model_dump(mode="json")
            ),
        )
        chunker = ComponentIdentity(
            component_id=components.chunker.descriptor.name,
            version=components.chunker.descriptor.version,
            fingerprint=canonical_sha256(
                components.chunking_policy.model_dump(mode="json")
            ),
        )
        tokenizers = tuple(
            sorted({chunk.tokenizer_id for chunk in active_chunks.values()})
        )
        return VariantExecution(
            variant=variant,
            cases=effective_cases,
            observations=observations,
            errors=errors,
            revision_ids=tuple(sorted(revisions.values())),
            index_fingerprints=tuple(
                sorted({chunk.index_fingerprint for chunk in observations})
            ),
            serving_fingerprints=tuple(
                sorted({chunk.serving_fingerprint for chunk in observations})
            ),
            providers=providers,
            parser=parser,
            chunker=chunker,
            parsing_policy=components.parsing_policy,
            tokenizer_identities=tokenizers,
            build_elapsed_ms=build_elapsed_ms,
            chunk_count=len(active_chunks),
        )


def _offline_persistent_profile(
    profile: RagProfile,
    variant: EvaluationVariant,
) -> RagProfile:
    components = profile.components
    if (
        components.embedding_topology != "deterministic-single"
        or components.embedding_primary != "deterministic"
    ):
        raise ValueError(
            "offline-structural 只允许 deterministic single Profile。"
        )
    resolved = ComponentsProfile(
        parser="docx-ooxml-v4",
        chunker="docx-structural-v3",
        embedding_topology=components.embedding_topology,
        embedding_primary=components.embedding_primary,
        embedding_standby=None,
        embedding_router="embedding-router-single",
        reranker=components.reranker,
        vector_store="memory-vector",
        lexical_store="sqlite-fts5",
        metadata_store="sqlite-control",
        blob_store="filesystem-blob",
        generator=components.generator,
        trace_sink=components.trace_sink,
    )
    return profile.model_copy(
        update={
            "profile_id": "p08-offline",
            "components": resolved,
            "parsing": profile.parsing.model_copy(
                update={"footnotes_endnotes": StoryPolicy.PARSE}
            ),
            "chunking": variant.chunking_policy,
            "security": EgressPolicy(),
        }
    )


def _build_corpus(
    dataset: LoadedDataset,
    runtime: P07Runtime,
) -> tuple[dict[tuple[str, str], str], dict[str, Chunk]]:
    persistence = runtime.persistence
    documents_by_scope: dict[tuple[str, str], list[DatasetDocument]] = (
        defaultdict(list)
    )
    for document in dataset.manifest.documents:
        documents_by_scope[
            (document.project_id, document.knowledge_base_id)
        ].append(document)
    revisions: dict[tuple[str, str], str] = {}
    active_chunks: dict[str, Chunk] = {}
    for (project_id, knowledge_base_id), documents in sorted(
        documents_by_scope.items()
    ):
        persistence.control.put_project(project_id, "P08 Synthetic Project")
        persistence.control.put_knowledge_base(
            knowledge_base_id,
            project_id,
            f"P08 Synthetic KB {knowledge_base_id[-8:]}",
            profile_id=persistence.components.profile.profile_id,
        )
        ingestion: list[IngestionDocument] = []
        for document in sorted(documents, key=lambda item: item.document_id):
            active_version = document.versions[-1]
            reference = DocumentRef(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document.document_id,
                display_name=active_version.display_name,
            )
            persistence.control.upsert_document(reference)
            ingestion.append(
                IngestionDocument(
                    document=reference,
                    content=fixture_bytes(active_version.fixture_id),
                    media_type=_MEDIA_TYPE,
                )
            )
        result = persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=tuple(ingestion),
            idempotency_key=canonical_sha256(
                {
                    "dataset": dataset.dataset_sha256,
                    "scope": (project_id, knowledge_base_id),
                    "index": persistence.components.index_fingerprint,
                }
            ),
            budgets=persistence.default_budgets(),
        )
        spec = persistence.control.revision_vector_spec(result.revision_id)
        persistence.validator.validate(
            spec,
            current_index_fingerprint=persistence.components.index_fingerprint,
        )
        revisions[(project_id, knowledge_base_id)] = result.revision_id
        for chunk in persistence.control.chunk_rows(result.revision_id):
            active_chunks[chunk.chunk_id] = chunk
    return revisions, active_chunks


def _effective_cases(
    cases: tuple[EvaluationCase, ...],
    chunks: dict[str, Chunk],
    *,
    require_fixed_labels: bool,
) -> tuple[EvaluationCase, ...]:
    output: list[EvaluationCase] = []
    for case in cases:
        if not case.expected.answerable:
            output.append(case)
            continue
        relevant_documents = set(case.expected.relevant_document_ids)
        relevant_chunks = tuple(
            chunk
            for chunk in chunks.values()
            if chunk.version.document_id in relevant_documents
        )
        marker = case.constraints.required_embedding_marker
        if marker is not None:
            if not any(
                marker in chunk.embedding_text for chunk in relevant_chunks
            ):
                raise ValueError(
                    f"{case.case_id}: embedding marker 未保留。"
                )
            if any(marker in chunk.citation_text for chunk in relevant_chunks):
                raise ValueError(
                    f"{case.case_id}: 非原文 marker 泄漏到 citation_text。"
                )
        expected_texts = {
            item.exact_text for item in case.expected.required_source_ranges
        }
        compiled = tuple(
            sorted(
                chunk.chunk_id
                for chunk in chunks.values()
                if chunk.version.document_id in relevant_documents
                and any(text in chunk.citation_text for text in expected_texts)
            )
        )
        if not compiled:
            raise ValueError(f"{case.case_id}: 来源标签未匹配 active Chunk。")
        fixed = tuple(sorted(case.expected.relevant_chunk_ids))
        if require_fixed_labels and fixed != compiled:
            raise ValueError(
                f"{case.case_id}: baseline chunk 标签漂移，"
                f"expected={fixed} observed={compiled}"
            )
        resolved_ranges = tuple(
            _resolve_source_expectation(expectation, relevant_chunks)
            for expectation in case.expected.required_source_ranges
        )
        expected = case.expected.model_copy(
            update={
                "relevant_chunk_ids": compiled,
                "required_source_ranges": resolved_ranges,
            }
        )
        output.append(case.model_copy(update={"expected": expected}))
    return tuple(output)


def _resolve_source_expectation(
    expectation: SourceRangeExpectation,
    chunks: tuple[Chunk, ...],
) -> SourceRangeExpectation:
    matches: list[tuple[str, int, int]] = []
    for chunk in chunks:
        if chunk.version.document_id != expectation.document_id:
            continue
        if expectation.node_kind is not None and (
            chunk.role.value != expectation.node_kind
        ):
            continue
        cursor = 0
        while True:
            start = chunk.citation_text.find(expectation.exact_text, cursor)
            if start < 0:
                break
            end = start + len(expectation.exact_text)
            for span in chunk.source_spans:
                if (
                    span.is_citable
                    and span.node_id is not None
                    and span.source_start_char is not None
                    and span.chunk_start_char <= start
                    and span.chunk_end_char >= end
                    and (
                        expectation.structural_anchor is None
                        or span.structural_path
                        == expectation.structural_anchor
                    )
                ):
                    source_start = span.source_start_char + (
                        start - span.chunk_start_char
                    )
                    matches.append(
                        (
                            span.node_id,
                            source_start,
                            source_start + len(expectation.exact_text),
                        )
                    )
            cursor = start + 1
    matches = sorted(set(matches))
    if not matches:
        raise ValueError("Source Range 标签无法解析到唯一 canonical span。")
    if len(matches) > 1 and (
        expectation.occurrence is None
        and expectation.structural_anchor is None
    ):
        raise ValueError(
            "重复 Source Range 必须指定 occurrence 或 structural anchor。"
        )
    occurrence = expectation.occurrence or 1
    if occurrence > len(matches):
        raise ValueError("Source Range occurrence 超出实际出现次数。")
    node_id, source_start, source_end = matches[occurrence - 1]
    return expectation.model_copy(
        update={
            "node_id": node_id,
            "source_start_char": source_start,
            "source_end_char": source_end,
        }
    )


def _execute_case(
    case: EvaluationCase,
    context: _ExecutionContext,
) -> CaseObservation:
    variant = context.variant
    runtime = context.runtime
    scope = KnowledgeBaseScope(
        project_id=case.project_id,
        knowledge_base_id=case.knowledge_base_id,
    )
    started = time.perf_counter()
    result = runtime.retrieval.search_and_answer(
        SearchRequest(scope=scope, text=case.query, limit=10)
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
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
            _range_covers(predicted, expected)
            for predicted in predicted_ranges
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
    expected_revision = context.revisions[
        (case.project_id, case.knowledge_base_id)
    ]
    wrong_revision = sum(
        context.chunks[identifier].index_revision_id != expected_revision
        for identifier in retrieved_chunks
        if identifier in context.chunks
    ) + sum(
        identifier not in context.chunks for identifier in retrieved_chunks
    )
    expected_vectors = {
        slot.slot_id: slot.vector_name
        for slot in runtime.persistence.components.embedding_topology.slots
    }
    wrong_vector = int(
        result.selected_embedding_slot is not None
        and expected_vectors.get(result.selected_embedding_slot)
        != result.selected_vector_name
    )
    call_counts = {item.operation: item for item in diagnostics.provider_calls}
    embedding_calls, embedding_retries = _operation_counts(
        call_counts, "embed"
    )
    reranker_calls, reranker_retries = _operation_counts(
        call_counts, "rerank"
    )
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
        variant_id=variant.variant_id,
        lane="offline-structural",
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
        required_source_range_count=len(
            case.expected.required_source_ranges
        ),
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
            > variant.retrieval_policy.evidence_token_budget
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
            max(1, (len(item.citation_text) + 3) // 4)
            for item in evidence
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
            by_quote[claim].evidence_id
            for claim in claims
            if claim in by_quote
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


def _error_record(
    case: EvaluationCase,
    observation: CaseObservation,
) -> ErrorRecord | None:
    relevant_hit = bool(
        set(observation.retrieved_chunk_ids)
        & set(case.expected.relevant_chunk_ids)
    )
    relevance_correct = (
        relevant_hit
        if case.expected.answerable
        else observation.status != "ANSWERABLE"
    )
    answerability_correct = (
        observation.status == "ANSWERABLE"
    ) == case.expected.answerable
    safe = (
        observation.wrong_scope_hit_count == 0
        and observation.wrong_revision_hit_count == 0
        and observation.wrong_vector_space_attempt_count == 0
    )
    citation_ok = not case.expected.answerable or observation.citation_valid
    if relevance_correct and answerability_correct and safe and citation_ok:
        return None
    bucket, stage = _root_cause(case, observation, relevant_hit)
    channel_ranks: list[tuple[str, int]] = []
    seen: set[str] = set()
    for rank, origins in enumerate(observation.retrieval_origins, start=1):
        for origin in origins:
            if origin not in seen:
                seen.add(origin)
                channel_ranks.append((origin, rank))
    return ErrorRecord(
        case_id=case.case_id,
        variant_id=observation.variant_id,
        category=case.category,
        lane=observation.lane,
        selected_slot=observation.selected_embedding_slot,
        channel_ranks=tuple(channel_ranks),
        rerank_mode=observation.rerank_mode,
        expected_document_ids=case.expected.relevant_document_ids,
        observed_document_ids=observation.retrieved_document_ids,
        expected_chunk_ids=case.expected.relevant_chunk_ids,
        observed_chunk_ids=observation.retrieved_chunk_ids,
        failure_stage=stage,
        root_cause_bucket=bucket,
        safe_evidence_hashes=(
            canonical_sha256(observation.retrieved_chunk_ids),
        ),
        recommended_action=(
            "复核该根因桶的通道和固定标签；禁止为单一 Case 添加业务硬编码。"
        ),
    )


def _root_cause(
    case: EvaluationCase,
    observation: CaseObservation,
    relevant_hit: bool,
) -> tuple[_RootCause, str]:
    if observation.wrong_scope_hit_count:
        bucket: _RootCause = "Identity/version"
        stage = "scope_filter"
    elif observation.wrong_revision_hit_count:
        bucket = "Identity/version"
        stage = "revision_snapshot"
    elif observation.wrong_vector_space_attempt_count:
        bucket = "Embedding standby"
        stage = "query_embedding_route"
    elif case.category == "table_structure" and not relevant_hit:
        bucket = "Table structure"
        stage = "chunk_or_retrieval"
    elif case.category == "exact_identifier" and not relevant_hit:
        bucket = "Exact tokenizer"
        stage = "exact_retrieval"
    elif not relevant_hit:
        bucket = "RRF"
        stage = "retrieval"
    elif observation.status != "ANSWERABLE" and case.expected.answerable:
        bucket = "Confidence/refusal"
        stage = "confidence"
    else:
        bucket = "Neighbor/evidence"
        stage = "citation_validation"
    return bucket, stage


__all__ = [
    "VariantExecution",
    "execute_offline_variant",
    "validate_fixture_identities",
]
