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
            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": (
                            "unknown"
                            if statuses is not None
                            and statuses[index] == "undetermined"
                            else "contradicted"
                            if statuses is not None
                            and statuses[index] == "irrelevant"
                            else statuses[index]
                            if statuses is not None
                            else "supported"
                        ),
                    }
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
