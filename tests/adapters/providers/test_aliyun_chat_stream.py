"""百炼真实 SSE 的 UTF-8、claim 增量与安全终态合同。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from rag_app.adapters.providers.aliyun_chat import (
    AliyunChatAdapter,
    AliyunChatConfig,
)
from rag_app.adapters.providers.budget_transport import BudgetedTransport
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.application.answering.grounded import GroundedAnsweringService
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import ProviderInvalidResponse, StreamDeliveryError
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    RetrievalPolicy,
)
from rag_app.core.ports import GenerationRequest
from tests.application.retrieval.test_descriptive_answers import (
    _candidates,
    _paragraph,
)


class _Chunks(httpx.SyncByteStream):
    """按测试指定边界提供原始响应字节。"""

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


def _request() -> GenerationRequest:
    return GenerationRequest(
        query="资料员负责什么？",
        citation_protocol="support-id-v1-claims",
        evidence=(
            EvidenceItem(
                evidence_id="support-1",
                chunk_id="chunk_" + "1" * 32,
                source_label="合成制度",
                citation_text="资料员每周核对设备清单。",
            ),
        ),
    )


def _event(
    *,
    content: str | None = None,
    finish: str | None = None,
    usage: object = None,
) -> bytes:
    choice = {"index": 0, "delta": {}, "finish_reason": finish}
    if content is not None:
        choice["delta"] = {"content": content}
    payload: dict[str, object] = {
        "model": "qwen3.7-flash",
        "choices": [choice],
    }
    if usage is not None:
        payload["usage"] = usage
    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\r\n\r\n"
    ).encode()


def _adapter(tmp_path: Path, body: bytes) -> AliyunChatAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        # 刻意在一个中文 UTF-8 字符中间切块。
        split = body.index("资".encode()) + 1
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            stream=_Chunks((body[:split], body[split:])),
        )

    client = httpx.Client(
        transport=BudgetedTransport(
            httpx.MockTransport(handler),
            ledger_path=tmp_path / "budget.sqlite3",
        )
    )
    return AliyunChatAdapter(
        AliyunChatConfig(egress_allowed=True),
        http_client=ProviderHttpClient(
            "https://dashscope.aliyuncs.com",
            client=client,
            max_attempts=1,
            defer_success_observation=True,
        ),
        api_key_resolver=lambda: "public-synthetic-credential",
    )


def test_stream_emits_complete_claim_before_finish_and_keeps_usage(
    tmp_path: Path,
) -> None:
    claim = {
        "text": "资料员每周核对设备清单。",
        "supports": [
            {
                "support_id": "support-1",
                "quote": "资料员每周核对设备清单。",
            }
        ],
    }
    content = json.dumps({"claims": [claim]}, ensure_ascii=False)
    midpoint = content.index("}") + 1
    body = b"".join(
        (
            _event(content=content[:midpoint]),
            _event(content=content[midpoint:]),
            _event(
                finish="stop",
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
            ),
            b"data: [DONE]\r\n\r\n",
        )
    )
    adapter = _adapter(tmp_path, body)
    emitted = []
    try:
        draft = adapter.generate_stream(
            _request(),
            on_claim=emitted.append,
            cancellation=StreamCancellation(),
        )
    finally:
        adapter.close()
    assert emitted == [draft.claims[0]]
    assert draft.text == "资料员每周核对设备清单。 [support-1]"
    assert draft.provider_calls[0].observed_tokens == 20


def test_stream_rejects_duplicate_claim_field_without_publishing(
    tmp_path: Path,
) -> None:
    content = (
        '{"claims":[{"text":"不得发布","text":"资料员每周核对设备清单。",'
        '"supports":[{"support_id":"support-1",'
        '"quote":"资料员每周核对设备清单。"}]}]}'
    )
    body = b"".join(
        (
            _event(content=content),
            _event(finish="stop"),
            b"data: [DONE]\n\n",
        )
    )
    adapter = _adapter(tmp_path, body)
    emitted = []
    try:
        with pytest.raises(ProviderInvalidResponse):
            adapter.generate_stream(
                _request(),
                on_claim=emitted.append,
                cancellation=StreamCancellation(),
            )
    finally:
        adapter.close()
    assert emitted == []


def test_invalid_later_claim_is_not_published_or_repaired_after_prefix(
    tmp_path: Path,
) -> None:
    source_text = "资料员每周核对设备清单。"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(source_text)),
        RetrievalPolicy(),
    )
    support_id = evidence[0].support_id
    content = json.dumps(
        {
            "claims": [
                {
                    "text": "资料员每周核对设备清单。",
                    "supports": [
                        {
                            "support_id": support_id,
                            "quote": source_text,
                        }
                    ],
                },
                {
                    "text": "资料员每月核对设备清单。",
                    "supports": [
                        {
                            "support_id": support_id,
                            "quote": source_text,
                        }
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )
    body = b"".join(
        (
            _event(content=content),
            _event(finish="stop"),
            b"data: [DONE]\n\n",
        )
    )
    adapter = _adapter(tmp_path, body)
    service = GroundedAnsweringService(adapter, Mock())
    emitted = []
    try:
        with pytest.raises(StreamDeliveryError) as captured:
            service.answer(
                "资料员多久核对一次设备清单？",
                evidence,
                ConfidenceDecision(
                    status=ConfidenceStatus.ANSWERABLE,
                    score=1.0,
                ),
                on_claim=emitted.append,
                cancellation=StreamCancellation(),
            )
    finally:
        adapter.close()
    assert [claim.text for claim in emitted] == ["资料员每周核对设备清单。"]
    assert len(captured.value.provider_calls) == 1
    assert captured.value.provider_calls[0].reason_code.startswith("CLAIM_")
