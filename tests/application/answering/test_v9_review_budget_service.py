"""真实回答服务的批量复核、共享补充名额与失败不发布边界。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from unittest.mock import Mock

import httpx
import pytest

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import QueryCancelled
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    QueryPlan,
)
from rag_app.core.models.retrieval import NaturalClaim
from tests.application.answering.test_grounded_claim_v5_quotes import _pack
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _draft,
    _evidence,
    _matrix,
    _plan,
)

_FIRST = "对于员工跨年领到证书的情况，请在证书领取年份进行报销。"
_SECOND = "员工在证书领取年份完成费用报销。"
_DIRECT = "甲部门负责归档记录。"


def _fixture(
    *, direct_first: bool = False
) -> tuple[QueryPlan, tuple[EvidenceItem, ...], tuple[NaturalClaim, ...]]:
    sources = (_DIRECT if direct_first else _FIRST, _SECOND)
    evidence = _evidence(*sources)
    by_text = {item.citation_text: item for item in evidence}
    plan = _plan(
        "甲部门" if direct_first else "费用跨年还能报吗？", "钱能放明年报不？"
    )
    claims = tuple(
        _claim(
            f"C{index}", source, f"A{index}", by_text[source].support_id, source
        )
        for index, source in enumerate(sources, 1)
    )
    return plan, evidence, claims


class _HttpHarness:
    """保存真实发送请求，第二次响应只能是repair或review之一。"""

    def __init__(
        self,
        claims: tuple[NaturalClaim, ...],
        *,
        repair: tuple[NaturalClaim, ...] | None = None,
        failure: str | None = None,
        after_send: Callable[[int], None] | None = None,
    ) -> None:
        self.sent: list[httpx.Request] = []
        self.claims = claims
        self.repair = repair
        self.failure = failure
        self.after_send = after_send
        self.adapter = OpenAICompatibleChatAdapter(
            OpenAICompatibleChatConfig(
                model="synthetic",
                egress_allowed=True,
                structured_output_mode="response_format",
            ),
            http_client=ProviderHttpClient(
                "https://provider.example/v1",
                client=httpx.Client(transport=httpx.MockTransport(self.handle)),
                max_attempts=1,
            ),
            api_key_resolver=lambda: "",
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        assert len(self.sent) <= 2, "generation和唯一补充之外不得发送第三次HTTP"
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])
        if self.after_send:
            self.after_send(len(self.sent))
        if len(self.sent) == 1:
            payload = {
                "claims": [
                    claim.model_dump(mode="json") for claim in self.claims
                ]
            }
        elif self.failure == "transport":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        elif self.failure == "json":
            return self.response("{invalid json")
        elif self.failure == "contract":
            return self.response(
                json.dumps({"results": [], "claim": "禁止的新事实"})
            )
        elif self.repair is not None:
            assert "candidates" not in data
            payload = {
                "claims": [
                    claim.model_dump(mode="json") for claim in self.repair
                ]
            }
        else:
            assert "candidates" in data
            source_quotes = {
                item["source_id"]: item["quotes"][0]
                for item in data["evidence"]
            }
            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": "supported",
                        "fact_source_ids": candidate["fact_source_ids"],
                        "source_scope": {
                            "relation_label": "报销",
                            "subject_anchors": [
                                {
                                    "source_id": source_id,
                                    "quote": source_quotes[source_id],
                                }
                                for source_id in candidate[
                                    "fact_source_ids"
                                ]
                            ],
                            "relation_anchors": [
                                {
                                    "source_id": source_id,
                                    "quote": source_quotes[source_id],
                                }
                                for source_id in candidate[
                                    "fact_source_ids"
                                ]
                            ],
                            "stage_anchors": [],
                            "condition_anchors": [
                                {
                                    "source_id": source_id,
                                    "quote": source_quotes[source_id],
                                }
                                for source_id in candidate[
                                    "fact_source_ids"
                                ]
                            ],
                        },
                    }
                    for candidate in data["candidates"]
                ]
            }
        return self.response(json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def response(content: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "synthetic",
                "choices": [
                    {"message": {"content": content}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 70,
                    "total_tokens": 270,
                },
            },
        )


def _run(
    generator: object,
    fixture: tuple[
        QueryPlan, tuple[EvidenceItem, ...], tuple[NaturalClaim, ...]
    ],
    *,
    cancellation: StreamCancellation | None = None,
) -> GroundedOutcome:
    plan, evidence, claims = fixture
    owners = {
        support.support_id: (claim.atom_id,)
        for claim in claims
        for support in claim.supports
    }
    return GroundedAnsweringService(generator).answer(
        plan.standalone_query,
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=plan,
        atom_support_matrix=_matrix(
            plan, tuple((AtomStatus.MISSING, ()) for _ in plan.atoms)
        ),
        generation_evidence_pack=_pack(
            plan, evidence, atom_ids_by_support=owners
        ),
        cancellation=cancellation,
    )


def test_multiple_unknown_claims_share_one_review_http() -> None:
    fixture = _fixture()
    harness = _HttpHarness(fixture[2])
    outcome = _run(harness.adapter, fixture)
    assert outcome.accepted_claim_count == 2
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert len(harness.sent) == len(outcome.prepared_packets) == 2
    assert sum(call.call_count for call in outcome.calls) == 2
    assert all(
        packet.evidence_level == "TRANSPORT_SENT"
        for packet in outcome.prepared_packets
    )
    second = json.loads(
        json.loads(harness.sent[1].content)["messages"][1]["content"]
    )
    assert len(second["candidates"]) == 2


def test_review_consumes_slot_even_when_another_atom_is_missing() -> None:
    fixture = _fixture()
    harness = _HttpHarness(fixture[2][:1])
    outcome = _run(harness.adapter, fixture)
    assert outcome.accepted_claim_count == 1
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert (
        outcome.repair_skip_reason == "SUPPLEMENT_SLOT_USED_BY_RELATION_REVIEW"
    )
    assert ("A2", "MISSING") in outcome.atom_coverage
    assert len(harness.sent) == len(outcome.prepared_packets) == 2


def test_unknown_repair_cannot_trigger_a_third_review_call() -> None:
    fixture = _fixture(direct_first=True)
    harness = _HttpHarness(fixture[2][:1], repair=fixture[2][1:])
    outcome = _run(harness.adapter, fixture)
    assert outcome.accepted_claim_count == 1
    assert outcome.repair_calls == 1
    assert outcome.relation_review_calls == 0
    assert len(harness.sent) == len(outcome.prepared_packets) == 2
    assert ("A2", "MISSING") in outcome.atom_coverage
    assert _SECOND not in (outcome.answer or "")


def test_unknown_without_sent_packet_is_not_reviewed_or_published() -> None:
    fixture = _fixture()
    generator = Mock()
    generator.generate.return_value = _draft(fixture[2], fixture[0])
    outcome = _run(generator, fixture)
    assert outcome.answer is None
    assert outcome.accepted_claim_count == outcome.published_claim_count == 0
    assert outcome.relation_review_calls == outcome.repair_calls == 0
    assert outcome.prepared_packets == ()
    assert generator.generate.call_count == 1


def test_expired_outer_deadline_prevents_review_and_publication() -> None:
    fixture = _fixture()
    cancellation = StreamCancellation()
    cancellation.deadline_monotonic = time.monotonic() - 1
    harness = _HttpHarness(fixture[2])
    outcome = _run(harness.adapter, fixture, cancellation=cancellation)
    assert outcome.answer is None
    assert outcome.relation_review_calls == outcome.repair_calls == 0
    assert outcome.relation_review_skip_reason == "DEADLINE_EXHAUSTED"
    assert len(harness.sent) == len(outcome.prepared_packets) == 1


@pytest.mark.parametrize("failure", ["json", "contract", "transport"])
def test_failed_review_records_attempt_but_never_publishes(
    failure: str,
) -> None:
    fixture = _fixture()
    harness = _HttpHarness(fixture[2], failure=failure)
    outcome = _run(harness.adapter, fixture)
    assert outcome.answer is None
    assert outcome.accepted_claim_count == outcome.published_claim_count == 0
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert len(harness.sent) == len(outcome.prepared_packets) == 2
    assert sum(call.call_count for call in outcome.calls) == 2


@pytest.mark.parametrize("failure", ["json", "contract", "transport"])
def test_failed_optional_review_keeps_already_valid_fact(
    failure: str,
) -> None:
    """补充复核失败只丢弃待定事实，不反向清空已通过的独立事实。"""
    fixture = _fixture(direct_first=True)
    harness = _HttpHarness(fixture[2], failure=failure)

    outcome = _run(harness.adapter, fixture)

    assert outcome.answer is not None
    assert _DIRECT in outcome.answer
    assert _SECOND not in outcome.answer
    assert outcome.accepted_claim_count == outcome.published_claim_count == 1
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert len(harness.sent) == len(outcome.prepared_packets) == 2


@pytest.mark.parametrize("cancel_on_send", [1, 2])
def test_cancelled_request_never_publishes_review_result(
    cancel_on_send: int,
) -> None:
    fixture = _fixture()
    cancellation = StreamCancellation()
    harness = _HttpHarness(
        fixture[2],
        after_send=lambda count: (
            cancellation.cancel() if count == cancel_on_send else None
        ),
    )
    with pytest.raises(QueryCancelled) as error:
        _run(harness.adapter, fixture, cancellation=cancellation)
    assert len(harness.sent) == cancel_on_send
    assert (
        sum(call.call_count for call in error.value.provider_calls)
        == cancel_on_send
    )


@pytest.mark.parametrize("boundary", ["role", "stage", "number", "source"])
def test_hard_boundary_never_reaches_supported_review_response(
    boundary: str,
) -> None:
    """即使第二次HTTP预置supported，明确硬矛盾也没有复核或发布许可。"""
    if boundary == "role":
        source = "乙部门负责归档记录。"
        text = source
        plan = _plan("甲部门")
    elif boundary == "stage":
        source = "验收后甲部门负责归档记录。"
        text = source
        plan = _plan("上线前甲部门负责哪些工作？")
    elif boundary == "number":
        source = "评测系统在上线前2-3个月安排兼容性检验。"
        text = "上线前3-4个月。"
        plan = _plan("评测系统", shape=AtomAnswerShape.DURATION)
    else:
        source = "甲部门负责归档记录。"
        text = source
        plan = _plan("甲部门")
        plan = plan.model_copy(
            update={
                "atoms": (
                    plan.atoms[0].model_copy(
                        update={"source_qualifier": "甲流程手册"}
                    ),
                )
            }
        )
    evidence = _evidence(source)
    if boundary == "source":
        evidence = (
            evidence[0].model_copy(update={"source_label": "乙流程手册"}),
        )
    claims = (_claim("C1", text, "A1", evidence[0].support_id, source),)
    fixture = (plan, evidence, claims)
    harness = _HttpHarness(claims)

    outcome = _run(harness.adapter, fixture)

    assert outcome.accepted_claim_count == outcome.published_claim_count == 0
    assert outcome.relation_review_calls == outcome.repair_calls == 0
    assert len(harness.sent) == len(outcome.prepared_packets) == 1
