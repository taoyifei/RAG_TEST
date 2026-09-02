"""Embedding 与 Reranker 响应的格式中立校验。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def ordered_vectors(  # noqa: PLR0913
    items: object,
    *,
    expected_count: int,
    dimension: int,
    index_field: str,
    vector_field: str,
    normalize: bool = True,
) -> tuple[tuple[float, ...], ...]:
    """校验索引完整性、维度和有限数值并恢复输入顺序。

    Args:
        items: Provider 返回的向量条目。
        expected_count: 预期输入数量。
        dimension: 严格输出维度。
        index_field: Provider 使用的索引字段。
        vector_field: Provider 使用的向量字段。
        normalize: 是否执行版本化 L2 归一化。

    Returns:
        与输入顺序一致的向量。

    Raises:
        ValueError: 响应违反数量、索引、维度或数值合同。

    """
    if not isinstance(items, list):
        raise ValueError("embedding 响应条目必须是 list。")
    indexed: dict[int, tuple[float, ...]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("embedding 响应条目必须是 object。")
        index = item.get(index_field)
        values = item.get(vector_field)
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("embedding 响应索引必须是 integer。")
        if index in indexed or not 0 <= index < expected_count:
            raise ValueError("embedding 响应索引重复或越界。")
        vector = _finite_vector(values, dimension)
        indexed[index] = l2_normalize(vector) if normalize else vector
    if set(indexed) != set(range(expected_count)):
        raise ValueError("embedding 响应索引没有完整覆盖输入。")
    return tuple(indexed[index] for index in range(expected_count))


def l2_normalize(vector: Sequence[float]) -> tuple[float, ...]:
    """按 ``l2-v1`` 规则归一化有限非零向量。

    Args:
        vector: Provider 返回的原始向量。

    Returns:
        L2 范数为 1 的浮点 tuple。

    Raises:
        ValueError: 向量为空、含非有限值或全零。

    """
    converted = tuple(float(value) for value in vector)
    if not converted or any(not math.isfinite(value) for value in converted):
        raise ValueError("embedding 向量必须非空且全部有限。")
    norm = math.sqrt(sum(value * value for value in converted))
    if norm == 0:
        raise ValueError("embedding 向量禁止全零。")
    normalized = tuple(value / norm for value in converted)
    normalized_norm = math.sqrt(sum(value * value for value in normalized))
    if not math.isclose(normalized_norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError("embedding 向量 L2 归一化失败。")
    return normalized


def finite_score(value: object) -> float:
    """校验并返回有限 Provider 分数。

    Args:
        value: 未信任响应字段。

    Returns:
        有限浮点分数。

    Raises:
        ValueError: 值不是有限数字。

    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("reranker score 必须是数字。")
    score = float(value)
    if not math.isfinite(score):
        raise ValueError("reranker score 必须有限。")
    return score


def _finite_vector(values: object, dimension: int) -> tuple[float, ...]:
    if not isinstance(values, list) or len(values) != dimension:
        raise ValueError("embedding 向量维度不匹配。")
    converted: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("embedding 向量元素必须是数字。")
        converted.append(float(value))
    if any(not math.isfinite(value) for value in converted):
        raise ValueError("embedding 向量禁止包含 NaN 或 Inf。")
    if not any(value != 0 for value in converted):
        raise ValueError("embedding 向量禁止全零。")
    return tuple(converted)
