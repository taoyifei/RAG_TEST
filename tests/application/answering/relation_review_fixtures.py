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


def fixed_review_generator(
    draft: AnswerDraft,
    *,
    subject: str,
    relation: str,
    conditions: tuple[str, ...],
) -> OpenAICompatibleChatAdapter:
    """首调用返回原草稿，第二调用只回固定范围和实际发送的引用。

    Args:
        draft: 测试原有未经接受的固定事实草稿。
        subject: 测试允许的来源主体。
        relation: 测试允许的来源关系。
        conditions: 保持原文限定的条件集合。

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
            payload = {
                "claims": [
                    claim.model_dump(mode="json")
                    for claim in draft.natural_claims
                ]
            }
        else:
            assert "candidates" in data
            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": "supported",
                        "supports": candidate["supports"],
                        "covered_scope": {
                            "subject": subject,
                            "relation": relation,
                            "stage": "",
                            "conditions": list(conditions),
                        },
                    }
                    for candidate in data["candidates"]
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
