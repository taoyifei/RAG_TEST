import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rag_app.generation.answer import (
    AnswerResult,
    AnswerStatus,
    RefusalCode,
)
from rag_app.generation.evidence import EvidenceBundle
from rag_app.model_contracts import VerifiedClaimContext
from rag_app.query_service import QueryDependencies, QueryService
from rag_app.retrieval.hybrid import HybridRetrievalResult
from rag_app.retrieval.neighbors import NeighborExpansionResult
from rag_app.retrieval.rerank import RerankedHit, RerankStageResult
from rag_app.retrieval.rewrite import QueryVariants
from rag_app.state.conversations import ConversationStore
from rag_app.tracing.models import (
    JsonValue,
    TraceIdentity,
    TraceMode,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import TraceRecorder
from rag_app.tracing.store import TraceStore


class _Rewriter:
    def rewrite(
        self,
        question: str,
        *,
        previous_questions: tuple[str, ...],
        verified_claims: tuple[VerifiedClaimContext, ...],
    ) -> QueryVariants:
        assert verified_claims == ()
        return QueryVariants(
            queries=(question,),
            resolved_query=question,
            rewritten=False,
            call=None,
            trace={
                "reason_code": "NO_HISTORY",
                "question_sha256": "1" * 64,
                "history_sha256": "2" * 64,
                "resolved_query_sha256": "1" * 64,
                "question_tokens": 2,
                "history_tokens": 0,
                "selected_history_tokens": 0,
                "resolved_query_tokens": 2,
                "question": question,
                "history": list(previous_questions),
                "resolved_query": question,
                "Authorization": "Bearer synthetic-secret-value",
                "embedding_vector": [0.1, 0.2],
                "ocr_base64": "c3ludGhldGlj",
            },
        )


class _Retriever:
    def retrieve(
        self,
        variants: QueryVariants,
        *,
        as_of: datetime,
    ) -> HybridRetrievalResult:
        del variants, as_of
        channels: list[JsonValue] = [
            {
                "name": name,
                "query_variant_index": 0,
                "channel_type": channel_type,
                "limit": 20,
                "returned_count": 0,
                "duration_ms": 2,
                "candidates": [],
            }
            for name, channel_type in (
                ("q0:dense", "dense"),
                ("q0:bm25", "bm25"),
            )
        ]
        return HybridRetrievalResult(
            candidates=(),
            query_count=1,
            embedding_calls=1,
            trace={
                "embedding_duration_ms": 3,
                "embedding_query_count": 1,
                "route": {
                    "route_id": None,
                    "source_ids": [],
                    "confidence": 0.0,
                    "routed": False,
                    "reason_code": "NO_RULES",
                    "threshold": 0.75,
                    "rule_scores": [],
                },
                "channels": channels,
                "fused": [],
                "rrf_rank_constant": 60,
                "candidate_limit": 20,
            },
        )


class _Reranker:
    def rerank(
        self,
        query: str,
        candidates: tuple[object, ...],
    ) -> RerankStageResult:
        del query, candidates
        return RerankStageResult(
            hits=(),
            call=None,
            scored_hits=(),
            input_candidate_count=0,
        )


class _Neighbors:
    def expand_with_trace(
        self,
        ranked_hits: tuple[RerankedHit, ...],
    ) -> NeighborExpansionResult:
        return NeighborExpansionResult(
            hits=ranked_hits,
            decisions=(),
        )


class _Assembler:
    def assemble(self, ranked_hits: tuple[object, ...]) -> EvidenceBundle:
        del ranked_hits
        return EvidenceBundle(
            items=(),
            rendered_json='{"evidence":[]}',
            token_count=4,
            quarantined_chunk_ids=(),
        )


class _Answerer:
    def answer(
        self,
        question: str,
        evidence: EvidenceBundle,
    ) -> AnswerResult:
        del question, evidence
        return AnswerResult(
            status=AnswerStatus.REFUSED,
            answer=None,
            claims=(),
            refusal_code=RefusalCode.NO_EVIDENCE,
            model_calls=0,
            calls=(),
            trace={
                "first_validation_code": "NO_EVIDENCE",
                "repair_triggered": False,
                "generations": [],
            },
        )


class _StructuredAnswerer:
    def answer(
        self,
        question: str,
        evidence: EvidenceBundle,
    ) -> AnswerResult:
        del question, evidence
        return AnswerResult(
            status=AnswerStatus.REFUSED,
            answer=None,
            claims=(),
            refusal_code=RefusalCode.EVIDENCE_INSUFFICIENT,
            model_calls=1,
            calls=(),
            trace={
                "first_validation_code": "EVIDENCE_INSUFFICIENT",
                "repair_triggered": False,
                "generations": [
                    {
                        "phase": "first",
                        "model": "Qwen/Qwen3-8B-AWQ",
                        "endpoint": "http://10.0.0.1:8000",
                        "retry_count": 0,
                        "elapsed_ms": 1000,
                        "prompt_tokens": 4829,
                        "completion_tokens": 20,
                        "total_tokens": 4849,
                        "max_output_tokens": 2048,
                        "claims_count": 0,
                        "top_level_keys": ["claims"],
                        "json_parse_ok": True,
                        "validation_code": "EVIDENCE_INSUFFICIENT",
                        "raw_output": (
                            '{"claims":[],"raw_secret":"must-not-export"}'
                        ),
                    }
                ],
            },
        )


class _AbstentionReviewAnswerer:
    def answer(
        self,
        question: str,
        evidence: EvidenceBundle,
    ) -> AnswerResult:
        del question, evidence
        return AnswerResult(
            status=AnswerStatus.REFUSED,
            answer=None,
            claims=(),
            refusal_code=RefusalCode.EVIDENCE_INSUFFICIENT,
            model_calls=2,
            calls=(),
            trace={
                "intent": "PROCEDURE",
                "evidence_count": 3,
                "non_low_ocr_evidence_count": 3,
                "first_validation_code": "MODEL_ABSTAINED",
                "review_triggered": True,
                "review_reason_code": "ABSTENTION_REVIEW_EMPTY",
                "review_validation_code": "EVIDENCE_INSUFFICIENT",
                "repair_triggered": False,
                "generations": [
                    {
                        "phase": "first",
                        "model": "Qwen/Qwen3-8B-AWQ",
                        "endpoint": "http://10.0.0.1:8000",
                        "retry_count": 0,
                        "prompt_tokens": 4829,
                        "completion_tokens": 5,
                        "claims_count": 0,
                        "top_level_keys": ["claims"],
                        "json_parse_ok": True,
                        "validation_code": "MODEL_ABSTAINED",
                        "raw_output": '{"claims":[]}',
                    },
                    {
                        "phase": "abstention_review",
                        "model": "Qwen/Qwen3-8B-AWQ",
                        "endpoint": "http://10.0.0.1:8001",
                        "retry_count": 0,
                        "prompt_tokens": 4900,
                        "completion_tokens": 5,
                        "claims_count": 0,
                        "top_level_keys": ["claims"],
                        "json_parse_ok": True,
                        "validation_code": "EVIDENCE_INSUFFICIENT",
                        "raw_output": '{"claims":[]}',
                    },
                ],
            },
        )


def _service(
    tmp_path: Path,
    *,
    recorder: TraceRecorder | None,
    assembler: object | None = None,
    answerer: object | None = None,
) -> QueryService:
    tmp_path.mkdir(parents=True, exist_ok=True)
    conversations = ConversationStore(
        tmp_path / "state.sqlite3",
        ttl_seconds=300,
        max_rounds=3,
    )
    conversations.initialize()
    identity = (
        None
        if recorder is None
        else TraceIdentity(
            pipeline_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            release_revision="release-1",
            active_collection="rag-active-v1",
            index_manifest_sha256="3" * 64,
            payload_schema_version=2,
        )
    )
    return QueryService(
        dependencies=QueryDependencies(
            conversations=conversations,
            rewriter=_Rewriter(),  # type: ignore[arg-type]
            retriever=_Retriever(),  # type: ignore[arg-type]
            reranker=_Reranker(),  # type: ignore[arg-type]
            neighbors=_Neighbors(),  # type: ignore[arg-type]
            assembler=(_Assembler() if assembler is None else assembler),  # type: ignore[arg-type]
            answerer=(
                _Answerer() if answerer is None else answerer
            ),  # type: ignore[arg-type]
        ),
        trace_recorder=recorder,
        trace_identity=identity,
    )


def test_safe_trace_has_complete_tree_without_business_artifacts(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    service = _service(tmp_path, recorder=recorder)
    now = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    trace_id = "a" * 32

    outcome = service.ask(
        trace_id=trace_id,
        conversation_id="conversation",
        question="public synthetic question",
        now=now,
        emit=lambda _event: None,
    )
    recorder.flush()
    detail = store.get_trace(trace_id)

    assert outcome.answer.refusal_code is RefusalCode.NO_EVIDENCE
    assert detail.trace.mode is TraceMode.SAFE
    assert detail.artifacts == ()
    assert {span.name for span in detail.spans} == {
        "rag.query",
        "context.load",
        "rewrite.decide",
        "route.decide",
        "route.fallback",
        "retrieve",
        "embedding.query",
        "qdrant.q0.dense",
        "qdrant.q0.bm25",
        "rrf.fuse",
        "rerank",
        "neighbor.expand",
        "evidence.assemble",
        "llm.answer",
        "answer.validate",
        "answer.publish",
    }
    assert all(span.duration_ms is not None for span in detail.spans)
    spans_by_id = {span.span_id: span for span in detail.spans}
    for span in detail.spans:
        if span.parent_span_id is None:
            continue
        parent = spans_by_id[span.parent_span_id]
        assert span.finished_at is not None
        assert parent.finished_at is not None
        assert span.finished_at <= parent.finished_at
    assert b"public synthetic question" not in store.export_trace(trace_id)
    recorder.close()


def test_full_trace_persists_exact_input_without_changing_outcome(
    tmp_path: Path,
) -> None:
    plain = _service(tmp_path / "plain", recorder=None)
    (tmp_path / "full").mkdir()
    store = TraceStore(tmp_path / "full" / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    traced = _service(tmp_path / "full", recorder=recorder)
    now = datetime.now(UTC)

    plain_outcome = plain.ask(
        trace_id="b" * 32,
        conversation_id="conversation",
        question="public synthetic question",
        now=now,
        emit=lambda _event: None,
    )
    traced_outcome = traced.ask_debug(
        trace_id="c" * 32,
        conversation_id="conversation",
        question="public synthetic question",
        now=now,
        emit=lambda _event: None,
    )
    recorder.flush()
    detail = store.get_trace("c" * 32)
    context_metadata = next(
        artifact for artifact in detail.artifacts if artifact.kind == "context"
    )
    context = json.loads(
        store.get_artifact(
            "c" * 32,
            context_metadata.artifact_id,
            now=now,
        ).payload
    )

    assert traced_outcome.answer == plain_outcome.answer
    assert traced_outcome.rewritten == plain_outcome.rewritten
    assert traced_outcome.stage_count == plain_outcome.stage_count
    assert detail.trace.mode is TraceMode.FULL
    assert context["question"] == "public synthetic question"
    exported = store.export_trace("c" * 32)
    assert b"synthetic-secret-value" not in exported
    assert b"embedding_vector" not in exported
    assert b"ocr_base64" not in exported
    recorder.close()


def test_safe_trace_records_only_answer_shape_diagnostics(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    service = _service(
        tmp_path,
        recorder=recorder,
        answerer=_StructuredAnswerer(),
    )
    trace_id = "d" * 32

    service.ask(
        trace_id=trace_id,
        conversation_id="conversation",
        question="public synthetic question",
        now=datetime.now(UTC),
        emit=lambda _event: None,
    )
    recorder.flush()
    detail = store.get_trace(trace_id)
    answer_span = next(
        span for span in detail.spans if span.name == "llm.answer"
    )
    exported = store.export_trace(trace_id)
    recorder.close()

    assert answer_span.attributes["phase"] == "first"
    assert answer_span.attributes["endpoint"] == "http://10.0.0.1:8000"
    assert answer_span.attributes["retry_count"] == 0
    assert answer_span.attributes["claims_count"] == 0
    assert answer_span.attributes["validation_code"] == (
        "EVIDENCE_INSUFFICIENT"
    )
    assert set(answer_span.attributes) == {
        "phase",
        "endpoint",
        "retry_count",
        "claims_count",
        "validation_code",
    }
    assert "raw_output" not in answer_span.attributes
    assert b"must-not-export" not in exported


def test_safe_trace_records_abstention_review_without_business_content(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    service = _service(
        tmp_path,
        recorder=recorder,
        answerer=_AbstentionReviewAnswerer(),
    )
    trace_id = "f" * 32

    service.ask(
        trace_id=trace_id,
        conversation_id="conversation",
        question="private procedure question",
        now=datetime.now(UTC),
        emit=lambda _event: None,
    )
    recorder.flush()
    detail = store.get_trace(trace_id)
    spans = {span.name: span for span in detail.spans}
    exported = store.export_trace(trace_id)
    recorder.close()

    assert spans["answer.validate"].reason_code == (
        DecisionCode.MODEL_ABSTAINED
    )
    assert spans["answer.abstention_review"].reason_code == (
        DecisionCode.ABSTENTION_REVIEW_TRIGGERED
    )
    assert spans["llm.abstention_review"].reason_code == (
        DecisionCode.ABSTENTION_REVIEW_EMPTY
    )
    allowed_keys = {
        "intent",
        "evidence_count",
        "non_low_ocr_evidence_count",
        "claims_count",
        "phase",
        "endpoint",
        "retry_count",
        "validation_code",
        "review_triggered",
    }
    assert set(spans["llm.answer"].attributes) <= allowed_keys
    assert set(spans["answer.validate"].attributes) <= allowed_keys
    assert set(spans["answer.abstention_review"].attributes) <= allowed_keys
    assert set(spans["llm.abstention_review"].attributes) <= allowed_keys
    assert spans["llm.answer"].attributes["intent"] == "PROCEDURE"
    assert spans["llm.answer"].attributes["evidence_count"] == 3
    assert spans["llm.answer"].attributes["review_triggered"] is True
    assert b"private procedure question" not in exported
    assert b"raw_output" not in exported


def test_full_trace_keeps_raw_answer_only_in_artifact(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    service = _service(
        tmp_path,
        recorder=recorder,
        answerer=_StructuredAnswerer(),
    )
    trace_id = "e" * 32
    now = datetime.now(UTC)

    service.ask_debug(
        trace_id=trace_id,
        conversation_id="conversation",
        question="public synthetic question",
        now=now,
        emit=lambda _event: None,
    )
    recorder.flush()
    detail = store.get_trace(trace_id)
    answer_metadata = next(
        artifact for artifact in detail.artifacts if artifact.kind == "answer"
    )
    answer_artifact = store.get_artifact(
        trace_id,
        answer_metadata.artifact_id,
        now=now,
    ).payload
    recorder.close()

    assert b"must-not-export" in answer_artifact
    recorder.close()


def test_safe_trace_store_failure_does_not_change_query_outcome(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    store.close()
    failures: list[DecisionCode] = []
    recorder = TraceRecorder(
        store,
        audit_failure=lambda _trace_id, code: failures.append(code),
    )
    traced = _service(tmp_path / "traced", recorder=recorder)
    plain = _service(tmp_path / "plain", recorder=None)
    now = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

    traced_outcome = traced.ask(
        trace_id="d" * 32,
        conversation_id="conversation",
        question="public synthetic question",
        now=now,
        emit=lambda _event: None,
    )
    plain_outcome = plain.ask(
        trace_id="e" * 32,
        conversation_id="conversation",
        question="public synthetic question",
        now=now,
        emit=lambda _event: None,
    )
    recorder.flush()

    assert traced_outcome.answer == plain_outcome.answer
    assert traced_outcome.rewritten == plain_outcome.rewritten
    assert DecisionCode.TRACE_CAPTURE_FAILED in failures
    recorder.close()


def test_query_failure_keeps_completed_spans_and_failure_stage(
    tmp_path: Path,
) -> None:
    class _FailingAssembler:
        def assemble(self, ranked_hits: tuple[object, ...]) -> EvidenceBundle:
            del ranked_hits
            raise RuntimeError("synthetic failure")

    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    service = _service(
        tmp_path,
        recorder=recorder,
        assembler=_FailingAssembler(),
    )

    with pytest.raises(RuntimeError, match="synthetic"):
        service.ask(
            trace_id="f" * 32,
            conversation_id="conversation",
            question="public synthetic question",
            now=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
            emit=lambda _event: None,
        )
    recorder.flush()
    detail = store.get_trace("f" * 32)
    spans = {span.name: span for span in detail.spans}

    assert detail.trace.status is TraceStatus.FAILED
    assert detail.trace.error_code == "EVIDENCE_ASSEMBLE_FAILED"
    assert spans["rewrite.decide"].status.value == "OK"
    assert spans["retrieve"].status.value == "OK"
    assert spans["evidence.assemble"].status.value == "ERROR"
    assert (
        spans["evidence.assemble"].attributes["failure_stage"]
        == "evidence.assemble"
    )
    recorder.close()
