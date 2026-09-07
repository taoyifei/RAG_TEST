"""产品内建的固定纯内存响应器；构造参数不能注入 Handler 或网络客户端。"""

from __future__ import annotations

import json
from typing import Any

import httpx

_DIMENSION = 1024


class BuiltinOfflineMockTransport(httpx.MockTransport):
    """仅由固定响应代码处理请求，不能通过参数注入可出网的回调。"""

    def __init__(self, provider_type: str, expected_host: str) -> None:
        """冻结响应形状和预期主机；不创建套接字或读取凭据。

        Args:
            provider_type: 产品支持的固定 Provider 类型。
            expected_host: 已通过产品端点解析得到的非秘密主机。

        Returns:
            无返回值；实例只有固定的内存响应能力。

        """
        if provider_type not in {"jina", "aliyun-model-studio"}:
            raise ValueError("内建离线响应器不支持该 Provider。")
        self._provider_type = provider_type
        self._expected_host = expected_host
        super().__init__(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if not isinstance(body, dict):
            return httpx.Response(400)
        if "rerank" in request.url.path:
            return _rerank_response(request, body)
        if self._provider_type == "jina":
            return _jina_response(request, body)
        return _aliyun_response(request, body, self._expected_host)


def _rerank_response(
    request: httpx.Request, body: dict[str, Any]
) -> httpx.Response:
    documents = body.get("documents")
    if (
        request.url.host != "api.jina.ai"
        or request.url.path != "/v1/rerank"
        or body.get("model") != "jina-reranker-v3.5"
        or not isinstance(documents, list)
        or not documents
        or any(not isinstance(item, str) or not item for item in documents)
        or body.get("top_n") != len(documents)
        or body.get("top_n", 0) <= 0
        or body.get("return_documents") is not False
    ):
        return httpx.Response(400)
    return httpx.Response(
        200,
        json={
            "model": body["model"],
            "results": [
                {"index": index, "relevance_score": 0.9 - index / 100}
                for index, _ in enumerate(documents)
            ],
            "usage": {"total_tokens": 12},
        },
    )


def _jina_response(
    request: httpx.Request, body: dict[str, Any]
) -> httpx.Response:
    if (
        request.url.host != "api.jina.ai"
        or request.url.path != "/v1/embeddings"
        or body.get("model") != "jina-embeddings-v5-text-small"
        or body.get("task") not in {"retrieval.passage", "retrieval.query"}
        or body.get("dimensions") != _DIMENSION
        or body.get("normalized") is not True
        or body.get("embedding_type") != "float"
        or body.get("truncate") is not False
    ):
        return httpx.Response(400)
    inputs = body.get("input")
    if not isinstance(inputs, list):
        return httpx.Response(400)
    vector = [0.125] * int(body.get("dimensions", 8))
    return httpx.Response(
        200,
        json={
            "data": [
                {"embedding": vector, "index": index}
                for index, _ in enumerate(inputs)
            ],
            "model": body["model"],
            "usage": {"total_tokens": 8},
        },
    )


def _aliyun_response(
    request: httpx.Request, body: dict[str, Any], expected_host: str
) -> httpx.Response:
    raw_input = body.get("input")
    parameters = body.get("parameters")
    if (
        request.url.host != expected_host
        or not isinstance(raw_input, dict)
        or not isinstance(parameters, dict)
        or body.get("model") != "qwen3.7-text-embedding"
        or parameters.get("text_type") not in {"document", "query"}
        or parameters.get("dimension") != _DIMENSION
        or parameters.get("output_type") != "dense"
        or any(key in body for key in ("dimensions", "region", "task"))
    ):
        return httpx.Response(400)
    text_type = parameters["text_type"]
    if (
        text_type == "query" and not isinstance(parameters.get("instruct"), str)
    ) or (text_type == "document" and "instruct" in parameters):
        return httpx.Response(400)
    texts = raw_input.get("texts")
    if not isinstance(texts, list):
        return httpx.Response(400)
    vector = [0.125] * int(parameters.get("dimension", 8))
    return httpx.Response(
        200,
        json={
            "code": "",
            "status_code": 200,
            "output": {
                "embeddings": [
                    {"embedding": vector, "text_index": index}
                    for index, _ in enumerate(texts)
                ]
            },
            "usage": {"total_tokens": 8},
        },
    )
