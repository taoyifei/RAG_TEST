"""百炼回答和改写的离线 HTTP 合同，不证明实际模型质量。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from rag_app.adapters.providers.aliyun_chat import (
    AliyunChatAdapter,
    AliyunChatConfig,
    ChatMessage,
    chat_payload,
    message_token_estimate,
)
from rag_app.adapters.providers.budget_transport import BudgetedTransport
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.core.errors import (
    PolicyDenied,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
    ProviderUnavailable,
)
from rag_app.core.models import EvidenceItem, ProviderCall
from rag_app.core.models.chunk import SourceSpan
from rag_app.core.models.document import SourceAnchor, StoryKind
from rag_app.core.ports.generator import GenerationRequest


def _response(
    content: str = "只读合成回答",
    *,
    model: str = "qwen3.7-flash",
    finish: str = "stop",
    usage: object = None,
) -> dict[str, object]:
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": "禁止保存的原始模型思考",
                },
            }
        ],
        "usage": usage,
    }


def _adapter(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: AliyunChatConfig | None = None,
    observer: Callable[[ProviderCall], None] | None = None,
) -> AliyunChatAdapter:
    client = httpx.Client(
        transport=BudgetedTransport(
            httpx.MockTransport(handler),
            ledger_path=tmp_path / "budget.sqlite3",
        )
    )
    return AliyunChatAdapter(
        config or AliyunChatConfig(egress_allowed=True),
        http_client=ProviderHttpClient(
            "https://dashscope.aliyuncs.com",
            client=client,
            max_attempts=1,
            observer=observer,
            defer_success_observation=True,
        ),
        api_key_resolver=lambda: "public-synthetic-credential",
    )


def test_request_protocol_and_unknown_usage_are_not_fabricated(tmp_path: Path):
    requests: list[httpx.Request] = []
    events: list[ProviderCall] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response())

    adapter = _adapter(tmp_path, handler, observer=events.append)
    messages = (ChatMessage(role="user", content="合成设备如何归档？"),)
    try:
        result = adapter.complete(messages)
        payload = json.loads(requests[0].content)
        assert str(requests[0].url) == (
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        )
        assert payload["model"] == "qwen3.7-flash"
        assert payload["enable_thinking"] is False
        assert payload["max_tokens"] == 1536
        assert payload["stream"] is False
        assert "tools" not in payload and "extra_body" not in payload
        assert "response_format" not in payload
        assert requests[0].extensions["rag_chat_operation"] == "generation"
        assert result.call.estimated_tokens == message_token_estimate(messages)
        assert result.call.observed_tokens is None
        assert result.usage.total_tokens is None
        assert result.call.call_count == 1
        assert len(events) == 1
        serialized = result.call.model_dump_json()
        assert "合成设备" not in serialized
        assert "原始模型思考" not in serialized
        assert "public-synthetic-credential" not in serialized
        assert "只读合成回答" not in repr(result)
    finally:
        adapter.close()


def test_known_usage_and_rewrite_operation_are_preserved(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_response(
                usage={
                    "prompt_tokens": 21,
                    "completion_tokens": 7,
                    "total_tokens": 28,
                }
            ),
        )

    adapter = _adapter(tmp_path, handler)
    try:
        result = adapter.complete(
            (ChatMessage(role="user", content="合成问题"),),
            operation="query.rewrite",
            max_output_tokens=256,
        )
        assert result.usage.prompt_tokens == 21
        assert result.usage.completion_tokens == 7
        assert result.call.observed_tokens == 28
        assert result.call.operation == "query.rewrite"
        assert requests[0].extensions["rag_chat_operation"] == "query.rewrite"
        assert json.loads(requests[0].content)["max_tokens"] == 256
    finally:
        adapter.close()


@pytest.mark.parametrize("case", ["length", "wrong_model", "tool", "bad_usage"])
def test_response_failures_keep_calls_and_hide_body(tmp_path: Path, case: str):
    response = _response(usage={"total_tokens": 17})
    if case == "length":
        response = _response(finish="length", usage={"total_tokens": 17})
    elif case == "wrong_model":
        response["model"] = "different-model"
    elif case == "bad_usage":
        response["usage"] = {"total_tokens": True}
    else:
        choices: Any = response["choices"]
        choices[0]["message"]["tool_calls"] = [{"name": "do_not_execute"}]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response)

    adapter = _adapter(tmp_path, handler)
    try:
        with pytest.raises(ProviderInvalidResponse) as raised:
            adapter.complete((ChatMessage(role="user", content="合成问题"),))
        assert len(requests) == 1
        call = raised.value.provider_call
        assert call is not None and call.call_count == 1
        assert call.status_category == "RESPONSE_CONTRACT"
        assert "只读合成回答" not in str(raised.value)
        if case != "bad_usage":
            assert call.observed_tokens == 17
    finally:
        adapter.close()


def test_limits_and_egress_block_before_credentials_or_http(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response())

    disabled = _adapter(tmp_path, handler, config=AliyunChatConfig())
    limited = _adapter(
        tmp_path,
        handler,
        config=AliyunChatConfig(
            egress_allowed=True,
            max_input_tokens=20,
        ),
    )
    try:
        with pytest.raises(PolicyDenied):
            disabled.complete((ChatMessage(role="user", content="合成问题"),))
        with pytest.raises(ProviderInputTooLarge):
            limited.complete((ChatMessage(role="user", content="字" * 21),))
        assert not requests
    finally:
        disabled.close()
        limited.close()


def test_unknown_compatible_model_does_not_receive_qwen_fields():
    payload = chat_payload(
        (ChatMessage(role="user", content="合成问题"),),
        AliyunChatConfig(model="internal-compatible-chat"),
    )
    assert "enable_thinking" not in payload
    assert "response_format" not in payload


def _generation_request() -> GenerationRequest:
    return GenerationRequest(
        query="协调员负责什么？",
        citation_protocol="grounded-support-v1",
        evidence=(
            EvidenceItem(
                evidence_id="support-1",
                chunk_id="chunk_" + "1" * 32,
                source_label="合成角色表",
                citation_text="协调员 | 每周核对设备清单并登记异常。",
            ),
        ),
    )


def test_generate_binds_server_quotes_and_explicit_repair(tmp_path: Path):
    requests: list[httpx.Request] = []
    claim = {
        "text": "协调员每周核对清单并登记异常。",
        "supports": [
            {
                "support_id": "support-1",
                "quote": "每周核对设备清单并登记异常。",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_response(
                "```json\n"
                + json.dumps({"claims": [claim]}, ensure_ascii=False)
                + "\n```"
            ),
        )

    adapter = _adapter(tmp_path, handler)
    try:
        request = _generation_request().model_copy(
            update={
                "repair_reason": "CLAIM_NEGATION_UNSUPPORTED",
            }
        )
        result = adapter.generate(request)
        assert result.generation_mode == "llm"
        assert result.cited_evidence_ids == ("support-1",)
        assert result.text.endswith("[support-1]")
        assert (
            result.claims[0].supports[0].quote == claim["supports"][0]["quote"]
        )
        assert len(result.provider_calls) == len(requests) == 1
        assert "CLAIM_NEGATION_UNSUPPORTED" in requests[0].content.decode()
        assert "repair_reason" not in result.text
    finally:
        adapter.close()


@pytest.mark.parametrize(
    "claims",
    [
        [
            {
                "text": "无根据的输出",
                "supports": [{"support_id": "missing", "quote": "无根据"}],
            }
        ],
        [
            {
                "text": "无根据的输出",
                "supports": [
                    {"support_id": "support-1", "quote": "新增不存在事实"}
                ],
            }
        ],
    ],
)
def test_invalid_claim_is_not_silently_published_or_retried(
    tmp_path: Path,
    claims: object,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json=_response(json.dumps({"claims": claims}))
        )

    adapter = _adapter(tmp_path, handler)
    try:
        with pytest.raises(ProviderInvalidResponse) as raised:
            adapter.generate(_generation_request())
        assert len(requests) == 1
        assert raised.value.provider_call is not None
        assert "GENERATION_CLAIMS_INVALID" in str(raised.value.details)
    finally:
        adapter.close()


def test_empty_claims_are_explicit_model_abstention(tmp_path: Path):
    adapter = _adapter(
        tmp_path, lambda _: httpx.Response(200, json=_response('{"claims":[]}'))
    )
    try:
        draft = adapter.generate(_generation_request())
        assert draft.reason_code == "GENERATION_ABSTAINED"
        assert not draft.claims and not draft.cited_evidence_ids
        assert draft.provider_calls[0].call_count == 1
    finally:
        adapter.close()


def test_generation_exposes_source_rows_and_requires_joint_role_quotes(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response('{"claims":[]}'))

    base = _generation_request().evidence[0]
    evidence: list[EvidenceItem] = []
    for index, text in enumerate(
        ("值班主管", "统一协调现场资源。", "资料专员")
    ):
        path = ("body", "tbl:0", f"tr:{index // 2}", f"tc:{index % 2}", "p:0")
        anchor = SourceAnchor(
            part_uri="/word/document.xml",
            story_kind=StoryKind.BODY,
            structural_path=path,
            ordinal=index,
        )
        span = SourceSpan(
            node_id="node_" + str(index + 1) * 32,
            source_anchor=anchor,
            structural_path=path,
            chunk_start_char=0,
            chunk_end_char=len(text),
            source_start_char=0,
            source_end_char=len(text),
        )
        evidence.append(
            base.model_copy(
                update={
                    "evidence_id": f"S{index + 1}",
                    "citation_text": text,
                    "source_spans": (span,),
                    "table_locator": "public-table",
                    "document_version_id": "dver_" + "1" * 32,
                    "section_id": "public-section",
                }
            )
        )
    adapter = _adapter(tmp_path, handler)
    try:
        adapter.generate(
            _generation_request().model_copy(
                update={
                    "evidence": tuple(evidence),
                    "repair_reason": "CLAIM_OBJECT_CHANGED",
                }
            )
        )
        messages = json.loads(requests[0].content)["messages"]
        prompt = messages[0]["content"]
        assert "每条写明角色或对象的事实" in prompt
        assert "这两个ID的逐字quote" in prompt
        content = json.loads(messages[1]["content"])
        locations = [item["source_structure"] for item in content["evidence"]]
        assert locations[0]["document_version_id"] == "dver_" + "1" * 32
        assert locations[0]["table_locator"] == "public-table"
        assert locations[0]["anchors"][0]["structural_path"][2] == "tr:0"
        assert locations[1]["anchors"][0]["structural_path"][2] == "tr:0"
        assert locations[2]["anchors"][0]["structural_path"][2] == "tr:1"
        assert "CLAIM_OBJECT_CHANGED" in messages[2]["content"]
        assert len(requests) == 1
    finally:
        adapter.close()


def test_generation_transport_failure_has_no_automatic_retry(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            503, json={"error": {"message": "private response"}}
        )

    adapter = _adapter(tmp_path, handler)
    try:
        with pytest.raises(ProviderUnavailable) as raised:
            adapter.generate(_generation_request())
        assert len(requests) == 1
        assert raised.value.provider_call is not None
        assert raised.value.provider_call.retry_count == 0
        assert "private response" not in str(raised.value)
    finally:
        adapter.close()
