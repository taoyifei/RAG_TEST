"""验证隔离 A/B 使用的内网三模型 HTTP 契约。"""

# 外部 JSON 响应形状在运行时校验，因此此处保留动态类型。
# ruff: noqa: ANN401

from __future__ import annotations

import argparse
import json
import math
import urllib.error
import urllib.request
from typing import Any


def _request(
    url: str, payload: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """发送小型合成请求，仅返回解析后的响应。"""
    if not url.startswith("http://"):
        raise ValueError("仅允许内网 HTTP 端点")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = {"error_type": "non_json_response"}
        return error.code, detail


def _embedding_shape(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"response_type": type(payload).__name__}
    entries = payload.get("data")
    if not isinstance(entries, list):
        return {"response_keys": sorted(payload)}
    dimensions: list[int] = []
    finite = True
    for entry in entries:
        vector = entry.get("embedding") if isinstance(entry, dict) else None
        if not isinstance(vector, list):
            return {"error": "missing_embedding_array"}
        dimensions.append(len(vector))
        finite = finite and all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in vector
        )
    return {"count": len(entries), "dimensions": dimensions, "finite": finite}


def _rerank_shape(payload: Any) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        return {"response_type": type(payload).__name__}
    indices: list[int | None] = []
    scores: list[float | None] = []
    for item in results:
        if not isinstance(item, dict):
            return {"error": "non_object_result"}
        indices.append(item.get("index"))
        scores.append(item.get("relevance_score", item.get("score")))
    return {
        "container": "object" if isinstance(payload, dict) else "array",
        "response_keys": sorted(payload) if isinstance(payload, dict) else None,
        "status": payload.get("status") if isinstance(payload, dict) else None,
        "message": payload.get("message")
        if isinstance(payload, dict)
        else None,
        "indices": indices,
        "scores": scores,
        "finite": all(
            isinstance(score, (int, float)) and math.isfinite(score)
            for score in scores
        ),
    }


def main() -> None:
    """运行合成请求并输出脱敏契约结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm-base", required=True)
    parser.add_argument("--llm-model", required=True)
    parser.add_argument("--embedding-base", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--rerank-url", required=True)
    parser.add_argument("--rerank-model", required=True)
    args = parser.parse_args()

    report: dict[str, Any] = {}
    status, models = _request(args.llm_base.rstrip("/") + "/models")
    report["llm_models"] = {
        "http_status": status,
        "models": [
            {"id": item.get("id"), "max_model_len": item.get("max_model_len")}
            for item in models.get("data", [])
        ]
        if isinstance(models, dict)
        else [],
    }

    status, answer = _request(
        args.llm_base.rstrip("/") + "/chat/completions",
        {
            "model": args.llm_model,
            "messages": [{"role": "user", "content": "只回答：收到"}],
            "max_tokens": 24,
            "temperature": 0,
            "stream": False,
        },
    )
    report["llm_chat"] = {
        "http_status": status,
        "choices": len(answer.get("choices", []))
        if isinstance(answer, dict)
        else None,
        "finish_reasons": [
            item.get("finish_reason") for item in answer.get("choices", [])
        ]
        if isinstance(answer, dict)
        else [],
        "usage": answer.get("usage") if isinstance(answer, dict) else None,
    }

    for label, texts in (
        ("single", ["研发项目名称建议控制在多少字以内？"]),
        ("batch", ["研发工时", "项目立项流程"]),
        ("empty", [""]),
        ("long", ["研发管理。" * 600]),
    ):
        status, result = _request(
            args.embedding_base.rstrip("/") + "/embeddings",
            {"model": args.embedding_model, "input": texts},
        )
        report["embedding_" + label] = {
            "http_status": status,
            **_embedding_shape(result),
        }

    for label, documents in (
        ("empty", []),
        ("single", ["项目名称尽量控制在三十字以内。"]),
        (
            "batch",
            [
                "研发项目名称不超过三十字。",
                "信息安全制度。",
                "研发项目名称不超过三十字。",
            ],
        ),
    ):
        for field in ("documents", "texts"):
            status, result = _request(
                args.rerank_url,
                {
                    "model": args.rerank_model,
                    "query": "研发项目名称长度要求",
                    field: documents,
                },
            )
            report[f"rerank_{field}_{label}"] = {
                "http_status": status,
                **_rerank_shape(result),
            }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
