"""重排协议转换的下标、分数与失败关闭回归。"""

# pytest 断言用于核对完整协议映射。
# ruff: noqa: S101

from __future__ import annotations

import pytest
from rerank_adapter import ProtocolError, translate_request, translate_response


def test_preserves_order_indices_scores_and_duplicate_text() -> None:
    """重复正文不能丢失下标；分数和排序必须逐项保持。"""
    documents = ["研发项目", "研发项目", "测试流程"]
    request, source = translate_request(
        {
            "model": "Qwen3-Reranker-0.6B",
            "query": "研发",
            "documents": documents,
            "top_n": 3,
        }
    )
    assert request == {"query": "研发", "texts": documents, "truncate": False}
    assert source == documents

    converted = translate_response(
        {
            "results": [
                {"index": 2, "score": 0.01},
                {"index": 0, "score": 1.0},
                {"index": 1, "score": 0.8},
            ]
        },
        documents,
    )
    assert converted["results"] == [
        {"index": 2, "score": 0.01, "document": {"text": "测试流程"}},
        {"index": 0, "score": 1.0, "document": {"text": "研发项目"}},
        {"index": 1, "score": 0.8, "document": {"text": "研发项目"}},
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"query": "q", "documents": ["a"], "top_n": 0},
        {"query": "q", "documents": ["a", 2]},
        {"query": "q", "texts": ["a"]},
    ],
)
def test_rejects_unsupported_requests(body: dict[str, object]) -> None:
    """不支持的请求不得静默转成空结果。"""
    with pytest.raises(ProtocolError):
        translate_request(body)


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "score": float("nan")}],
        [{"index": 1, "score": 0.5}],
        [],
    ],
)
def test_rejects_invalid_upstream_results(
    results: list[dict[str, object]],
) -> None:
    """缺失、越界及非有限上游分数应失败关闭。"""
    with pytest.raises(ProtocolError):
        translate_response({"results": results}, ["研发项目"])
