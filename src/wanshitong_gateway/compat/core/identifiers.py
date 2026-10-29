"""登录签名与加密使用的稳定 JSON 编码。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

_KEY_VALUE_ITEM_LENGTH = 2


def canonical_json(value: object) -> str:
    """把 JSON 兼容值编码成稳定 UTF-8 文本形式。

    Args:
        value: 只含 JSON 值的对象。

    Returns:
        键排序、紧凑且保留 Unicode 的 JSON 文本。

    Raises:
        ValueError: 值包含 NaN、Inf、非字符串键或非 JSON 类型。

    """
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _normalize_json(value: object) -> object:  # noqa: PLR0911
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON 禁止 NaN 或 Inf。")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object 的键必须是字符串。")
        return {
            key: _normalize_json(item) for key, item in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        if _looks_like_object_items(value):
            return {
                str(item[0]): _normalize_json(item[1])
                for item in sorted(value, key=lambda pair: str(pair[0]))
            }
        return [_normalize_json(item) for item in value]
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=False)
        return _normalize_json(dumped)
    raise ValueError(f"canonical JSON 不支持类型 {type(value).__name__}。")


def _looks_like_object_items(value: Sequence[object]) -> bool:
    return bool(value) and all(
        isinstance(item, (tuple, list))
        and len(item) == _KEY_VALUE_ITEM_LENGTH
        and isinstance(item[0], str)
        for item in value
    )
