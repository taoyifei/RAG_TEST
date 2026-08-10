import json
from collections.abc import Iterator

import httpx

from rag_app.chunking import Utf8TokenCounter
from rag_app.clients.llm import BufferedLlmClient
from rag_app.clients.resilience import (
    ResiliencePolicy,
    ResilientHttpPool,
    StreamCancellation,
)
from rag_app.generation.answer import (
    AnswerClaim,
    AnswerConfig,
    AnswerGenerator,
    AnswerMode,
    AnswerStatus,
)
from rag_app.generation.evidence import (
    EvidenceAssembler,
    EvidenceBundle,
    EvidenceConfig,
)
from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.rerank import RerankedHit

_MODEL = "Qwen/Qwen3-8B-AWQ"


class _SequencedStream(httpx.SyncByteStream):
    def __init__(
        self,
        chunks: list[tuple[str, bytes]],
        phases: list[str],
    ) -> None:
        self._chunks = chunks
        self._phases = phases

    def __iter__(self) -> Iterator[bytes]:
        for phase, chunk in self._chunks:
            self._phases.append(phase)
            yield chunk


def _bundle(text: str) -> EvidenceBundle:
    locator = {
        "file_path": "规范.docx",
        "heading_path": ["需求变更"],
        "paragraph_index": 1,
        "fragment": text,
    }
    hit = RerankedHit(
        rank=1,
        rerank_score=1.0,
        hit=FusedHit(
            chunk_id="chunk-1",
            rrf_score=0.1,
            channel_ranks=(("q0:dense", 1),),
            payload={
                "chunk_id": "chunk-1",
                "source_id": "source-1",
                "neighbor_group_id": "neighbor-1",
                "text": text,
                "embedding_text": text,
                "locators": [locator],
                "source_spans": [
                    {
                        "element_id": "element-1",
                        "locator": locator,
                        "start_char": 0,
                        "end_char": len(text),
                        "source_start_char": 0,
                        "source_end_char": len(text),
                        "is_repeated": False,
                    }
                ],
                "contains_ocr": False,
                "minimum_ocr_confidence": None,
            },
        ),
    )
    return EvidenceAssembler(
        Utf8TokenCounter(),
        EvidenceConfig(
            max_evidence_tokens=2048,
            max_items=4,
            low_ocr_threshold=0.8,
        ),
    ).assemble((hit,))


def _multi_anchor_bundle() -> EvidenceBundle:
    hits: list[RerankedHit] = []
    for rank, (source, text) in enumerate(
        (
            ("GM-07", "技术文件原稿由品质部归档保存。"),
            ("GM-09", "仓库物品应离地、离墙存放。"),
        ),
        start=1,
    ):
        locator = {
            "file_path": f"{source} 管理制度.docx",
            "heading_path": ["要求"],
            "paragraph_index": rank,
            "fragment": text,
        }
        hits.append(
            RerankedHit(
                rank=rank,
                rerank_score=1.0,
                hit=FusedHit(
                    chunk_id=f"chunk-{rank}",
                    rrf_score=0.1,
                    channel_ranks=(("q0:dense", rank),),
                    payload={
                        "chunk_id": f"chunk-{rank}",
                        "source_id": f"source-{rank}",
                        "neighbor_group_id": f"neighbor-{rank}",
                        "text": text,
                        "embedding_text": text,
                        "locators": [locator],
                        "source_spans": [
                            {
                                "element_id": f"element-{rank}",
                                "locator": locator,
                                "start_char": 0,
                                "end_char": len(text),
                                "source_start_char": 0,
                                "source_end_char": len(text),
                                "is_repeated": False,
                            }
                        ],
                        "contains_ocr": False,
                        "minimum_ocr_confidence": None,
                    },
                ),
            )
        )
    return EvidenceAssembler(
        Utf8TokenCounter(),
        EvidenceConfig(
            max_evidence_tokens=2048,
            max_items=4,
            low_ocr_threshold=0.8,
        ),
    ).assemble(tuple(hits))


def _sse(payload: object) -> bytes:
    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode()


def _delta(content: str, *, finish_reason: str | None = None) -> object:
    return {
        "model": _MODEL,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": None,
    }


def _usage() -> object:
    return {
        "model": _MODEL,
        "choices": [],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }


def _stream_response(
    deltas: list[tuple[str, str]],
    phases: list[str],
) -> httpx.Response:
    chunks = [
        (phase, _sse(_delta(content))) for phase, content in deltas
    ]
    chunks.extend(
        (
            ("finish", _sse(_delta("", finish_reason="stop"))),
            ("usage", _sse(_usage())),
            ("done", b"data: [DONE]\n\n"),
        )
    )
    return httpx.Response(200, stream=_SequencedStream(chunks, phases))


def _buffered_response(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": _MODEL,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(payload, ensure_ascii=False)
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        },
    )


def _generator(handler: object) -> AnswerGenerator:
    client = BufferedLlmClient(
        ResilientHttpPool(
            ("http://llm",),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            policy=ResiliencePolicy(
                max_attempts=2,
                failure_threshold=1,
                cooldown_seconds=30,
                max_concurrency=2,
            ),
        ),
        model=_MODEL,
        max_context_tokens=8192,
        api_token=None,
    )
    return AnswerGenerator(
        client,
        AnswerConfig(max_output_tokens=768, max_repair_tokens=384),
    )


def test_streaming_answer_emits_only_complete_validated_claims() -> None:
    evidence = _bundle("需求变更应提交书面申请并完成影响评估。")
    valid = json.dumps(
        {
            "text": "需求变更应提交书面申请。",
            "support_ids": ["E1:S1"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    unsupported = json.dumps(
        {
            "text": "需求变更应在2099年完成。",
            "support_ids": ["E1:S1"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    phases: list[str] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return _stream_response(
            [
                ("valid-claim", '{"claims":[' + valid + ","),
                ("invalid-claim", unsupported + "]}"),
            ],
            phases,
        )

    emitted: list[tuple[str, AnswerClaim]] = []
    result = _generator(handler).answer_stream(
        "需求变更需要经过哪些步骤？",
        evidence,
        rerank_scores=(1.0, 0.99),
        on_claim=lambda claim: emitted.append((phases[-1], claim)),
        cancellation=StreamCancellation(),
    )

    assert [(phase, claim.text) for phase, claim in emitted] == [
        ("valid-claim", "需求变更应提交书面申请。")
    ]
    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.PARTIAL
    assert result.model_calls == 1
    assert result.trace["llm_stream"] is True
    assert result.trace["selected_endpoint"] == "http://llm"
    assert result.trace["first_delta_ms"] is not None
    assert result.trace["delta_count"] == 2
    assert result.trace["validated_claim_count"] == 1
    assert result.trace["dropped_claim_count"] == 1
    assert result.trace["first_validated_claim_ms"] is not None
    assert result.trace["stream_cancelled"] is False
    assert result.trace["stream_finish_reason"] == "stop"
    assert result.trace["retry_count"] == 0
    assert result.trace["repair_triggered"] is False


def test_streaming_answer_with_no_valid_claim_uses_one_buffered_repair(
) -> None:
    evidence = _bundle("需求变更应提交书面申请。")
    request_modes: list[bool] = []
    invalid = json.dumps(
        {
            "claims": [
                {
                    "text": "无效回答。",
                    "support_ids": ["E99:S99"],
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_modes.append(payload["stream"])
        if payload["stream"] is True:
            return _stream_response([("invalid", invalid)], [])
        return _buffered_response(
            {
                "claims": [
                    {
                        "text": "需求变更应提交书面申请。",
                        "support_ids": ["E1:S1"],
                    }
                ]
            }
        )

    emitted: list[AnswerClaim] = []
    result = _generator(handler).answer_stream(
        "需求变更需要经过哪些步骤？",
        evidence,
        rerank_scores=(1.0, 0.99),
        on_claim=emitted.append,
        cancellation=StreamCancellation(),
    )

    assert emitted == []
    assert result.status is AnswerStatus.ANSWERED
    assert result.model_calls == 2
    assert result.trace["repair_triggered"] is True
    assert request_modes == [True, False]


def test_streaming_does_not_emit_unsupported_named_process_claim() -> None:
    evidence = _bundle("来料急需时，经批准后可以例外放行。")
    invalid = json.dumps(
        {
            "claims": [
                {
                    "text": "《需求快验流程》允许部分环节灵活处理。",
                    "support_ids": ["E1:S1"],
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    request_modes: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_modes.append(payload["stream"])
        if payload["stream"] is True:
            return _stream_response([("invalid", invalid)], [])
        return _buffered_response({"claims": []})

    emitted: list[AnswerClaim] = []
    result = _generator(handler).answer_stream(
        "《需求快验流程》中哪些环节可以灵活处理？",
        evidence,
        rerank_scores=(1.0,),
        on_claim=emitted.append,
        cancellation=StreamCancellation(),
    )

    assert emitted == []
    assert result.status is AnswerStatus.REFUSED
    assert result.model_calls == 2
    assert result.trace["first_validation_code"] == (
        "UNSUPPORTED_QUESTION_ANCHOR"
    )
    assert result.trace.get("extractive_fallback") is None
    assert request_modes == [True, False]


def test_multi_anchor_stream_waits_until_all_anchors_are_covered() -> None:
    evidence = _multi_anchor_bundle()
    first = json.dumps(
        {
            "text": "技术文件原稿由品质部归档保存。",
            "support_ids": ["E1:S1"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    second = json.dumps(
        {
            "text": "仓库物品应离地、离墙存放。",
            "support_ids": ["E2:S1"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    phases: list[str] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return _stream_response(
            [
                ("first-anchor", '{"claims":[' + first + ","),
                ("second-anchor", second + "]}"),
            ],
            phases,
        )

    emitted: list[tuple[str, str]] = []
    result = _generator(handler).answer_stream(
        "GM-07 和 GM-09 有什么不同？",
        evidence,
        rerank_scores=(1.0, 0.99),
        on_claim=lambda claim: emitted.append((phases[-1], claim.text)),
        cancellation=StreamCancellation(),
    )

    assert emitted == [
        ("second-anchor", "技术文件原稿由品质部归档保存。"),
        ("second-anchor", "仓库物品应离地、离墙存放。"),
    ]
    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.SOURCE_SEPARATED
