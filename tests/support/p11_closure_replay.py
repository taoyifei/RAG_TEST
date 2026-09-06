"""公开 pilot 历史排名的零网络复放；缺失 Provider 分数始终保留为空。"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import patch

from evaluation.p11_pilot import evaluate_pilot
from evaluation.p11_pilot_data import load_pilot_dataset
from evaluation.v2.models import SourceRangeExpectation
from evaluation.v2.observations import ObservationContext, observe_case_result
from rag_app.adapters.legacy.providers import ExtractiveGenerator
from rag_app.application.answering.service import ExtractiveAnsweringService
from rag_app.application.retrieval import evidence as evidence_module
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.answer_support import AnswerSupport
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.neighbors import NeighborExpander
from rag_app.application.retrieval.planner import QueryPlanner
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    Chunk,
    DiagnosticExpansionItem,
    DiagnosticRerankItem,
    EvidenceSelectionContext,
    HydratedChunk,
    KnowledgeBaseScope,
    QueryVariant,
    RankedChunk,
    RetrievalDiagnostics,
    RetrievalPolicy,
    RrfContribution,
    SearchAnswerResult,
    SearchRequest,
    SourceSpan,
)
from rag_app.core.ports import EvidenceSourcePort

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts/p11-final/quality-security-closure/replay"
INPUT = Path(__file__).with_name("p11_closure_input.json")


class CanonicalSource:
    """只允许读取已冻结 canonical Chunk 的内存来源。"""

    def __init__(self, chunks: dict[str, Chunk]) -> None:
        self.chunks = chunks

    def hydrate_chunks(
        self, snapshot: ActiveRevisionQuerySnapshot, chunk_ids: tuple[str, ...]
    ) -> tuple[HydratedChunk, ...]:
        """返回原始 Chunk，不修改位置或身份。"""
        del snapshot
        return tuple(
            HydratedChunk(chunk=self.chunks[key], display_name="公开合成.docx")
            for key in chunk_ids
        )

    def section_chunk_ids(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        *,
        document_version_id: str,
        section_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        """按原始顺序返回有界同节 ID。"""
        del snapshot
        return tuple(
            c.chunk_id
            for c in self.chunks.values()
            if c.version.document_version_id == document_version_id
            and c.section_id == section_id
        )[:limit]


@contextmanager
def no_network() -> Iterator[None]:
    """封死全部 socket 连接，包括 Provider 和本地服务。"""

    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("REPLAY_NETWORK_FORBIDDEN")

    with (
        patch.object(socket.socket, "connect", blocked),
        patch.object(socket.socket, "connect_ex", blocked),
        patch.object(socket, "create_connection", blocked),
    ):
        yield


def _baseline(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "p11_baseline_" + name, ARTIFACTS / ("baseline_" + name + ".py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _record_span_admission(
    module: ModuleType, trace: list[dict[str, Any]]
) -> Iterator[None]:
    original = module._ranked_citable_spans
    accepts_support_overrides = (
        "support_overrides" in inspect.signature(original).parameters
    )

    def record(  # noqa: PLR0913
        chunk: Chunk,
        used: set[tuple[object, ...]],
        *,
        context: EvidenceSelectionContext | None,
        minimum_overlap: float,
        allow_semantic: bool,
        table_spans: set[tuple[object, ...]] | None = None,
        support_overrides: dict[tuple[object, ...], AnswerSupport]
        | None = None,
    ) -> tuple[tuple[SourceSpan, str, tuple[object, ...]], ...]:
        extra = (
            {"support_overrides": support_overrides}
            if accepts_support_overrides
            else {}
        )
        selected = original(
            chunk,
            used,
            context=context,
            minimum_overlap=minimum_overlap,
            allow_semantic=allow_semantic,
            table_spans=table_spans,
            **extra,
        )
        selected_keys = {key for _, _, key in selected}
        for span in chunk.source_spans:
            key = module._span_key(chunk, span)
            quote = chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ]
            support = (support_overrides or {}).get(key)
            support_source = "bounded_context_override" if support else None
            if (
                support is None
                and context is not None
                and hasattr(module, "evaluate_span_support")
            ):
                support = module.evaluate_span_support(
                    context.analysis,
                    quote,
                    span_id=span.node_id or "",
                    table_relation=table_spans is not None,
                )
                support_source = "evaluate_span_support"
            reason = "ADMITTED_BEFORE_PACKING"
            if key not in selected_keys:
                if not span.is_citable or span.span_type.value == "separator":
                    reason = "NOT_CITABLE"
                elif key in used:
                    reason = "ALREADY_SELECTED_SOURCE"
                elif table_spans is not None and key not in table_spans:
                    reason = "OUTSIDE_TABLE_INTERSECTION"
                elif not quote.strip():
                    reason = "EMPTY_QUOTE"
                elif (
                    support is not None and support.status.value != "SUPPORTED"
                ):
                    reason = "ANSWER_SUPPORT_" + support.status.value
                else:
                    reason = "SPAN_RELEVANCE_OR_SUPPORT_REJECTED"
            trace.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "node_id": span.node_id,
                    "source_start_char": span.source_start_char,
                    "source_end_char": span.source_end_char,
                    "quote": quote,
                    "is_citable": span.is_citable,
                    "admission_reason": reason,
                    "minimum_overlap": minimum_overlap,
                    "allow_semantic": allow_semantic,
                    "relevance": module._span_relevance(
                        quote, context, chunk.role.value
                    ),
                    "table_intersection": table_spans is not None,
                    "answer_support": asdict(support) if support else None,
                    "support_source": support_source,
                }
            )
        return cast(
            tuple[tuple[SourceSpan, str, tuple[object, ...]], ...], selected
        )

    with patch.object(module, "_ranked_citable_spans", record):
        yield


def ranked_inputs(
    observation: dict[str, Any],
    chunks: dict[str, Chunk],
    policy: RetrievalPolicy,
) -> tuple[RankedChunk, ...]:
    """重建真实历史排名及公开 RRF 贡献；不填补未知原始分数。"""
    origins: dict[str, list[RrfContribution]] = {}
    for channel, identifiers in observation["channel_chunk_ids"]:
        for rank, identifier in enumerate(identifiers, 1):
            origins.setdefault(identifier, []).append(
                RrfContribution(
                    channel=channel,
                    rank=rank,
                    weight=1.0,
                    contribution=1.0 / (policy.rrf_k + rank),
                )
            )
    return tuple(
        RankedChunk(
            hydrated=HydratedChunk(
                chunk=chunks[key], display_name="公开合成.docx"
            ),
            fusion_rank=observation["fused_chunk_ids"].index(key) + 1,
            rerank_rank=rank,
            rerank_score=None,
            contributions=tuple(
                sorted(origins[key], key=lambda item: (item.rank, item.channel))
            ),
        )
        for rank, key in enumerate(observation["reranked_chunk_ids"], 1)
    )


def run_replay(*, baseline: bool = False) -> dict[str, Any]:
    """运行两路原 30 问，返回原指标计算器结果和完整候选差异。

    Args:
        baseline: 使用修复前保存的原始三个应用模块。

    Returns:
        标明离线、分数缺失和零 HTTP 的逐题结果，不声称新 Live。

    """
    with no_network():
        return _run_replay(baseline=baseline)


def _run_replay(*, baseline: bool) -> dict[str, Any]:  # noqa: PLR0915
    fixture = json.loads(INPUT.read_text("utf-8"))
    chunks = {
        item["chunk_id"]: Chunk.model_validate(item)
        for item in fixture["inventory"]["chunks"]
    }
    source = CanonicalSource(chunks)
    first = next(iter(chunks.values()))
    scope = KnowledgeBaseScope(
        project_id=first.project_id, knowledge_base_id=first.knowledge_base_id
    )
    policy = RetrievalPolicy.model_validate(fixture["inventory"]["policy"])
    # 复用验收隔离上下文的已存在校准状态，不写入产品 Profile 或质量记录。
    vector_spaces = fixture["inventory"]["vector_spaces"]
    policy = policy.model_copy(
        update={
            "dense_semantic_enabled": True,
            "dense_semantic_calibration_state": "CONTROLLED_TEST_ONLY",
            "dense_calibrated_vector_spaces": tuple(vector_spaces.values()),
        }
    )
    evidence_impl = _baseline("evidence") if baseline else evidence_module
    assembler = evidence_impl.EvidenceAssembler()
    evaluator = (
        _baseline("confidence").ConfidenceEvaluator()
        if baseline
        else ConfidenceEvaluator()
    )
    expander_type = (
        _baseline("neighbors").NeighborExpander
        if baseline
        else NeighborExpander
    )
    expander = expander_type(cast(EvidenceSourcePort, source))
    dataset = load_pilot_dataset()
    corpus = json.loads(
        (ROOT / "evaluation/datasets/p11-pilot/corpus.json").read_text("utf-8")
    )
    documents = []
    document_ids = {}
    for document in dataset.manifest.documents:
        paragraph = corpus[document.versions[-1].fixture_id]["paragraphs"][0]
        matching = next(
            (c for c in chunks.values() if paragraph in c.citation_text), None
        )
        if matching is not None:
            document_ids[document.document_id] = matching.version.document_id
            documents.append(
                document.model_copy(
                    update={
                        "document_id": matching.version.document_id,
                        "project_id": scope.project_id,
                        "knowledge_base_id": scope.knowledge_base_id,
                    }
                )
            )
    cases = []
    for case in dataset.cases:
        ranges = tuple(
            SourceRangeExpectation.model_validate(
                r
                | {
                    "exact_text": case.expected.required_source_ranges[
                        index
                    ].exact_text
                }
            )
            for index, r in enumerate(
                fixture["source_ranges"]["primary:" + case.case_id]["expected"]
            )
        )
        ids = tuple(
            c.chunk_id
            for c in chunks.values()
            if any(
                s.node_id == r.node_id for s in c.source_spans for r in ranges
            )
        )
        expected = case.expected.model_copy(
            update={
                "required_source_ranges": ranges,
                "relevant_document_ids": tuple(
                    document_ids.get(d, d)
                    for d in case.expected.relevant_document_ids
                ),
                "relevant_chunk_ids": ids,
            }
        )
        cases.append(
            case.model_copy(
                update={
                    "expected": expected,
                    "project_id": scope.project_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                }
            )
        )
    by_case = {case.case_id: case for case in cases}
    observations: dict[str, tuple[Any, ...]] = {}
    details = []
    for lane, historical in fixture["observations"].items():
        vector_space = vector_spaces[lane]
        lane_results = []
        for old in historical:
            case = by_case[old["case_id"]]
            analysis = QueryAnalyzer().analyze(
                SearchRequest(scope=scope, text=case.query)
            )
            variant = QueryVariant(
                text=case.query, kind="original", identity="sha256:" + "0" * 64
            )
            plan = QueryPlanner().plan(analysis, (variant,), policy)
            ranked = ranked_inputs(old, chunks, policy)
            expansion = expander.expand(
                cast(ActiveRevisionQuerySnapshot, object()),
                ranked,
                plan.neighbor_mode,
                policy,
            )
            context = EvidenceSelectionContext(
                analysis=analysis,
                query_kind=plan.query_kind,
                rerank_mode=old["rerank_mode"],
                selected_slot=lane,
                selected_vector_space=vector_space,
            )
            span_trace: list[dict[str, Any]] = []
            with _record_span_admission(evidence_impl, span_trace):
                evidence = assembler.assemble(
                    expansion.candidates, policy, context=context
                )
            selected_sources = {
                (
                    item.chunk_id,
                    span.node_id,
                    span.source_start_char,
                    span.source_end_char,
                )
                for item in evidence
                for span in item.source_spans
            }
            for span in span_trace:
                span["selected"] = (
                    span["chunk_id"],
                    span["node_id"],
                    span["source_start_char"],
                    span["source_end_char"],
                ) in selected_sources
                if (
                    span["admission_reason"] == "ADMITTED_BEFORE_PACKING"
                    and not span["selected"]
                ):
                    span["selection_reason"] = (
                        "NOT_SELECTED_BY_EXISTING_CAP_OR_BUDGET"
                    )
                else:
                    span["selection_reason"] = (
                        "SELECTED"
                        if span["selected"]
                        else span["admission_reason"]
                    )
            confidence = evaluator.evaluate(
                analysis,
                plan.query_kind,
                expansion.candidates,
                evidence,
                expansion.degraded_reason_codes,
                policy=policy,
                rerank_mode=old["rerank_mode"],
                selected_vector_space=vector_space,
            )
            answer = ExtractiveAnsweringService(ExtractiveGenerator()).answer(
                case.query, evidence, confidence
            )
            diagnostics = RetrievalDiagnostics(
                channel_chunk_ids=old["channel_chunk_ids"],
                fused_chunk_ids=old["fused_chunk_ids"],
                reranked=tuple(
                    DiagnosticRerankItem(
                        chunk_id=c.hydrated.chunk.chunk_id,
                        rank=cast(int, c.rerank_rank),
                        score=None,
                    )
                    for c in ranked
                ),
                expanded=tuple(
                    DiagnosticExpansionItem(
                        chunk_id=c.hydrated.chunk.chunk_id,
                        reason=c.expansion_reason,
                    )
                    for c in expansion.candidates
                ),
                cited_chunk_ids=tuple(e.chunk_id for e in evidence)
                if answer
                else (),
            )
            result = SearchAnswerResult(
                trace_id="trace_" + "0" * 32,
                status=confidence.status,
                reason_code=confidence.status.value,
                answer=answer,
                evidence=evidence,
                confidence=confidence,
                query_kind=plan.query_kind,
                active_index_revision_id=old["active_index_revision_id"],
                index_fingerprint=old["index_fingerprint"],
                serving_fingerprint=old["serving_fingerprint"],
                selected_embedding_slot=lane,
                selected_vector_name=old["selected_vector_name"],
                route_reason_code=old["route_reason_code"],
                rerank_execution_mode=old["rerank_mode"],
                generation_mode="extractive" if answer else "none",
                cache_key="sha256:" + "0" * 64,
                diagnostics=diagnostics,
            )
            observed = observe_case_result(
                case,
                result,
                ObservationContext(
                    chunks=chunks,
                    documents=tuple(documents),
                    expected_revision=old["active_index_revision_id"],
                    expected_vectors=(
                        ("primary", "dense_primary"),
                        ("standby", "dense_standby"),
                    ),
                    variant_id="p11-fixed-profile",
                    lane="offline-" + lane,
                    evidence_token_budget=policy.evidence_token_budget,
                ),
                latency_ms=0.0,
            )
            lane_results.append(observed)
            details.append(
                {
                    "lane": lane,
                    "case_id": case.case_id,
                    "query": case.query,
                    "historical_status": old["status"],
                    "status": confidence.status.value,
                    "historical_source_count": old[
                        "matched_source_range_count"
                    ],
                    "source_count": observed.matched_source_range_count,
                    "correct": (confidence.status.value == "ANSWERABLE")
                    == case.expected.answerable
                    and observed.matched_source_range_count
                    == observed.required_source_range_count,
                    "ranked": [
                        c.model_dump(mode="json", exclude={"hydrated"})
                        | {"chunk_id": c.hydrated.chunk.chunk_id}
                        for c in ranked
                    ],
                    "expanded": [
                        c.model_dump(mode="json", exclude={"hydrated"})
                        | {"chunk_id": c.hydrated.chunk.chunk_id}
                        for c in expansion.candidates
                    ],
                    "evidence": [e.model_dump(mode="json") for e in evidence],
                    "confidence": confidence.model_dump(mode="json"),
                    "generator_text": answer,
                    "span_trace": span_trace,
                }
            )
        observations[lane] = tuple(lane_results)
    report = evaluate_pilot(
        tuple(cases),
        observations,
        evaluation_kind="independent_holdout"
        if baseline
        else "exposed_regression",
    )
    return {
        "mode": "offline_regression",
        "classification": "exposed_set_regression",
        "provider_http": 0,
        "network_guard": "all_socket_connect_blocked",
        "baseline": baseline,
        "missing_fields": fixture["missing_fields"],
        "reconstruction": fixture["reconstruction"],
        "identity_scope": {
            "observation_fingerprints": (
                "historical_profile_and_index_reused_as_offline_labels"
            ),
            "implementation_identity": "replayed_code_sha256",
            "current_live_serving_fingerprint": None,
            "note": (
                "离线重建保留旧观测 Profile/index/serving 标签，"
                "并绑定本次实际实现哈希。未构造新运行现场，"
                "也不把旧 serving fingerprint 声称为新服务的查询缓存身份。"
            ),
        },
        "replayed_code_sha256": {
            name: hashlib.sha256(
                (
                    ARTIFACTS / ("baseline_" + name + ".py")
                    if baseline
                    and name in {"neighbors", "evidence", "confidence"}
                    else ROOT
                    / ("src/rag_app/application/retrieval/" + name + ".py")
                ).read_bytes()
            ).hexdigest()
            for name in (
                "neighbors",
                "evidence",
                "confidence",
                "analyzer",
                *(("answer_support",) if not baseline else ()),
            )
        },
        "report": report.model_dump(mode="json"),
        "observations": {
            lane: [o.model_dump(mode="json") for o in obs]
            for lane, obs in observations.items()
        },
        "details": details,
    }


if __name__ == "__main__":
    baseline_mode = "--baseline" in sys.argv
    output = run_replay(baseline=baseline_mode)
    path = ARTIFACTS / (
        "baseline-result.json" if baseline_mode else "current-result.json"
    )
    path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "provider_http": 0,
                "failures": output["report"]["failures"],
                "output": str(path),
            },
            ensure_ascii=False,
        )
    )
