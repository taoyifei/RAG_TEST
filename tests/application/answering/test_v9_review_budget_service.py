"""真实回答服务的批量复核、共享补充名额与失败不发布边界。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

import rag_app.application.answering.grounded as grounded_module
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
)
from rag_app.application.answering.semantic_validation import (
    SemanticValidationRequest,
    SemanticValidationResponse,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import ProviderInputTooLarge, QueryCancelled
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    QueryPlan,
    SourceContentRequirement,
    SourceDocumentIdentity,
    SourceIntent,
    SourceResolution,
    SourceScopeDecision,
)
from rag_app.core.models.retrieval import NaturalClaim
from tests.application.answering.test_evidence_binding import (
    _fixture as _table_fixture,
)
from tests.application.answering.test_grounded_claim_v5_quotes import _pack
from tests.application.answering.test_natural_grounded_answer import (
    _answer,
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


class _PreparationFailingAdapter(OpenAICompatibleChatAdapter):
    """在发送语义复核 HTTP 前模拟本地输入预算拒绝。"""

    def review_semantics(
        self, request: SemanticValidationRequest
    ) -> SemanticValidationResponse:
        del request
        raise ProviderInputTooLarge(
            "synthetic preparation rejection",
            stage="generation.semantic_review",
            code="SEMANTIC_REVIEW_INPUT_BUDGET_EXCEEDED",
        )


class _BatchSplittingAdapter(OpenAICompatibleChatAdapter):
    """只允许单 Claim 复核，用于证明两批互斥且不重复自审。"""

    def review_semantics(
        self, request: SemanticValidationRequest
    ) -> SemanticValidationResponse:
        if len(request.candidates) > 1:
            raise ProviderInputTooLarge(
                "synthetic actual batch budget rejection",
                stage="generation.semantic_review",
                code="SEMANTIC_REVIEW_INPUT_BUDGET_EXCEEDED",
            )
        return super().review_semantics(request)


class _HttpHarness:
    """保存真实发送请求，复核最多拆成两个互斥批次。"""

    def __init__(  # noqa: PLR0913
        self,
        claims: tuple[NaturalClaim, ...],
        *,
        max_input_tokens: int = 6144,
        failure: str | None = None,
        statuses: tuple[str, ...] | None = None,
        fail_review_preparation: bool = False,
        force_split_review: bool = False,
    ) -> None:
        self.sent: list[httpx.Request] = []
        self.claims = claims
        self.failure = failure
        self.statuses = statuses
        self.after_send: Callable[[int], None] | None = None
        self.append_bad_wire_item = False
        adapter_type = (
            _BatchSplittingAdapter
            if force_split_review
            else _PreparationFailingAdapter
            if fail_review_preparation
            else OpenAICompatibleChatAdapter
        )
        self.adapter = adapter_type(
            OpenAICompatibleChatConfig(
                model="synthetic",
                egress_allowed=True,
                max_input_tokens=max_input_tokens,
                disable_thinking_supported=True,
                disable_thinking=True,
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
        assert len(self.sent) <= 3, "生成和至多两个互斥复核批之外不得发送HTTP"
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        data = json.loads(body["messages"][1]["content"])
        if self.after_send:
            self.after_send(len(self.sent))
        if len(self.sent) == 1:
            read_units = data["read_units"]
            allowed_by_atom = {
                atom["atom_id"]: set(atom["allowed_ref_ids"])
                for atom in data["atoms"]
            }
            payload = {
                "claims": [
                    {
                        "atom_id": claim.atom_id,
                        "text": claim.text,
                        "refs": [
                            unit["unit_id"]
                            for unit in read_units
                            if unit["unit_id"] in allowed_by_atom[claim.atom_id]
                            and any(
                                support.quote in unit["text"]
                                or unit["text"] in support.quote
                                for support in claim.supports
                            )
                        ],
                    }
                    for claim in self.claims
                ]
            }
            if self.append_bad_wire_item:
                payload["claims"].append(
                    {
                        "atom_id": "A1",
                        "text": "不得发布的坏条目",
                        "refs": ["E999"],
                    }
                )
        elif self.failure == "transport":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        elif self.failure == "json":
            return self.response("{invalid json")
        elif self.failure == "contract":
            return self.response(
                json.dumps({"results": [], "claim": "禁止的新事实"})
            )
        else:
            assert "candidates" in data
            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": self.statuses[index]
                        if self.statuses is not None
                        else "supported",
                    }
                    for index, candidate in enumerate(data["candidates"])
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


def test_source_scope_digest_survives_generation_and_review_packets() -> None:
    plan, evidence, claims = _fixture(direct_first=True)
    item = evidence[0]
    assert item.document_id is not None
    assert item.document_version_id is not None
    scope = SourceScopeDecision(
        atom_id="A1",
        source_intent=SourceIntent.DOCUMENT_AUTHORITY,
        resolution=SourceResolution.RESOLVED,
        allowed_documents=(
            SourceDocumentIdentity(
                document_id=item.document_id,
                document_version_id=item.document_version_id,
            ),
        ),
        required_content=SourceContentRequirement.BODY,
        mention_sha256=canonical_sha256("甲流程手册"),
        registry_revision="test-registry-v1",
        scope_digest=canonical_sha256("source-scope"),
    )
    plan = plan.model_copy(
        update={
            "atoms": (
                plan.atoms[0].model_copy(update={"source_scope": scope}),
                plan.atoms[1],
            )
        }
    )
    harness = _HttpHarness(claims[:1])

    outcome = _run(harness.adapter, (plan, evidence, claims))

    assert len(outcome.prepared_packets) == 2
    assert all(
        dict(packet.per_atom_source_scope_digests)["A1"] == scope.scope_digest
        for packet in outcome.prepared_packets
    )


def test_wire_path_never_calls_legacy_lexical_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新 Provider 路径只做绑定与一次语义复核，不回到旧硬门。"""

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("新 Wire 路径调用了旧词面裁决")

    monkeypatch.setattr(grounded_module, "_validated_natural_claim", forbidden)
    fixture = _fixture()
    harness = _HttpHarness(fixture[2])

    outcome = _run(harness.adapter, fixture)

    assert outcome.accepted_claim_count == 2
    assert outcome.repair_calls == 0
    assert outcome.relation_review_calls == 1
    assert len(harness.sent) == 2


def test_supported_review_cannot_upgrade_incomplete_table_relation() -> None:
    """复核一律 supported 时，服务端仍按物理关系缺口保持 PARTIAL。"""
    evidence, fact, _unit, binding = _table_fixture()
    replacements = {
        "S1": "需求快验",
        "S2": "产品需求文档",
        "S3": "交互原型",
        "S4": "业务规则",
        "S5": "接口清单",
        "S6": "数据字典",
        "S7": "验收标准",
        "S8": "参与人员",
        "S9": "输入",
        "S10": "（业务团队 / 外部单位需提供）",
    }
    evidence = tuple(
        item.model_copy(update={"citation_text": replacements[item.support_id]})
        for item in evidence
    )
    assert evidence[0].document_id is not None
    assert evidence[0].document_version_id is not None
    fact = fact.model_copy(
        update={
            "document_id": evidence[0].document_id,
            "document_version_id": evidence[0].document_version_id,
        }
    )
    plan = _plan("做快验前到底得备齐啥？")
    binding = binding.model_copy(
        update={
            "requested_target": plan.atoms[0].target,
            "requested_relation": plan.atoms[0].relation,
            "relation_status": "UNDETERMINED",
        }
    )
    claim = _claim(
        "C1",
        "做快验之前必须备齐全部输入。",
        "A1",
        "S2",
        "产品需求文档",
    )
    harness = _HttpHarness((claim,), statuses=("supported",))
    pack = replace(
        _pack(plan, evidence),
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    outcome = GroundedAnsweringService(harness.adapter).answer(
        plan.standalone_query,
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=plan,
        atom_support_matrix=_matrix(
            plan,
            (
                (
                    AtomStatus.SUPPORTED,
                    tuple(item.support_id for item in evidence),
                ),
            ),
        ),
        generation_evidence_pack=pack,
    )

    assert len(harness.sent) == 2
    assert outcome.accepted_claim_count == outcome.published_claim_count == 1
    assert outcome.atom_coverage == (("A1", "PARTIAL"),)
    assert outcome.missing_atom_reasons == (("A1", "EVIDENCE_NOT_DIRECT"),)
    assert "需求快验" in (outcome.answer or "")
    assert "之前必须" not in (outcome.answer or "")
    assert outcome.source_projection_records
    projection = dict(outcome.source_projection_records[0])
    assert projection["render_origin"] == "physical_table_fact"
    assert projection["relation_gap"] is True


def test_bad_wire_item_does_not_delete_independent_valid_claim() -> None:
    fixture = _fixture()
    harness = _HttpHarness(fixture[2][:1])
    harness.append_bad_wire_item = True

    outcome = _run(harness.adapter, fixture)

    assert outcome.answer is not None
    assert _FIRST in outcome.answer
    assert "不得发布的坏条目" not in outcome.answer
    assert outcome.accepted_claim_count == outcome.published_claim_count == 1
    assert tuple(item.failure_code for item in outcome.wire_diagnostics) == (
        "UNKNOWN_OR_OUT_OF_SCOPE_REF",
    )
    assert outcome.repair_calls == 0
    assert outcome.relation_review_calls == 1
    assert len(harness.sent) == 2


def test_missing_atom_does_not_trigger_generation_repair() -> None:
    fixture = _fixture()
    harness = _HttpHarness(fixture[2][:1])
    outcome = _run(harness.adapter, fixture)
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert outcome.repair_skip_reason == "AUTOMATIC_GENERATION_REPAIR_DISABLED"
    assert ("A2", "MISSING") in outcome.atom_coverage
    assert len(harness.sent) == len(outcome.prepared_packets) == 2


def test_single_claim_uses_only_generation_and_one_review() -> None:
    fixture = _fixture(direct_first=True)
    harness = _HttpHarness(fixture[2][:1])
    outcome = _run(harness.adapter, fixture)
    assert outcome.accepted_claim_count == 1
    assert outcome.repair_calls == 0
    assert outcome.relation_review_calls == 1
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
    assert outcome.relation_review_skip_reason == (
        "SEMANTIC_REVIEW_DEADLINE_EXHAUSTED"
    )
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

    assert outcome.accepted_claim_count == outcome.published_claim_count == 0
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert len(harness.sent) == len(outcome.prepared_packets) == 2


def test_all_semantic_claims_rejected_has_precise_terminal_reason() -> None:
    fixture = _fixture()
    harness = _HttpHarness(fixture[2], statuses=("contradicted", "unknown"))

    outcome = _run(harness.adapter, fixture)

    assert outcome.answer is None
    assert outcome.reason_code == "SEMANTIC_REVIEW_NO_SUPPORTED_CLAIM"
    assert outcome.accepted_claim_count == outcome.published_claim_count == 0
    assert outcome.repair_calls == 0
    assert len(harness.sent) == 2


def test_review_preparation_failure_does_not_consume_repair_slot() -> None:
    fixture = _fixture(direct_first=True)
    harness = _HttpHarness(
        fixture[2],
        fail_review_preparation=True,
    )

    outcome = _run(harness.adapter, fixture)

    assert outcome.relation_review_calls == 0
    assert outcome.repair_calls == 0
    assert outcome.repair_skip_reason == "AUTOMATIC_GENERATION_REPAIR_DISABLED"
    assert len(harness.sent) == 1
    assert len(outcome.prepared_packets) == 1


def test_generation_budget_depends_only_on_actual_generation_packet() -> None:
    """生成预算不再为尚未出现的假想 Claim 反向裁剪。"""
    fixture = _fixture()
    harness = _HttpHarness(fixture[2], max_input_tokens=4000)

    outcome = _run(harness.adapter, fixture)

    assert outcome.accepted_claim_count == 2
    assert len(harness.sent) == 2
    first_body = json.loads(harness.sent[0].content)
    first_reserved = outcome.prepared_packets[0].reserved_output_tokens
    assert first_body["max_tokens"] == first_reserved == 1536
    assert outcome.prepared_packets[1].estimated_input_tokens <= 4000
    review_body = json.loads(
        json.loads(harness.sent[1].content)["messages"][1]["content"]
    )
    assert [item["atom_id"] for item in review_body["tasks"]] == [
        "A1",
        "A2",
    ]
    assert all(
        "question" not in candidate for candidate in review_body["candidates"]
    )


def test_actual_claims_replace_fixed_semantic_preflight_shells() -> None:
    """只用真实 Claim 构造复核，不再因固定 24 条空壳阻断生成。"""
    fixture = _fixture()
    harness = _HttpHarness(fixture[2], max_input_tokens=3800)

    outcome = _run(harness.adapter, fixture)

    assert outcome.answer is not None
    assert outcome.accepted_claim_count == 2
    assert len(harness.sent) == 2
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0


def test_two_actual_review_batches_are_disjoint() -> None:
    fixture = _fixture()
    harness = _HttpHarness(fixture[2], force_split_review=True)

    outcome = _run(harness.adapter, fixture)

    assert outcome.accepted_claim_count == 2
    assert outcome.relation_review_calls == 2
    assert len(harness.sent) == 3
    review_bodies = tuple(
        json.loads(request.content)["messages"][1]["content"]
        for request in harness.sent[1:]
    )
    batches = tuple(
        {item["claim_id"] for item in json.loads(body)["candidates"]}
        for body in review_bodies
    )
    assert batches == ({"C1"}, {"C2"})


def test_hard_failure_on_a2_does_not_change_a1_repair_eligibility() -> None:
    evidence = _evidence(
        "甲部门负责归档记录。",
        "乙部门负责复核记录。",
    )
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲部门", "乙部门")
    wrong_a2 = _claim(
        "C1",
        "丙部门负责复核记录。",
        "A2",
        by_text["乙部门负责复核记录。"],
        "乙部门负责复核记录。",
    )
    repaired_a1 = _claim(
        "C2",
        "甲部门负责归档记录。",
        "A1",
        by_text["甲部门负责归档记录。"],
    )
    generator = Mock()
    generator.generate.side_effect = (
        _draft((wrong_a2,), plan),
        _draft((repaired_a1,), plan),
    )

    outcome = _answer(
        generator,
        evidence,
        plan,
        _matrix(
            plan,
            (
                (
                    AtomStatus.SUPPORTED,
                    (by_text["甲部门负责归档记录。"],),
                ),
                (
                    AtomStatus.SUPPORTED,
                    (by_text["乙部门负责复核记录。"],),
                ),
            ),
        ),
    )

    assert outcome.answer is not None
    assert _DIRECT in outcome.answer
    assert outcome.repair_calls == 1
    repair_request = generator.generate.call_args_list[1].args[0]
    assert repair_request.repair_atom_ids == ("A1",)


@pytest.mark.parametrize("cancel_on_send", [1, 2])
def test_cancelled_request_never_publishes_review_result(
    cancel_on_send: int,
) -> None:
    fixture = _fixture()
    cancellation = StreamCancellation()
    harness = _HttpHarness(fixture[2])
    harness.after_send = lambda count: (
        cancellation.cancel() if count == cancel_on_send else None
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
    harness = _HttpHarness(claims, statuses=("contradicted",))

    outcome = _run(harness.adapter, fixture)

    assert outcome.accepted_claim_count == outcome.published_claim_count == 0
    assert outcome.relation_review_calls == 1
    assert outcome.repair_calls == 0
    assert len(harness.sent) == len(outcome.prepared_packets) == 2
