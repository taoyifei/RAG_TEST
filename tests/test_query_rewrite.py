import json

import httpx
import pytest

from rag_app.chunking import Utf8TokenCounter
from rag_app.clients.llm import BufferedLlmClient
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.model_contracts import (
    VerifiedClaimContext,
    VerifiedClaimSupport,
)
from rag_app.retrieval.rewrite import (
    QueryRewriteConfig,
    QueryRewriter,
)


def _llm(handler: object) -> BufferedLlmClient:
    pool = ResilientHttpPool(
        ("http://llm",),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(
            max_attempts=1,
            failure_threshold=1,
            cooldown_seconds=30,
            max_concurrency=1,
        ),
    )
    return BufferedLlmClient(
        pool,
        model="Qwen/Qwen3-8B-AWQ",
        max_context_tokens=8192,
        api_token=None,
    )


def _rewrite_handler(standalone_query: str) -> object:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"standalone_query": standalone_query},
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    return handler


def _verified_claim(text: str, suffix: str) -> VerifiedClaimContext:
    return VerifiedClaimContext(
        text=text,
        supports=(
            VerifiedClaimSupport(
                chunk_id=f"chunk_{suffix * 32}",
                locator=f"规范.docx > 段落{suffix}",
            ),
        ),
    )


def test_rewriter_only_triggers_for_context_dependent_question() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "standalone_query": (
                                        "需求快验流程的验收负责人是谁？"
                                    )
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                },
            },
        )

    rewriter = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=200,
            max_question_tokens=100,
            max_output_tokens=64,
        ),
    )

    standalone = rewriter.rewrite(
        "项目交付的验收条件有哪些？",
        previous_questions=("前一个问题",),
    )
    contextual = rewriter.rewrite(
        "其中负责人是谁？",
        previous_questions=(
            "无关旧问题",
            "需求快验流程有哪些步骤？",
        ),
    )

    assert standalone.queries == ("项目交付的验收条件有哪些？",)
    assert standalone.resolved_query == "项目交付的验收条件有哪些？"
    assert contextual.queries == (
        "其中负责人是谁？",
        "需求快验流程的验收负责人是谁？",
    )
    assert contextual.resolved_query == "需求快验流程的验收负责人是谁？"
    assert contextual.rewritten is True
    assert standalone.trace["reason_code"] == "NO_CONTEXT_SIGNAL"
    assert contextual.trace["reason_code"] == "REWRITE_TRIGGER_PRONOUN"
    assert contextual.trace["rewrite_result_code"] == "REWRITE_OK"
    assert contextual.trace["question_tokens"] > 0
    assert contextual.trace["resolved_query_sha256"] != ""
    assert calls == 1


def test_invalid_rewrite_falls_back_to_original() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"unexpected":"value"}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        )

    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=2,
            history_token_budget=100,
            max_question_tokens=100,
            max_output_tokens=32,
        ),
    ).rewrite(
        "它什么时候生效？",
        previous_questions=("项目交付规范是什么？",),
    )

    assert result.queries == ("它什么时候生效？",)
    assert result.resolved_query == "它什么时候生效？"
    assert result.rewritten is False
    assert result.trace["reason_code"] == "REWRITE_INVALID_SCHEMA"


def test_rewrite_trigger_uses_bounded_context_references() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "standalone_query": (
                                        "3号主变跳闸告警如何处理？"
                                    )
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    rewriter = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=200,
            max_question_tokens=100,
            max_output_tokens=64,
        ),
    )
    history = ("3号主变昨天发生跳闸告警。",)

    direct = rewriter.rewrite(
        "设备过热该如何处理",
        previous_questions=history,
    )
    sentence_particle = rewriter.rewrite(
        "变压器油温过高怎么办呢",
        previous_questions=history,
    )
    contextual = rewriter.rewrite(
        "昨天那个跳闸的怎么处理",
        previous_questions=history,
    )
    contextual_particle = rewriter.rewrite(
        "3号主变那个告警呢",
        previous_questions=history,
    )
    without_history = rewriter.rewrite(
        "其中负责人是谁？",
        previous_questions=(),
    )

    assert direct.rewritten is False
    assert sentence_particle.rewritten is False
    assert contextual.rewritten is True
    assert contextual_particle.rewritten is True
    assert without_history.rewritten is False
    assert without_history.trace["reason_code"] == "NO_HISTORY"
    assert calls == 2


def test_rewrite_trigger_rejects_substring_and_particle_false_positives() -> (
    None
):
    def unexpected_call(_: httpx.Request) -> httpx.Response:
        raise AssertionError("独立问句不得调用改写模型。")

    rewriter = QueryRewriter(
        _llm(unexpected_call),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=200,
            max_question_tokens=100,
            max_output_tokens=64,
        ),
    )
    history = ("3号主变昨天发生跳闸告警。",)

    for question in (
        "其他情况怎么处理",
        "其次要检查什么",
        "尤其要注意什么",
        "该设备如何处理",
        "其运行原理是什么",
        "变压器油温过高怎么办呢",
    ):
        result = rewriter.rewrite(
            question,
            previous_questions=history,
        )

        assert result.rewritten is False
        assert result.trace["reason_code"] == "NO_CONTEXT_SIGNAL"


@pytest.mark.parametrize(
    ("question", "rewritten", "expected_trigger"),
    [
        (
            "上述流程由谁验收？",
            "项目验收流程由质量负责人验收？",
            "REWRITE_TRIGGER_PRONOUN",
        ),
        (
            "刚才提到的流程由谁验收？",
            "项目验收流程由质量负责人验收？",
            "REWRITE_TRIGGER_TEMPORAL",
        ),
        (
            "第二种怎么做？",
            "流程的第二种复核怎么做？",
            "REWRITE_TRIGGER_ORDINAL",
        ),
        (
            "再详细说明一下？",
            "请详细说明项目验收流程。",
            "REWRITE_TRIGGER_CONTINUATION",
        ),
    ],
)
def test_rewrite_trigger_reports_stable_rule_category(
    question: str,
    rewritten: str,
    expected_trigger: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        user_payload = json.loads(
            request_payload["messages"][1]["content"]
        )
        assert user_payload["current_question"] == question
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"standalone_query": rewritten},
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    verified_claims = (
        (
            _verified_claim("流程的第一种准备", "1"),
            _verified_claim("流程的第二种复核", "2"),
        )
        if question == "第二种怎么做？"
        else ()
    )
    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=200,
            max_question_tokens=100,
            max_output_tokens=64,
        ),
    ).rewrite(
        question,
        previous_questions=(
            "项目验收流程包括第一种准备和第二种复核，"
            "由质量负责人负责。",
        ),
        verified_claims=verified_claims,
    )

    assert result.rewritten is True
    assert result.trace["reason_code"] == expected_trigger
    assert result.trace["rewrite_result_code"] == "REWRITE_OK"
    assert result.trace["trigger_reason_code"] == expected_trigger
    assert question not in str(result.trace["trigger_reason_code"])


@pytest.mark.parametrize(
    ("history", "question", "rewritten"),
    [
        (
            ("3号主变发生过跳闸告警。",),
            "3号主变那个告警怎么处理？",
            "2号主变告警怎么处理？",
        ),
        (
            ("保护阈值为5%。",),
            "上述5%阈值何时适用？",
            "10%阈值何时适用？",
        ),
        (
            ("维护日期为2026-07-30。",),
            "上述2026-07-30维护日期可以调整吗？",
            "2026-07-31维护日期可以调整吗？",
        ),
        (
            ("设备当前运行版本为v2。",),
            "上述v2版本如何升级？",
            "v3版本如何升级？",
        ),
        (
            ("应遵循GB/T19001-2016条款。",),
            "上述GB/T19001-2016条款如何执行？",
            "GB/T19001-2015条款如何执行？",
        ),
        (
            ("规范要求使用“Alpha方案”。",),
            "上述“Alpha方案”何时生效？",
            "“Beta方案”何时生效？",
        ),
    ],
)
def test_rewrite_anchor_guard_rejects_entity_drift(
    history: tuple[str, ...],
    question: str,
    rewritten: str,
) -> None:
    result = QueryRewriter(
        _llm(_rewrite_handler(rewritten)),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=200,
            max_question_tokens=100,
            max_output_tokens=64,
        ),
    ).rewrite(
        question,
        previous_questions=history,
    )

    assert result.queries == (question,)
    assert result.resolved_query == question
    assert result.rewritten is False
    assert result.trace["reason_code"] == "REWRITE_ANCHOR_DRIFT"


@pytest.mark.parametrize(
    ("history", "question", "rewritten"),
    [
        (
            ("A-17设备发生过热告警。",),
            "那个怎么处理？",
            "A-17设备过热告警怎么处理？",
        ),
        (
            ("“Alpha方案”的范围是什么？",),
            "它何时生效？",
            "“Alpha方案”何时生效？",
        ),
    ],
)
def test_rewrite_anchor_guard_allows_selected_history_anchor(
    history: tuple[str, ...],
    question: str,
    rewritten: str,
) -> None:
    result = QueryRewriter(
        _llm(_rewrite_handler(rewritten)),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=200,
            max_question_tokens=100,
            max_output_tokens=64,
        ),
    ).rewrite(
        question,
        previous_questions=history,
    )

    assert result.queries == (question, rewritten)
    assert result.resolved_query == rewritten
    assert result.rewritten is True
    assert result.trace["reason_code"] == "REWRITE_TRIGGER_PRONOUN"
    assert result.trace["rewrite_result_code"] == "REWRITE_OK"


def test_rewrite_reports_history_budget_empty() -> None:
    rewriter = QueryRewriter(
        _llm(lambda _: httpx.Response(500)),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=2,
            history_token_budget=1,
            max_question_tokens=100,
            max_output_tokens=32,
        ),
    )

    result = rewriter.rewrite(
        "它什么时候生效？",
        previous_questions=("项目交付规范是什么？",),
    )

    assert result.trace["reason_code"] == "HISTORY_BUDGET_EMPTY"


def test_rewrite_reports_model_unavailable() -> None:
    result = QueryRewriter(
        _llm(lambda _: httpx.Response(503)),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=2,
            history_token_budget=100,
            max_question_tokens=100,
            max_output_tokens=32,
        ),
    ).rewrite(
        "它什么时候生效？",
        previous_questions=("项目交付规范是什么？",),
    )

    assert result.trace["reason_code"] == "REWRITE_MODEL_UNAVAILABLE"


def test_rewrite_reports_same_as_original() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"standalone_query": "它什么时候生效？"},
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        )

    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=2,
            history_token_budget=100,
            max_question_tokens=100,
            max_output_tokens=32,
        ),
    ).rewrite(
        "它什么时候生效？",
        previous_questions=("项目交付规范是什么？",),
    )

    assert result.trace["reason_code"] == "REWRITE_SAME_AS_ORIGINAL"


def test_rewrite_reports_resolved_query_token_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"standalone_query": "超长改写" * 30},
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        )

    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=2,
            history_token_budget=100,
            max_question_tokens=40,
            max_output_tokens=32,
        ),
    ).rewrite(
        "它何时生效？",
        previous_questions=("项目规范？",),
    )

    assert result.trace["reason_code"] == "REWRITE_TOKEN_LIMIT"


def test_spatial_front_and_above_words_do_not_trigger_rewrite() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _rewrite_handler("设备铭牌写了什么？")(request)

    rewriter = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=256,
            max_question_tokens=128,
            max_output_tokens=64,
        ),
    )

    above = rewriter.rewrite(
        "设备上面有铭牌吗？",
        previous_questions=("设备检查要求是什么？",),
    )
    front = rewriter.rewrite(
        "设备前面需要留多少空间？",
        previous_questions=("设备检查要求是什么？",),
    )

    assert above.trace["reason_code"] == "NO_CONTEXT_SIGNAL"
    assert front.trace["reason_code"] == "NO_CONTEXT_SIGNAL"
    assert calls == 0


def test_explicit_discourse_phrase_still_triggers_rewrite() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured_payload.update(json.loads(payload["messages"][1]["content"]))
        return _rewrite_handler("项目验收方案怎么执行？")(request)

    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=256,
            max_question_tokens=128,
            max_output_tokens=64,
        ),
    ).rewrite(
        "前面提到的方案怎么执行？",
        previous_questions=("项目验收方案是什么？",),
    )

    assert result.rewritten is True
    assert captured_payload["history_questions"] == [
        "项目验收方案是什么？"
    ]


def test_referential_ordinal_uses_verified_claim_order() -> None:
    captured_payload: dict[str, object] = {}
    claims = (
        _verified_claim("方案甲：先隔离设备。", "1"),
        _verified_claim("方案乙：执行双人复核。", "2"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured_payload.update(json.loads(payload["messages"][1]["content"]))
        return _rewrite_handler("方案乙：执行双人复核。怎么操作？")(
            request
        )

    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=512,
            max_question_tokens=256,
            max_output_tokens=64,
        ),
    ).rewrite(
        "第二种怎么操作？",
        previous_questions=("有哪些处理方案？",),
        verified_claims=claims,
    )

    assert result.rewritten is True
    assert result.resolved_query == "方案乙：执行双人复核。怎么操作？"
    resolved = captured_payload["resolved_references"]
    assert isinstance(resolved, list)
    assert resolved[0]["reference"] == "第二种"
    assert resolved[0]["resolved_claim"]["text"] == claims[1].text


def test_unresolvable_referential_ordinal_falls_back_without_llm() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _rewrite_handler("不应调用")(request)

    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=256,
            max_question_tokens=128,
            max_output_tokens=64,
        ),
    ).rewrite(
        "第二种怎么操作？",
        previous_questions=("有哪些处理方案？",),
        verified_claims=(
            _verified_claim("仅有一种方案。", "1"),
        ),
    )

    assert result.queries == ("第二种怎么操作？",)
    assert result.trace["reason_code"] == "REWRITE_CONTEXT_UNRESOLVED"
    assert calls == 0


def test_fact_ordinal_must_not_be_replaced_by_claim_text() -> None:
    result = QueryRewriter(
        _llm(_rewrite_handler("安全规范第1条怎么执行？")),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=256,
            max_question_tokens=128,
            max_output_tokens=64,
        ),
    ).rewrite(
        "第2条怎么执行？",
        previous_questions=("安全规范有哪些条款？",),
        verified_claims=(
            _verified_claim("安全规范要求双人复核。", "1"),
        ),
    )

    assert result.rewritten is False
    assert result.trace["reason_code"] == "REWRITE_ANCHOR_DRIFT"


@pytest.mark.parametrize(
    ("question", "claim_index"),
    [("前者怎么操作？", 0), ("后者怎么操作？", 1)],
)
def test_pair_reference_uses_verified_claim_order(
    question: str,
    claim_index: int,
) -> None:
    claims = (
        _verified_claim("方案甲：先隔离设备。", "1"),
        _verified_claim("方案乙：执行双人复核。", "2"),
    )
    expected = f"{claims[claim_index].text}怎么操作？"

    result = QueryRewriter(
        _llm(_rewrite_handler(expected)),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=512,
            max_question_tokens=256,
            max_output_tokens=64,
        ),
    ).rewrite(
        question,
        previous_questions=("有哪些处理方案？",),
        verified_claims=claims,
    )

    assert result.rewritten is True
    assert result.resolved_query == expected


def test_referential_claim_outside_token_budget_is_unresolved() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _rewrite_handler("不应调用")(request)

    result = QueryRewriter(
        _llm(handler),
        Utf8TokenCounter(),
        QueryRewriteConfig(
            max_history_turns=3,
            history_token_budget=8,
            max_question_tokens=128,
            max_output_tokens=64,
        ),
    ).rewrite(
        "第二项怎么操作？",
        previous_questions=("有哪些处理方案？",),
        verified_claims=(
            _verified_claim("方案甲。", "1"),
            _verified_claim("方案乙。", "2"),
        ),
    )

    assert result.trace["reason_code"] == "REWRITE_CONTEXT_UNRESOLVED"
    assert calls == 0
