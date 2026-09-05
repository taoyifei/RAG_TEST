"""Probe 与 Adapter 共享的百炼 Native 请求和严格响应合同。"""

from collections.abc import Mapping, Sequence

from rag_app.adapters.providers.validation import ordered_vectors, usage_tokens


def embedding_payload(
    texts: Sequence[str],
    *,
    model: str,
    dimension: int,
    text_type: str,
    instruct: str,
) -> dict[str, object]:
    """构造固定 Native 请求，只有 query 包含 instruct。

    Args:
        texts: 当前批次文本。
        model: 已校验的模型。
        dimension: 输出维度。
        text_type: document 或 query。
        instruct: 已冻结的查询指令。

    Returns:
        不含端点元数据或凭据的请求体。

    """
    parameters: dict[str, object] = {
        "dimension": dimension,
        "output_type": "dense",
        "text_type": text_type,
    }
    if text_type == "query":
        parameters["instruct"] = instruct
    return {
        "model": model,
        "input": {"texts": list(texts)},
        "parameters": parameters,
    }


def decode_embeddings(
    payload: object,
    *,
    expected_count: int,
    dimension: int,
) -> tuple[tuple[tuple[float, ...], ...], int | None]:
    """按官方带业务状态的成功示例验证向量，不推测未实测形态。

    Args:
        payload: 已通过 HTTP 层检查的 JSON。
        expected_count: 输入文本数量。
        dimension: 约定向量维度。

    Returns:
        有序、有限且维度正确的向量和可选用量。

    Raises:
        TypeError: 响应类型无效。
        ValueError: 业务失败、重复索引、数量或向量合同无效。

    """
    if not isinstance(payload, Mapping):
        raise TypeError("百炼响应必须为 object。")
    if (
        payload.get("status_code") not in (200, "200")
        or payload.get("code") != ""
    ):
        raise ValueError("百炼业务状态无效。")
    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise TypeError("百炼 output 必须为 object。")
    vectors = ordered_vectors(
        output.get("embeddings"),
        expected_count=expected_count,
        dimension=dimension,
        index_field="text_index",
        vector_field="embedding",
    )
    return vectors, usage_tokens(payload)
