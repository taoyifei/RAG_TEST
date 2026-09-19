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


def fixed_review_generator(  # noqa: PLR0913
    draft: AnswerDraft,
    *,
    subject: str,
    relation: str,
    conditions: tuple[str, ...],
    relation_anchor: str | None = None,
    statuses: tuple[str, ...] | None = None,
) -> OpenAICompatibleChatAdapter:
    """首调用返回原草稿，第二调用只回固定范围和实际发送的引用。

    Args:
        draft: 测试原有未经接受的固定事实草稿。
        subject: 测试允许的来源主体。
        relation: 测试允许的来源关系。
        conditions: 保持原文限定的条件集合。
        relation_anchor: 可选的逐字关系锚点；默认使用 relation。
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
            payload = {
                "claims": [
                    claim.model_dump(mode="json")
                    for claim in draft.natural_claims
                ]
            }
        else:
            assert "candidates" in data
            source_quotes = {
                item["source_id"]: item["quotes"][0]
                for item in data["evidence"]
            }

            def anchor(
                candidate: dict[str, object], value: str
            ) -> dict[str, str]:
                """把测试语义字段绑定到候选实际可用来源。"""
                source_ids = (
                    candidate["context_source_ids"]
                    or candidate["fact_source_ids"]
                )
                assert isinstance(source_ids, list)
                source_id = next(
                    (
                        item
                        for item in source_ids
                        if value in source_quotes[item]
                    ),
                    source_ids[0],
                )
                return {"source_id": source_id, "quote": value}

            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": (
                            statuses[index]
                            if statuses is not None
                            else "supported"
                        ),
                        "fact_source_ids": (
                            candidate["fact_source_ids"]
                            if statuses is None
                            or statuses[index] == "supported"
                            else []
                        ),
                        "source_scope": {
                            "relation_label": (
                                relation
                                if statuses is None
                                or statuses[index] == "supported"
                                else ""
                            ),
                            "subject_anchors": (
                                [anchor(candidate, subject)]
                                if subject
                                and (
                                    statuses is None
                                    or statuses[index] == "supported"
                                )
                                else []
                            ),
                            "relation_anchors": (
                                [anchor(candidate, relation_anchor or relation)]
                                if statuses is None
                                or statuses[index] == "supported"
                                else []
                            ),
                            "stage_anchors": [],
                            "condition_anchors": [
                                anchor(candidate, condition)
                                for condition in conditions
                            ]
                            if statuses is None
                            or statuses[index] == "supported"
                            else [],
                        },
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
