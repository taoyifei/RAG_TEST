"""只读核验 embedding、reranker 与 Qwen LLM 的 HTTP 契约。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

ServiceName = Literal["embedding", "reranker", "llm"]

_SERVICES = ("embedding", "reranker", "llm")
_PROBE_COUNT = 2
_SUCCESS_STATUS = 200
_REVISION_FIELDS = ("revision", "model_revision", "commit_hash", "sha")
_REVISION_HEADERS = (
    "x-model-revision",
    "x-service-revision",
    "x-revision",
)
_SAFE_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")
_REWRITE_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "query_rewrite",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "standalone_query": {"type": "string", "minLength": 1}
            },
            "required": ["standalone_query"],
            "additionalProperties": False,
        },
    },
}
_ANSWER_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "strict_evidence_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["answered", "refused"],
                },
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "minLength": 1},
                            "supports": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "evidence_id": {"type": "string"},
                                        "quote": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                    },
                                    "required": ["evidence_id", "quote"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["text", "supports"],
                        "additionalProperties": False,
                    },
                },
                "refusal_reason": {"type": ["string", "null"]},
            },
            "required": ["status", "claims", "refusal_reason"],
            "additionalProperties": False,
        },
    },
}
_LLM_CONTRACTS = (
    (
        "rewrite",
        _REWRITE_RESPONSE_FORMAT,
        "把依赖上文的合成追问改成独立问题，只输出 JSON。",
    ),
    (
        "answer",
        _ANSWER_RESPONSE_FORMAT,
        "合成证据不足，请严格拒答并只输出 JSON。",
    ),
)


class ContractError(RuntimeError):
    """不包含端点响应或请求正文的稳定契约错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModelContractOptions:
    """一次只读模型契约核验的输入。"""

    service: ServiceName
    endpoint: str
    model: str
    token: str
    dimension: int | None
    timeout_seconds: float

    def __post_init__(self) -> None:
        """拒绝无界、不完整或角色不兼容的输入。"""
        if self.service not in _SERVICES:
            raise ValueError("service 必须是 embedding、reranker 或 llm。")
        _normalize_endpoint(self.endpoint)
        if not self.model.strip() or not self.token:
            raise ValueError("model 与 token 不能为空。")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限正数。")
        if self.service == "embedding":
            if self.dimension is None or self.dimension <= 0:
                raise ValueError("embedding 必须提供正数 dimension。")
        elif self.dimension is not None:
            raise ValueError("只有 embedding 可以提供 dimension。")


def verify_model_contract(
    options: ModelContractOptions,
    *,
    client: httpx.Client,
) -> dict[str, object]:
    """执行 health、model ID、revision 和最小真实请求核验。

    Args:
        options: 单个模型服务的端点、模型、令牌与预算。
        client: 调用方管理生命周期的同步 HTTP 客户端。

    Returns:
        不含令牌、问题、prompt 或完整响应的脱敏报告。

    Raises:
        ContractError: 端点或响应不满足契约。

    """
    endpoint = _normalize_endpoint(options.endpoint)
    health = _request(
        client,
        options,
        "GET",
        f"{endpoint}/health",
    )
    models = _request_json(
        client,
        options,
        "GET",
        f"{endpoint}/v1/models",
    )
    model_entry = _require_model(models, options.model)
    revision = _endpoint_revision(model_entry, health.headers)
    probe = _run_probe(options, client, endpoint)
    return {
        "schema_version": "1",
        "status": "passed",
        "service": options.service,
        "endpoint": endpoint,
        "model": options.model,
        "endpoint_revision": revision,
        "health": "passed",
        "model_id": "passed",
        "probe": probe,
    }


def _run_probe(
    options: ModelContractOptions,
    client: httpx.Client,
    endpoint: str,
) -> dict[str, object]:
    if options.service == "embedding":
        return _probe_embedding(options, client, endpoint)
    if options.service == "reranker":
        return _probe_reranker(options, client, endpoint)
    return _probe_llm(options, client, endpoint)


def _probe_embedding(
    options: ModelContractOptions,
    client: httpx.Client,
    endpoint: str,
) -> dict[str, object]:
    payload = _request_json(
        client,
        options,
        "POST",
        f"{endpoint}/v1/embeddings",
        payload={
            "model": options.model,
            "input": ["contract probe alpha", "contract probe beta"],
            "truncate": False,
            "encoding_format": "float",
        },
    )
    if not isinstance(payload, dict) or payload.get("model") != options.model:
        raise ContractError("MODEL_MISMATCH")
    raw_data = payload.get("data")
    if not isinstance(raw_data, list) or len(raw_data) != _PROBE_COUNT:
        raise ContractError("EMBEDDING_INDEX_MISMATCH")
    indexes: set[int] = set()
    dimension = options.dimension
    if dimension is None:
        raise ContractError("INVALID_INPUT")
    for item in raw_data:
        if not isinstance(item, dict):
            raise ContractError("RESPONSE_SCHEMA_INVALID")
        index = item.get("index")
        vector = item.get("embedding")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(vector, list)
        ):
            raise ContractError("RESPONSE_SCHEMA_INVALID")
        if len(vector) != dimension:
            raise ContractError("EMBEDDING_DIMENSION_MISMATCH")
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in vector
        ):
            raise ContractError("RESPONSE_SCHEMA_INVALID")
        if any(not math.isfinite(float(value)) for value in vector):
            raise ContractError("EMBEDDING_NONFINITE")
        indexes.add(index)
    if indexes != set(range(_PROBE_COUNT)):
        raise ContractError("EMBEDDING_INDEX_MISMATCH")
    return {
        "count": _PROBE_COUNT,
        "dimension": dimension,
        "indexes": sorted(indexes),
        "finite": True,
    }


def _probe_reranker(
    options: ModelContractOptions,
    client: httpx.Client,
    endpoint: str,
) -> dict[str, object]:
    payload = _request_json(
        client,
        options,
        "POST",
        f"{endpoint}/rerank",
        payload={
            "query": "contract probe",
            "texts": ["candidate alpha", "candidate beta"],
            "truncate": False,
        },
    )
    if not isinstance(payload, dict) or not isinstance(
        payload.get("results"),
        list,
    ):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    results = payload["results"]
    if len(results) != _PROBE_COUNT:
        raise ContractError("RERANK_INDEX_MISMATCH")
    indexes: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            raise ContractError("RESPONSE_SCHEMA_INVALID")
        index = item.get("index")
        score = item.get("score")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ContractError("RESPONSE_SCHEMA_INVALID")
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 1.0
        ):
            raise ContractError("RERANK_SCORE_INVALID")
        indexes.add(index)
    if indexes != set(range(_PROBE_COUNT)):
        raise ContractError("RERANK_INDEX_MISMATCH")
    return {
        "count": _PROBE_COUNT,
        "indexes": sorted(indexes),
        "score_range": [0.0, 1.0],
    }


def _probe_llm(
    options: ModelContractOptions,
    client: httpx.Client,
    endpoint: str,
) -> dict[str, object]:
    contracts: dict[str, object] = {}
    for name, response_format, prompt in _LLM_CONTRACTS:
        payload = _request_json(
            client,
            options,
            "POST",
            f"{endpoint}/v1/chat/completions",
            payload={
                "model": options.model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "synthetic contract input"},
                ],
                "temperature": 0,
                "max_tokens": 128,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": response_format,
            },
        )
        contracts[name] = _parse_llm_response(
            payload,
            expected_model=options.model,
            contract=name,
        )
    return {
        **contracts,
        "temperature": 0,
        "thinking_enabled": False,
    }


def _parse_llm_response(
    payload: object,
    *,
    expected_model: str,
    contract: str,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    if payload.get("model") != expected_model:
        raise ContractError("MODEL_MISMATCH")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    finish_reason = choice.get("finish_reason")
    if finish_reason != "stop":
        code = (
            "LLM_TRUNCATED"
            if finish_reason == "length"
            else "LLM_STOP_INVALID"
        )
        raise ContractError(code)
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(
        message.get("content"),
        str,
    ):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    try:
        content: object = json.loads(message["content"])
    except (json.JSONDecodeError, TypeError) as error:
        raise ContractError("RESPONSE_SCHEMA_INVALID") from error
    if contract == "rewrite":
        _require_rewrite_content(content)
    else:
        _require_answer_content(content)
    return {
        "finish_reason": "stop",
        "usage": _require_usage(payload.get("usage")),
    }


def _require_rewrite_content(content: object) -> None:
    if (
        not isinstance(content, dict)
        or set(content) != {"standalone_query"}
        or not isinstance(content["standalone_query"], str)
        or not content["standalone_query"].strip()
    ):
        raise ContractError("RESPONSE_SCHEMA_INVALID")


def _require_answer_content(content: object) -> None:
    if not isinstance(content, dict) or set(content) != {
        "status",
        "claims",
        "refusal_reason",
    }:
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    status = content["status"]
    claims = content["claims"]
    reason = content["refusal_reason"]
    if status not in {"answered", "refused"} or not isinstance(claims, list):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    if status == "refused":
        if claims or not isinstance(reason, str) or not reason.strip():
            raise ContractError("RESPONSE_SCHEMA_INVALID")
        return
    if reason is not None or not claims:
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    for claim in claims:
        _require_claim(claim)


def _require_claim(claim: object) -> None:
    if (
        not isinstance(claim, dict)
        or set(claim) != {"text", "supports"}
        or not isinstance(claim["text"], str)
        or not claim["text"].strip()
        or not isinstance(claim["supports"], list)
        or not claim["supports"]
    ):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    for support in claim["supports"]:
        if (
            not isinstance(support, dict)
            or set(support) != {"evidence_id", "quote"}
            or any(
                not isinstance(support[field], str)
                or not support[field].strip()
                for field in ("evidence_id", "quote")
            )
        ):
            raise ContractError("RESPONSE_SCHEMA_INVALID")


def _require_usage(raw_usage: object) -> dict[str, int]:
    if not isinstance(raw_usage, dict):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    usage: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw_usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ContractError("RESPONSE_SCHEMA_INVALID")
        usage[field] = value
    if usage["total_tokens"] != (
        usage["prompt_tokens"] + usage["completion_tokens"]
    ):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    return usage


def _request_json(
    client: httpx.Client,
    options: ModelContractOptions,
    method: str,
    url: str,
    *,
    payload: dict[str, object] | None = None,
) -> object:
    response = _request(
        client,
        options,
        method,
        url,
        payload=payload,
    )
    try:
        parsed: object = response.json()
    except ValueError as error:
        raise ContractError("RESPONSE_SCHEMA_INVALID") from error
    return parsed


def _request(
    client: httpx.Client,
    options: ModelContractOptions,
    method: str,
    url: str,
    *,
    payload: dict[str, object] | None = None,
) -> httpx.Response:
    try:
        response = client.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {options.token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=options.timeout_seconds,
        )
    except httpx.HTTPError as error:
        raise ContractError("ENDPOINT_FAILURE") from error
    if response.status_code != _SUCCESS_STATUS:
        raise ContractError("ENDPOINT_FAILURE")
    return response


def _require_model(payload: object, expected_model: str) -> dict[str, object]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("data"),
        list,
    ):
        raise ContractError("RESPONSE_SCHEMA_INVALID")
    matches = [
        item
        for item in payload["data"]
        if isinstance(item, dict) and item.get("id") == expected_model
    ]
    if len(matches) != 1:
        raise ContractError("MODEL_MISMATCH")
    return matches[0]


def _endpoint_revision(
    model_entry: dict[str, object],
    health_headers: httpx.Headers,
) -> str:
    candidates = [
        model_entry.get(field) for field in _REVISION_FIELDS
    ] + [health_headers.get(header) for header in _REVISION_HEADERS]
    for candidate in candidates:
        if isinstance(candidate, str) and _SAFE_REVISION.fullmatch(candidate):
            return candidate
    raise ContractError("REVISION_MISSING")


def _normalize_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "endpoint 必须是不含凭据、query 或 fragment 的 HTTP URL。"
        )
    return endpoint.strip().rstrip("/")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=_SERVICES)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--dimension", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    """运行单个真实模型端点契约并输出脱敏 JSON。

    Args:
        无参数；从命令行读取端点、模型和令牌环境变量名。

    Returns:
        全部契约通过返回 0，否则返回 1。

    """
    arguments = _arguments()
    try:
        token = os.environ.get(arguments.token_env)
        if not token:
            raise ContractError("TOKEN_ENV_MISSING")
        options = ModelContractOptions(
            service=arguments.service,
            endpoint=arguments.endpoint,
            model=arguments.model,
            token=token,
            dimension=arguments.dimension,
            timeout_seconds=arguments.timeout_seconds,
        )
        with httpx.Client(
            timeout=options.timeout_seconds,
            trust_env=False,
        ) as client:
            report = verify_model_contract(options, client=client)
        exit_code = 0
    except (ContractError, ValueError) as error:
        code = (
            error.code
            if isinstance(error, ContractError)
            else "INVALID_INPUT"
        )
        report = {
            "schema_version": "1",
            "status": "failed",
            "service": arguments.service,
            "model": arguments.model,
            "error_code": code,
        }
        exit_code = 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
