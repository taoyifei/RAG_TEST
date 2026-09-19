"""窄关系复核使用真实 Provider 序列化、严格协议及实际发送包。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.core.errors import ProviderInvalidResponse
from rag_app.core.models.generation_packet import (
    PreparedGenerationPacket,
    stable_support_key,
)
from rag_app.core.models.query import QueryAnalysis
from rag_app.core.models.relation_review import (
    RelationReviewCandidate,
    RelationReviewRequest,
)
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
from tests.application.answering.test_natural_grounded_answer import (
    _evidence,
    _plan,
)


def _request() -> RelationReviewRequest:
    """通过原首次发送包为两个不确定 Claim 建立同一批次。"""
    evidence = _evidence("甲部门负责核对资料。", "甲部门负责归档记录。")
    plan = _plan("甲部门")
    packet = PreparedGenerationPacket(
        request_id="request-1",
        attempt_id="attempt-first",
        packet_id="packet-first",
        schema_revision="test",
        evidence_level="TRANSPORT_SENT",
        alias_to_support_key=tuple(
            (item.support_id, stable_support_key(item)) for item in evidence
        ),
        per_atom_support_ids=(
            ("A1", tuple(item.support_id for item in evidence)),
        ),
        original_support_keys=tuple(
            stable_support_key(item) for item in evidence
        ),
        messages_sha256="sha256:" + "0" * 64,
        transport_body_sha256="sha256:" + "1" * 64,
        estimated_input_tokens=100,
        max_input_tokens=6144,
        reserved_output_tokens=1536,
        safety_margin_tokens=128,
    )
    analysis = QueryAnalysis(
        original_query=plan.original_query,
        normalized_query=plan.original_query,
        conversation_fingerprint="sha256:" + "0" * 64,
    )
    return RelationReviewRequest(
        original_query=plan.original_query,
        evidence=evidence,
        sent_packet=packet,
        candidates=tuple(
            RelationReviewCandidate(
                claim_id=f"C{index}",
                atom=plan.atoms[0],
                analysis=analysis,
                claim=NaturalClaim(
                    atom_id="A1",
                    text=item.citation_text,
                    supports=(
                        ClaimSupport(
                            support_id=item.support_id, quote=item.citation_text
                        ),
                    ),
                ),
            )
            for index, item in enumerate(evidence, 1)
        ),
        request_id="request-1",
        attempt_id="attempt-review",
        deadline_monotonic=time.monotonic() + 10,
    )


def _payload(request: RelationReviewRequest) -> dict[str, object]:
    aliases = {
        item.support_id: f"E{index}"
        for index, item in enumerate(request.evidence, start=1)
    }
    return {
        "results": [
            {
                "claim_id": candidate.claim_id,
                "status": "supported",
                "fact_source_ids": [
                    aliases[support.support_id]
                    for support in candidate.claim.supports
                ],
                "source_scope": {
                    "relation_label": "职责",
                    "subject_anchors": [
                        {
                            "source_id": aliases[support.support_id],
                            "quote": support.quote,
                        }
                        for support in candidate.claim.supports[:1]
                    ],
                    "relation_anchors": [
                        {
                            "source_id": aliases[support.support_id],
                            "quote": support.quote,
                        }
                        for support in candidate.claim.supports
                    ],
                    "stage_anchors": [],
                    "condition_anchors": [],
                },
            }
            for candidate in request.candidates
        ]
    }


def _adapter(
    payload: dict[str, object],
    sent: list[httpx.Request],
    *,
    max_input_tokens: int = 6144,
) -> OpenAICompatibleChatAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "model": "synthetic",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(payload, ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 420,
                    "completion_tokens": 100,
                    "total_tokens": 520,
                },
            },
        )

    return OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic",
            egress_allowed=True,
            structured_output_mode="response_format",
            max_input_tokens=max_input_tokens,
        ),
        http_client=ProviderHttpClient(
            "https://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1,
        ),
        api_key_resolver=lambda: "",
    )


def test_batch_uses_one_http_and_records_schema_identity_usage() -> None:
    request = _request()
    sent: list[httpx.Request] = []
    response = _adapter(_payload(request), sent).review_relations(request)
    assert len(sent) == 1
    assert len(response.results) == 2
    assert response.purpose == "relation_review"
    assert response.call.operation == "generation"
    assert response.call.call_count == 1
    packet = response.prepared_packet
    assert packet.evidence_level == "TRANSPORT_SENT"
    assert packet.observed_prompt_tokens == 420
    assert packet.transport_body_sha256 is not None
    assert packet.schema_tokens > 0
    body = json.loads(sent[0].content)
    assert body["max_tokens"] <= 1024
    assert (
        body["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )
    assert (
        packet.alias_to_support_key == request.sent_packet.alias_to_support_key
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_id",
        "duplicate_id",
        "unknown_source",
        "unknown_scope_source",
        "rewritten_claim",
        "missing_claim",
        "missing_support",
        "invalid_status",
    ],
)
def test_strict_response_cannot_rewrite_or_expand_sources(
    mutation: str,
) -> None:
    request = _request()
    payload = _payload(request)
    results = payload["results"]
    assert isinstance(results, list)
    first = results[0]
    if mutation == "unknown_id":
        first["claim_id"] = "C999"
    elif mutation == "duplicate_id":
        results[1]["claim_id"] = first["claim_id"]
    elif mutation == "unknown_source":
        first["fact_source_ids"][0] = "E999"
    elif mutation == "unknown_scope_source":
        first["source_scope"]["relation_anchors"] = [
            {"source_id": "E999", "quote": "不存在的原文"}
        ]
    elif mutation == "rewritten_claim":
        first["claim"] = "模型擅自新增事实"
    elif mutation == "missing_claim":
        results.pop()
    elif mutation == "missing_support":
        first["fact_source_ids"] = []
    else:
        first["status"] = "probably_supported"
    sent: list[httpx.Request] = []
    with pytest.raises(ProviderInvalidResponse) as failure:
        _adapter(payload, sent).review_relations(request)
    assert len(sent) == 1
    assert (
        dict(failure.value.details)["reason_code"]
        == "RELATION_REVIEW_RESPONSE_INVALID"
    )
    assert (
        vars(failure.value)["_prepared_generation_packet"].evidence_level
        == "TRANSPORT_SENT"
    )


@pytest.mark.parametrize("budget", ["time", "input"])
def test_exhausted_budget_sends_no_http(budget: str) -> None:
    request = _request()
    if budget == "time":
        request = request.model_copy(
            update={"deadline_monotonic": time.monotonic() - 1}
        )
    sent: list[httpx.Request] = []
    adapter = _adapter(
        _payload(request),
        sent,
        max_input_tokens=1 if budget == "input" else 6144,
    )
    with pytest.raises(Exception) as failure:
        adapter.review_relations(request)
    assert sent == []
    packet = vars(failure.value)["_prepared_generation_packet"]
    assert packet.evidence_level == "PREPARATION_REJECTED"
    assert packet.transport_body_sha256 is None


@pytest.mark.parametrize(
    "change",
    [
        "unsent",
        "wrong_request",
        "same_attempt",
        "outside_atom",
        "changed_source",
    ],
)
def test_request_rejects_non_sent_identity_before_provider(change: str) -> None:
    request = _request()
    values = {
        name: getattr(request, name) for name in type(request).model_fields
    }
    if change == "unsent":
        values["sent_packet"] = request.sent_packet.model_copy(
            update={"evidence_level": "TRANSPORT_PREPARED"}
        )
    elif change == "wrong_request":
        values["request_id"] = "other-request"
    elif change == "same_attempt":
        values["attempt_id"] = request.sent_packet.attempt_id
    elif change == "outside_atom":
        values["sent_packet"] = request.sent_packet.model_copy(
            update={"per_atom_support_ids": (("A1", ()),)}
        )
    else:
        values["evidence"] = (
            request.evidence[0].model_copy(
                update={"citation_text": "已改变来源"}
            ),
            request.evidence[1],
        )
    with pytest.raises(ValueError, match="RELATION_REVIEW_"):
        RelationReviewRequest(**values)


@pytest.mark.parametrize(
    "configured,expected", [(2.0, 2.0), (90.0, 30.0), (None, 30.0)]
)
def test_supplement_cannot_expand_existing_http_timeout(
    configured: float | None, expected: float
) -> None:
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            timeout=configured,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        ),
    )
    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(model="synthetic", egress_allowed=True),
        http_client=client,
        api_key_resolver=lambda: "",
    )
    assert client.request_timeout_seconds == expected
    assert adapter.supplement_timeout_seconds == expected
