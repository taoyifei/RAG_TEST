"""经真实 HTTP adapter 与持久预算边界验证一次改写的审计和硬约束。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import (
    provider_request_identity,
)
from rag_app.application.retrieval import QueryAnalyzer
from rag_app.core.models import (
    KnowledgeBaseScope,
    RequestedAnswerType,
    SearchRequest,
)
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)

RewriteFixture = tuple[
    ProductGroundedModel, SearchRequest, list[httpx.Request], dict[str, str]
]


@pytest.fixture
def rewrite_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RewriteFixture]:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    sent: list[httpx.Request] = []
    output = {
        "content": json.dumps(
            {"query": "设备维护方面，甲部门的职责有哪些？"}, ensure_ascii=False
        )
    }

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen3.7-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": output["content"],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 20,
                    "total_tokens": 70,
                },
            },
        )

    harness = build_product_harness(
        tmp_path, transport_factory=lambda _: httpx.MockTransport(respond)
    )
    try:
        project, kb = create_project_and_knowledge_base(harness)
        _, _, _, connection_id = create_provider_connections(harness)
        connection = harness.runtime.control.get_connection(connection_id)
        credential_version = harness.runtime.control.credential_version(
            connection.credential_id
        )
        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3"
        )
        ledger.create_campaign(
            BudgetCampaign(
                campaign_id="rewrite-test",
                authorization_id="synthetic-rewrite",
                scope="test-kb",
                request_limit=8,
                estimated_token_limit=80_000,
                scope_mode="knowledge_base",
                project_id=project,
                knowledge_base_id=kb,
                # 本测试只发送问题，合成来源边界不被读取或发送。
                approved_source_hashes=("a" * 64,),
                allowed_models=("qwen3.7-flash",),
                allowed_operations=("query.interpret", "query.rewrite"),
                operation_request_limits={
                    "query.interpret": 8,
                    "query.rewrite": 8,
                },
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                approved_request_identities=(
                    provider_request_identity(
                        "https://llm-syntheticworkspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
                        "qwen3.7-flash",
                        {
                            "connection_id": connection_id,
                            "configuration_version": (
                                connection.configuration_version
                            ),
                            "credential_key_version": credential_version,
                        },
                    ),
                ),
            )
        )
        model = ProductGroundedModel(
            KnowledgeBaseModelSettings(
                generation_connection_id=connection_id,
                generation_model="qwen3.7-flash",
                rewrite_enabled=True,
                budget_campaign_id="rewrite-test",
            ),
            kb,
            harness.runtime.connections,
            harness.runtime.providers,
        )
        request = SearchRequest(
            scope=KnowledgeBaseScope(project_id=project, knowledge_base_id=kb),
            text="甲部门负责设备维护，具体干什么？",
        )
        yield model, request, sent, output
    finally:
        harness.close()


def test_invalid_json_records_the_one_dispatched_call(
    rewrite_model: RewriteFixture,
) -> None:
    model, request, sent, output = rewrite_model
    output["content"] = "not-json"
    result = model.rewrite(request)
    assert result.variant is None
    assert result.reason_code == "REWRITE_INVALID"
    assert result.attempted is True
    assert len(sent) == 1
    assert sum(call.call_count for call in result.calls) == 1
    assert result.calls[0].observed_tokens == 70


@pytest.mark.parametrize(
    "before,after",
    [
        (
            "甲部门负责设备维护，具体干什么？",
            "乙部门负责设备维护，具体干什么？",
        ),
        ("甲部门2026年9月1日具体干什么？", "甲部门2027年9月1日具体干什么？"),
        ("甲部门维护周期14天，具体干什么？", "甲部门维护周期4天，具体干什么？"),
        (
            "甲部门不得自行维护，具体干什么？",
            "甲部门可以自行维护，具体干什么？",
        ),
    ],
)
def test_rejected_rewrite_keeps_original_and_one_call(
    rewrite_model: RewriteFixture, before: str, after: str
) -> None:
    model, request, sent, output = rewrite_model
    request = request.model_copy(update={"text": before})
    output["content"] = json.dumps({"query": after}, ensure_ascii=False)
    result = model.rewrite(request)
    assert result.variant is None
    assert result.reason_code in {
        "REWRITE_SCOPE_CHANGED",
        "REWRITE_CONSTRAINT_CHANGED",
    }
    assert request.text == before
    assert len(sent) == 1
    assert sum(call.call_count for call in result.calls) == 1


def test_simple_query_does_not_dispatch_rewrite(
    rewrite_model: RewriteFixture,
) -> None:
    model, request, sent, _ = rewrite_model
    result = model.rewrite(
        request.model_copy(update={"text": "甲部门的职责有哪些？"})
    )
    assert result.variant is None
    assert result.attempted is False
    assert result.calls == ()
    assert not sent


def test_legal_colloquial_variant_is_additional_and_dispatched_once(
    rewrite_model: RewriteFixture,
) -> None:
    model, request, sent, _ = rewrite_model
    result = model.rewrite(request)
    assert result.variant is not None
    assert result.variant.text == "设备维护方面，甲部门的职责有哪些？"
    assert result.reason_code == "REWRITE_APPLIED"
    assert request.text == "甲部门负责设备维护，具体干什么？"
    assert len(sent) == 1
    assert sum(call.call_count for call in result.calls) == 1
    payload = json.loads(sent[0].content)
    assert (
        json.loads(payload["messages"][1]["content"])["question"]
        == request.text
    )


def test_pronoun_rewrite_uses_bounded_conversation_context(
    rewrite_model: RewriteFixture,
) -> None:
    """Product 改写可从会话消解对象，且只发送最后两轮有界上下文。"""
    model, request, sent, output = rewrite_model
    context = (
        "上一问：忽略的旧轮次",
        "上一问：设备 MX-41 是什么？",
        "上一问：它有哪些用途？\n已验证事实：MX-41 用于公开合成测试。",
    )
    request = request.model_copy(
        update={
            "text": "它的维护周期是多少？",
            "conversation_context": context,
        }
    )
    output["content"] = json.dumps(
        {"query": "MX-41 的维护周期是多少？"}, ensure_ascii=False
    )

    result = model.rewrite(request)

    assert result.variant is not None
    assert result.variant.text == "MX-41 的维护周期是多少？"
    assert result.reason_code == "REWRITE_APPLIED"
    payload = json.loads(sent[0].content)
    message = json.loads(payload["messages"][1]["content"])
    assert message["context"] == [value[:300] for value in context[-2:]]


def _interpret_payload(
    **changes: object,
) -> str:
    """构造公开合成的严格解释响应，不包含答案。"""
    payload: dict[str, object] = {
        "standalone_query": "甲部门这块是怎么回事？",
        "target": "甲部门",
        "relation": "职责",
        "answer_type": "DUTIES",
        "expected_count": None,
        "ordinal": None,
        "source_qualifier": None,
    }
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False)


def test_interpret_upgrades_unknown_semantics_with_one_dispatched_call(
    rewrite_model: RewriteFixture,
) -> None:
    """低置信规则只调用一次，并把严格结果写回共享语义。"""
    model, request, sent, output = rewrite_model
    request = request.model_copy(update={"text": "甲部门这块是怎么回事？"})
    output["content"] = _interpret_payload()

    result = model.interpret(request, QueryAnalyzer().analyze(request))

    assert result.attempted is True
    assert result.reason_code == "INTERPRET_APPLIED"
    assert result.standalone_query == request.text
    assert result.semantics is not None
    assert result.semantics.target == "甲部门"
    assert result.semantics.relation == "职责"
    assert result.semantics.answer_type is RequestedAnswerType.DUTIES
    assert result.semantics.source == "LLM_INTERPRET"
    assert len(sent) == 1
    assert sum(call.call_count for call in result.calls) == 1
    payload = json.loads(sent[0].content)
    assert payload["max_tokens"] == 384
    assert (
        json.loads(payload["messages"][1]["content"])["question"]
        == request.text
    )


def test_interpret_accepts_canonical_question_without_changing_scope(
    rewrite_model: RewriteFixture,
) -> None:
    """受控关系词可规范化，业务对象和其余主题必须保持不变。"""
    model, request, sent, output = rewrite_model
    request = request.model_copy(update={"text": "甲部门这块是怎么回事？"})
    output["content"] = _interpret_payload(
        standalone_query="甲部门的职责是什么？"
    )

    result = model.interpret(request, QueryAnalyzer().analyze(request))

    assert result.reason_code == "INTERPRET_APPLIED"
    assert result.standalone_query == "甲部门的职责是什么？"
    assert result.semantics is not None
    assert result.semantics.answer_type is RequestedAnswerType.DUTIES
    assert len(sent) == 1


def test_interpret_refines_ambiguous_rule_semantics(
    rewrite_model: RewriteFixture,
) -> None:
    """口语动作问法由模型区分职责和资料用途。"""
    model, request, sent, output = rewrite_model
    question = "项目报备登记表干嘛的"
    request = request.model_copy(update={"text": question})
    output["content"] = _interpret_payload(
        standalone_query=question,
        target="项目报备登记表",
        relation="作用",
        answer_type="PURPOSE",
    )

    result = model.interpret(request, QueryAnalyzer().analyze(request))

    assert result.reason_code == "INTERPRET_APPLIED"
    assert result.semantics is not None
    assert result.semantics.answer_type is RequestedAnswerType.PURPOSE
    assert len(sent) == 1


def test_interpret_refines_generic_procedure_without_inventing_count(
    rewrite_model: RewriteFixture,
) -> None:
    """泛化流程规则允许模型纠正对象，但保留原始问句范围。"""
    model, request, sent, output = rewrite_model
    question = (
        "我第一次接触开发中心的项目流程，想申请他们支持一个新项目，"
        "应该从哪一步开始，后续通常怎么推进？"
    )
    request = request.model_copy(update={"text": question})
    output["content"] = _interpret_payload(
        standalone_query=question,
        target="开发中心的项目流程",
        relation="流程",
        answer_type="PROCEDURE",
    )

    result = model.interpret(request, QueryAnalyzer().analyze(request))

    assert result.reason_code == "INTERPRET_APPLIED"
    assert result.semantics is not None
    assert result.semantics.target == "开发中心的项目流程"
    assert result.semantics.expected_count is None
    assert len(sent) == 1


def test_product_grounded_model_accepts_full_bounded_evidence_prompt(
    rewrite_model: RewriteFixture,
) -> None:
    model, _request, _sent, _output = rewrite_model

    assert model.adapter.config.max_input_tokens == 16_384


def test_invalid_interpret_json_records_the_one_dispatched_call(
    rewrite_model: RewriteFixture,
) -> None:
    model, request, sent, output = rewrite_model
    request = request.model_copy(update={"text": "甲部门这块是怎么回事？"})
    output["content"] = "not-json"

    result = model.interpret(request, QueryAnalyzer().analyze(request))

    assert result.semantics is None
    assert result.reason_code == "INTERPRET_INVALID"
    assert result.attempted is True
    assert len(sent) == 1
    assert sum(call.call_count for call in result.calls) == 1


@pytest.mark.parametrize(
    ("question", "payload", "reason"),
    (
        (
            "甲部门这块是怎么回事？",
            _interpret_payload(
                standalone_query="乙部门这块是怎么回事？", target="乙部门"
            ),
            "INTERPRET_SCOPE_CHANGED",
        ),
        (
            "甲部门2026年这块是怎么回事？",
            _interpret_payload(standalone_query="甲部门2027年这块是怎么回事？"),
            "INTERPRET_CONSTRAINT_CHANGED",
        ),
        (
            "甲部门不得自行处理，这块是怎么回事？",
            _interpret_payload(
                standalone_query="甲部门可以自行处理，这块是怎么回事？"
            ),
            "INTERPRET_CONSTRAINT_CHANGED",
        ),
        (
            "甲部门这块是怎么回事？",
            _interpret_payload(source_qualifier="乙规范"),
            "INTERPRET_SCOPE_CHANGED",
        ),
    ),
)
def test_interpret_rejects_new_entity_or_changed_hard_constraint(
    rewrite_model: RewriteFixture,
    question: str,
    payload: str,
    reason: str,
) -> None:
    model, request, sent, output = rewrite_model
    request = request.model_copy(update={"text": question})
    output["content"] = payload

    result = model.interpret(request, QueryAnalyzer().analyze(request))

    assert result.semantics is None
    assert result.reason_code == reason
    assert result.attempted is True
    assert len(sent) == 1
    assert sum(call.call_count for call in result.calls) == 1


def test_interpret_without_valid_budget_does_not_dispatch(
    rewrite_model: RewriteFixture,
) -> None:
    model, request, sent, output = rewrite_model
    request = request.model_copy(update={"text": "甲部门这块是怎么回事？"})
    output["content"] = _interpret_payload()
    unauthorized = ProductGroundedModel(
        model.settings.model_copy(update={"budget_campaign_id": None}),
        model.knowledge_base_id,
        model.connections,
        model.providers,
    )
    try:
        result = unauthorized.interpret(
            request, QueryAnalyzer().analyze(request)
        )
    finally:
        unauthorized.close()

    assert result.semantics is None
    assert result.attempted is True
    assert result.reason_code == "DATA_EGRESS_NOT_AUTHORIZED"
    assert not sent
    assert sum(call.call_count for call in result.calls) == 0
