"""通过真实适配器序列化回放固定草稿与严格语义复核响应。"""

from __future__ import annotations

import json

import httpx

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.core.models import AnswerDraft


def _review_result(claim_id: str, status: str) -> dict[str, str]:
    """将测试状态转换为当前三维复核协议。"""
    result = {
        "claim_id": claim_id,
        "source_support": "supported",
        "question_relevance": "answered",
        "qualifier_fidelity": "faithful",
    }
    if status in {"unknown", "undetermined"}:
        result.update(
            source_support="unknown",
            question_relevance="unknown",
            qualifier_fidelity="unknown",
        )
    elif status == "irrelevant":
        result["question_relevance"] = "irrelevant"
    elif status == "contradicted":
        result["source_support"] = "contradicted"
    return result


def fixed_review_generator(
    draft: AnswerDraft,
    *,
    statuses: tuple[str, ...] | None = None,
) -> OpenAICompatibleChatAdapter:
    """首调用返回 Wire Claim，第二调用返回固定三态语义结果。

    Args:
        draft: 测试原有未经接受的固定事实草稿。
        statuses: 可选的逐候选复核状态；默认全部 supported。

    Returns:
        使用 Fake HTTP 的真实兼容模型适配器。

    """
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        assert sends <= 2
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])
        if sends == 1:
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
                    for claim in draft.natural_claims
                ]
            }
        else:
            assert "candidates" in data
            review_statuses = (
                statuses or ("supported",) * len(data["candidates"])
            )
            payload = {
                "results": [
                    _review_result(
                        candidate["claim_id"], review_statuses[index]
                    )
                    for index, candidate in enumerate(data["candidates"])
                ]
            }
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
                    "prompt_tokens": 200,
                    "completion_tokens": 70,
                    "total_tokens": 270,
                },
            },
        )

    return OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic",
            egress_allowed=True,
            structured_output_mode="response_format",
        ),
        http_client=ProviderHttpClient(
            "https://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1,
        ),
        api_key_resolver=lambda: "",
    )
