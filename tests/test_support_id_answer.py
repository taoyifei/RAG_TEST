import json

import httpx
import pytest

from rag_app.chunking import Utf8TokenCounter
from rag_app.clients.llm import BufferedLlmClient
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.generation.answer import (
    AnswerConfig,
    AnswerGenerator,
    AnswerMode,
    AnswerStatus,
    RefusalCode,
)
from rag_app.generation.evidence import (
    EvidenceAssembler,
    EvidenceBundle,
    EvidenceConfig,
)
from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.rerank import RerankedHit


def _ranked(  # noqa: PLR0913
    chunk_id: str,
    text: str,
    *,
    rank: int = 1,
    score: float = 1.0,
    file_path: str = "规范.docx",
    source_id: str = "source-1",
    neighbor_group_id: str = "neighbor-1",
    low_ocr: bool = False,
) -> RerankedHit:
    locator = {
        "file_path": file_path,
        "heading_path": ["验收"],
        "paragraph_index": 1,
        "fragment": text[:40],
    }
    return RerankedHit(
        rank=rank,
        rerank_score=score,
        hit=FusedHit(
            chunk_id=chunk_id,
            rrf_score=0.1,
            channel_ranks=(("q0:dense", 1),),
            payload={
                "chunk_id": chunk_id,
                "source_id": source_id,
                "neighbor_group_id": neighbor_group_id,
                "text": text,
                "embedding_text": text,
                "locators": [locator],
                "source_spans": [
                    {
                        "element_id": f"element-{chunk_id}",
                        "locator": locator,
                        "start_char": 0,
                        "end_char": len(text),
                        "source_start_char": 0,
                        "source_end_char": len(text),
                        "is_repeated": False,
                    }
                ],
                "contains_ocr": low_ocr,
                "minimum_ocr_confidence": 0.5 if low_ocr else None,
            },
        ),
    )


def _bundle(*hits: RerankedHit) -> EvidenceBundle:
    return EvidenceAssembler(
        Utf8TokenCounter(),
        EvidenceConfig(
            max_evidence_tokens=4096,
            max_items=8,
            low_ocr_threshold=0.8,
        ),
    ).assemble(tuple(hits))


def _response(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "Qwen/Qwen3-8B-AWQ",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(payload, ensure_ascii=False)
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 20,
                "total_tokens": 140,
            },
        },
    )


def _generator(handler: object) -> AnswerGenerator:
    llm = BufferedLlmClient(
        ResilientHttpPool(
            ("http://llm",),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            policy=ResiliencePolicy(
                max_attempts=1,
                failure_threshold=1,
                cooldown_seconds=30,
                max_concurrency=2,
            ),
        ),
        model="Qwen/Qwen3-8B-AWQ",
        max_context_tokens=8192,
        api_token=None,
    )
    return AnswerGenerator(
        llm,
        AnswerConfig(max_output_tokens=768, max_repair_tokens=384),
    )


def test_support_id_is_resolved_to_exact_quote_and_locator() -> None:
    bundle = _bundle(
        _ranked("chunk-1", "验收测试完成后输出《验收测试报告》。")
    )
    support_id = bundle.units[0].unit_id

    result = _generator(
        lambda _: _response(
            {
                "claims": [
                    {
                        "text": "验收测试完成后输出《验收测试报告》。",
                        "support_ids": [support_id],
                    }
                ]
            }
        )
    ).answer(
        "验收测试完成后需要输出什么文档？",
        bundle,
        rerank_scores=(1.0, 0.98),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.ANSWERED
    assert result.model_calls == 1
    support = result.claims[0].supports[0]
    assert support.evidence_id == "E1"
    assert support.chunk_id == "chunk-1"
    assert support.quote == "验收测试完成后输出《验收测试报告》。"
    assert support.locator.startswith("规范.docx > 验收")


def test_valid_claim_is_published_when_another_support_id_is_invalid() -> None:
    bundle = _bundle(_ranked("chunk-1", "需求变更应提交书面申请。"))
    valid_id = bundle.units[0].unit_id

    result = _generator(
        lambda _: _response(
            {
                "claims": [
                    {
                        "text": "需求变更应提交书面申请。",
                        "support_ids": [valid_id],
                    },
                    {
                        "text": "未经支持的内容。",
                        "support_ids": ["E99:S99"],
                    },
                ]
            }
        )
    ).answer(
        "需求变更需要经过哪些审批步骤？",
        bundle,
        rerank_scores=(1.0, 0.99),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.PARTIAL
    assert [claim.text for claim in result.claims] == [
        "需求变更应提交书面申请。"
    ]
    assert result.model_calls == 1
    assert result.trace["dropped_claim_count"] == 1
    assert result.trace["dropped_claim_codes"] == {
        "INVALID_SUPPORT_ID": 1
    }


def test_missing_strong_anchors_return_friendly_not_found_without_llm() -> None:
    calls = 0
    bundle = _bundle(
        _ranked(
            "chunk-1",
            "验收测试应记录测试结论。",
            score=0.3477,
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("NOT_FOUND 不得调用 LLM。")

    result = _generator(handler).answer(
        "知识库是否记载火星基地RAG-999项目在2099年的验收负责人姓名？",
        bundle,
        rerank_scores=(0.3477, 0.0447, 0.0275),
    )

    assert result.status is AnswerStatus.REFUSED
    assert result.refusal_code is RefusalCode.EVIDENCE_INSUFFICIENT
    assert result.answer_mode is AnswerMode.NOT_FOUND
    assert result.user_message == (
        "知识库中暂未找到能够支持该问题的资料。请核对项目名称、编号或时间，"
        "或补充相关文档。"
    )
    assert result.model_calls == 0
    assert result.trace["answerability_decision"] == "NOT_FOUND"
    assert calls == 0


def test_missing_uppercase_subject_returns_not_found_without_llm() -> None:
    calls = 0
    bundle = _bundle(
        _ranked(
            "chunk-1",
            "品质部经理负责质量管理体系的建立、实施和保持。",
            score=0.003,
            file_path="GM-02 岗位职责规定.docx",
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("缺少 OPC 证据时不得调用 LLM。")

    result = _generator(handler).answer(
        "OPC owner 在项目启动后要做什么？",
        bundle,
        rerank_scores=(0.003, 0.0006),
    )

    assert result.status is AnswerStatus.REFUSED
    assert result.answer_mode is AnswerMode.NOT_FOUND
    assert result.model_calls == 0
    assert result.trace["covered_anchor_count"] == 0
    assert calls == 0


def test_named_subject_is_allowed_when_selected_evidence_supports_it() -> None:
    bundle = _bundle(
        _ranked(
            "chunk-opc",
            "OPC owner 在项目启动后应确认项目范围。",
            file_path="OPC 项目管理规范.docx",
        )
    )

    result = _generator(
        lambda _: _response(
            {
                "claims": [
                    {
                        "text": "OPC owner 应确认项目范围。",
                        "support_ids": ["E1:S1"],
                    }
                ]
            }
        )
    ).answer(
        "OPC owner 在项目启动后要做什么？",
        bundle,
        rerank_scores=(1.0,),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.model_calls == 1


def test_unsupported_quoted_process_cannot_use_extractive_fallback() -> None:
    bundle = _bundle(
        _ranked(
            "chunk-1",
            "来料急需时，经批准后可以例外放行。",
            score=1.0,
            file_path="GM-06 产品质量检验管理制度.docx",
        )
    )
    responses = iter(
        (
            {
                "claims": [
                    {
                        "text": "《需求快验流程》允许对部分环节灵活处理。",
                        "support_ids": ["E1:S1"],
                    }
                ]
            },
            {"claims": []},
        )
    )

    result = _generator(lambda _: _response(next(responses))).answer(
        "《需求快验流程》中哪些环节可以灵活处理？",
        bundle,
        rerank_scores=(1.0,),
    )

    assert result.status is AnswerStatus.REFUSED
    assert result.answer_mode is AnswerMode.INTERNAL_VALIDATION_ERROR
    assert result.refusal_code is RefusalCode.EVIDENCE_INSUFFICIENT
    assert result.model_calls == 2
    assert result.trace["first_validation_code"] == (
        "UNSUPPORTED_QUESTION_ANCHOR"
    )
    assert result.trace.get("extractive_fallback") is None


def test_named_source_fallback_filters_before_limit() -> None:
    bundle = _bundle(
        *(
            _ranked(
                f"chunk-generic-{index}",
                f"通用质量管理要求 {index}。",
                rank=index,
                score=1.0 - index / 100,
                file_path=f"GM-0{index} 通用质量制度.docx",
                source_id=f"source-generic-{index}",
                neighbor_group_id=f"generic-{index}",
            )
            for index in range(1, 5)
        ),
        _ranked(
            "chunk-gm06",
            "检验人员应按规定填写检验记录。",
            rank=5,
            score=0.95,
            file_path="GM-06 产品质量检验管理制度.docx",
            source_id="source-gm06",
            neighbor_group_id="gm06",
        ),
    )
    invalid = {
        "claims": [
            {
                "text": "通用质量管理要求。",
                "support_ids": ["E1:S1"],
            }
        ]
    }

    result = _generator(lambda _: _response(invalid)).answer(
        "GM-06《产品质量检验管理制度》有哪些要求？",
        bundle,
        rerank_scores=(0.99, 0.98, 0.97, 0.96, 0.95),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.EXTRACTIVE_FALLBACK
    assert result.model_calls == 2
    assert result.trace["first_validation_code"] == (
        "UNSUPPORTED_QUESTION_ANCHOR"
    )
    assert result.trace["repair_validation_code"] == (
        "UNSUPPORTED_QUESTION_ANCHOR"
    )
    assert result.trace["extractive_fallback"] is True
    assert len(result.claims) == 1
    assert result.claims[0].text == "检验人员应按规定填写检验记录。"
    assert result.claims[0].supports[0].locator.startswith(
        "GM-06 产品质量检验管理制度.docx"
    )


def test_deliverables_preserve_source_and_actor_relationships() -> None:
    bundle = _bundle(
        _ranked(
            "chunk-opc",
            "责任人为测试工程师，测试完成后输出《验收测试报告》。",
            file_path="OPC规范.docx",
            source_id="source-opc",
            neighbor_group_id="opc-test",
        ),
        _ranked(
            "chunk-delivery",
            "测试阶段应输出《测试交付报告》。",
            file_path="项目交付规范.docx",
            source_id="source-delivery",
            neighbor_group_id="delivery-test",
        ),
    )
    first_id = bundle.units[0].unit_id
    second_id = next(
        unit.unit_id
        for unit in bundle.units
        if unit.evidence_id == "E2"
    )

    result = _generator(
        lambda _: _response(
            {
                "claims": [
                    {
                        "text": (
                            "OPC规范由测试工程师输出《验收测试报告》。"
                        ),
                        "support_ids": [first_id],
                    },
                    {
                        "text": (
                            "项目交付规范要求输出《测试交付报告》，当前证据未说明"
                            "责任人。"
                        ),
                        "support_ids": [second_id],
                    },
                ]
            }
        )
    ).answer(
        "验收测试完成后需要输出什么文档？",
        bundle,
        rerank_scores=(1.0, 1.0, 0.95),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.SOURCE_SEPARATED
    assert result.user_message == "下面按模式或来源分别列出。"
    assert "《验收测试报告》" in result.claims[0].text
    assert "测试工程师" in result.claims[0].text
    assert "《测试交付报告》" in result.claims[1].text
    assert "当前证据未说明责任人" in result.claims[1].text
    assert result.claims[0].supports[0].chunk_id == "chunk-opc"
    assert result.claims[1].supports[0].chunk_id == "chunk-delivery"


@pytest.mark.parametrize(
    (
        "question",
        "expected_intent",
    ),
    (
        ("项目交付、需求快验和产品开发三种模式有什么区别？", "COMPARE"),
        ("一个需求先走快验还是直接产品开发？", "DECISION"),
        ("快验可灵活，但项目交付不能省略哪些环节？", "COMPARE"),
    ),
)
def test_compare_and_decision_keep_each_claim_in_one_source_group(
    question: str,
    expected_intent: str,
) -> None:
    bundle = _bundle(
        _ranked(
            "quick-check",
            "需求快验用于验证可行性，团队可按实际情况安排研发环节。",
            file_path="需求快验规范.docx",
            source_id="quick-check",
            neighbor_group_id="quick-check",
        ),
        _ranked(
            "delivery",
            "项目交付必须完成启动评审并提交项目交付材料。",
            file_path="项目交付规范.docx",
            source_id="delivery",
            neighbor_group_id="delivery",
        ),
    )
    first_id = bundle.units[0].unit_id
    second_id = next(
        unit.unit_id
        for unit in bundle.units
        if unit.evidence_id == "E2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        assert (
            payload["question_profile"]["primary_operation"]
            == expected_intent
        )
        prompt = body["messages"][0]["content"]
        assert "每个模式或来源单独输出一条 claim" in prompt
        assert "优先输出高置信、单一来源的 claim" in prompt
        return _response(
            {
                "claims": [
                    {
                        "text": "需求快验可按实际情况安排研发环节。",
                        "support_ids": [first_id],
                    },
                    {
                        "text": "项目交付必须完成启动评审并提交材料。",
                        "support_ids": [second_id],
                    },
                ]
            }
        )

    result = _generator(handler).answer(
        question,
        bundle,
        rerank_scores=(1.0, 0.98),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.SOURCE_SEPARATED
    assert result.user_message == "下面按模式或来源分别列出。"
    assert result.trace["dropped_claim_codes"] == {}
    assert result.model_calls == 1


def test_final_support_quality_is_recorded_without_changing_selection() -> None:
    bundle = _bundle(
        _ranked(
            "opc-owner",
            "OPC owner 负责组织启动评审。",
            rank=6,
            score=0.0393,
        )
    )
    support_id = bundle.units[0].unit_id

    result = _generator(
        lambda _: _response(
            {
                "claims": [
                    {
                        "text": "OPC owner 负责组织启动评审。",
                        "support_ids": [support_id],
                    }
                ]
            }
        )
    ).answer("我是 OPC owner，项目开始了我要干啥", bundle)

    assert result.status is AnswerStatus.ANSWERED
    assert result.trace["selected_support_ranks"] == [6]
    assert result.trace["min_selected_support_score"] == 0.0393
    assert result.trace["low_rank_support_count"] == 1


def test_supported_double_abstention_uses_matching_extractive_fallback(
) -> None:
    calls = 0
    bundle = _bundle(
        _ranked(
            "chunk-1",
            "需求变更应提交书面申请；完成影响评估后由双方确认。",
            score=1.0,
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response({"claims": []})

    result = _generator(handler).answer(
        "需求变更需要经过哪些审批步骤？",
        bundle,
        rerank_scores=(1.0, 0.99),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.answer_mode is AnswerMode.EXTRACTIVE_FALLBACK
    assert result.model_calls == 2
    assert result.trace["extractive_fallback"] is True
    assert "需求变更应提交书面申请" in (result.answer or "")
    assert calls == 2


@pytest.mark.parametrize(
    ("question", "evidence_text"),
    (
        (
            "需求变更需要经过哪些审批步骤？",
            "需求变更应提交申请、评估影响、书面确认并更新需求基线。",
        ),
        (
            "需求规格说明书需要明确哪些内容？",
            "需求规格说明书应明确核心功能、验收标准、边界和技术约束。",
        ),
        (
            "需求变更前需要评估哪些方面的影响？",
            "项目团队应评估变更对交付周期、成本和技术实现的影响。",
        ),
        (
            "需求基线形成前需要完成哪些评审工作？",
            "项目负责人应组织需求对齐评审，评审通过后形成需求基线。",
        ),
        (
            "验收测试完成后需要输出什么文档？",
            "测试工程师完成验收测试后输出《验收测试报告》。",
        ),
        (
            "验收测试包括哪些内容？",
            "验收测试包括功能验收、非功能验收、数据验收和文档验收。",
        ),
        (
            "OPC是什么？",
            "OPC是面向项目交付的协同开发流程。",
        ),
    ),
)
def test_real_answerable_trace_questions_publish_with_one_generation(
    question: str,
    evidence_text: str,
) -> None:
    calls = 0
    bundle = _bundle(_ranked("chunk-1", evidence_text, score=1.0))

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(
            {
                "claims": [
                    {
                        "text": evidence_text,
                        "support_ids": ["E1:S1"],
                    }
                ]
            }
        )

    result = _generator(handler).answer(
        question,
        bundle,
        rerank_scores=(1.0, 0.99),
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.model_calls == 1
    assert result.claims[0].supports[0].quote == evidence_text
    assert "QUOTE_NOT_IN_EVIDENCE" not in result.trace.values()
    assert calls == 1
