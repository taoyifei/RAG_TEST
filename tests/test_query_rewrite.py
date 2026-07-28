import json

import httpx

from rag_app.chunking import Utf8TokenCounter
from rag_app.clients.llm import BufferedLlmClient
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
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
    assert calls == 2
