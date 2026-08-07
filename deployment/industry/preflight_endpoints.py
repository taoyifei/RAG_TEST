"""对 Industry 复用的模型服务执行最小只读合同检查。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any


class EndpointPreflightError(RuntimeError):
    """表示模型或外部 OCR endpoint 未通过最小合同。"""


_HTTP_SUCCESS_MIN = 200
_HTTP_REDIRECT_MIN = 300


def verify_endpoints(environment: Mapping[str, str]) -> dict[str, int]:
    """验证模型 endpoint 的 health、models 和最小请求。

    Args:
        environment: 只读环境变量映射。

    Returns:
        各类已验证 endpoint 数量。

    Raises:
        EndpointPreflightError: 配置或任一 HTTP 合同失败。

    """
    embedding = _endpoint_list(environment, "RAG_EMBEDDING_ENDPOINTS")
    reranker = _endpoint_list(environment, "RAG_RERANKER_ENDPOINTS")
    llm = _endpoint_list(environment, "RAG_LLM_ENDPOINTS")
    embedding_credential = environment.get("RAG_EMBEDDING_API_TOKEN", "")
    reranker_credential = environment.get("RAG_RERANKER_API_TOKEN", "")
    llm_credential = environment.get("RAG_LLM_API_TOKEN", "")
    ocr_credential = environment.get("RAG_OCR_API_TOKEN", "")
    for endpoint in (*embedding, *reranker, *llm):
        _request_status(endpoint, "/health")
    for endpoint in embedding:
        _request_json(endpoint, "/info", method="GET")
    for endpoint in llm:
        _request_json(endpoint, "/v1/models", method="GET")
    for endpoint in embedding:
        payload = _request_json(
            endpoint,
            "/v1/embeddings",
            payload={
                "encoding_format": "float",
                "input": ["health check"],
                "model": environment["RAG_EMBEDDING_MODEL"],
                "truncate": False,
            },
            token=embedding_credential,
        )
        if not isinstance(payload.get("data"), list):
            raise EndpointPreflightError("embedding 最小响应缺少 data。")
    for endpoint in reranker:
        payload = _request_json(
            endpoint,
            "/rerank",
            payload={
                "query": "health check",
                "texts": ["health check"],
                "truncate": False,
            },
            token=reranker_credential,
        )
        if not isinstance(payload.get("results"), list):
            raise EndpointPreflightError("reranker 最小响应缺少 results。")
    for endpoint in llm:
        payload = _request_json(
            endpoint,
            "/v1/chat/completions",
            payload={
                "max_tokens": 1,
                "messages": [{"content": "ping", "role": "user"}],
                "model": environment["RAG_LLM_MODEL"],
                "stream": False,
                "temperature": 0,
            },
            token=llm_credential,
        )
        if not isinstance(payload.get("choices"), list):
            raise EndpointPreflightError("LLM 最小响应缺少 choices。")
    if environment.get("RAG_OCR_MODE") == "external":
        ocr = _endpoint_list(environment, "RAG_OCR_ENDPOINTS")
        for endpoint in ocr:
            _request_json(
                endpoint,
                "/ready",
                method="GET",
                token=ocr_credential,
            )
    else:
        ocr = ()
    return {
        "embedding": len(embedding),
        "external_ocr": len(ocr),
        "llm": len(llm),
        "reranker": len(reranker),
    }


def _endpoint_list(
    environment: Mapping[str, str],
    name: str,
) -> tuple[str, ...]:
    try:
        value = json.loads(environment[name])
    except (KeyError, json.JSONDecodeError) as error:
        raise EndpointPreflightError(f"{name} 不是 JSON 数组。") from error
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(item, str)
            or not item.startswith(("http://", "https://"))
            for item in value
        )
    ):
        raise EndpointPreflightError(f"{name} 必须是非空 HTTP URL 数组。")
    normalized = tuple(item.rstrip("/") for item in value)
    if len(normalized) != len(set(normalized)):
        raise EndpointPreflightError(f"{name} 不能含重复 endpoint。")
    return normalized


def _request_status(endpoint: str, path: str) -> None:
    request = urllib.request.Request(  # noqa: S310
        f"{endpoint}{path}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=10,
        ) as response:
            if not (
                _HTTP_SUCCESS_MIN
                <= response.status
                < _HTTP_REDIRECT_MIN
            ):
                raise EndpointPreflightError("endpoint health 非 2xx。")
    except (OSError, urllib.error.HTTPError) as error:
        raise EndpointPreflightError("endpoint health 请求失败。") from error


def _request_json(
    endpoint: str,
    path: str,
    *,
    method: str = "POST",
    payload: dict[str, object] | None = None,
    token: str = "",
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310
        f"{endpoint}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=30,
        ) as response:
            value = json.loads(response.read())
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
    ) as error:
        raise EndpointPreflightError("endpoint HTTP/JSON 合同失败。") from error
    if not isinstance(value, dict):
        raise EndpointPreflightError("endpoint JSON 顶层必须是对象。")
    return value


def main() -> int:
    """执行环境中配置的 endpoint preflight。

    Returns:
        全部通过返回 0；异常产生非零退出码。

    """
    print(
        json.dumps(
            verify_endpoints(os.environ),
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
