"""避免 Core schema 模块循环依赖的内部基础类型。"""

from __future__ import annotations

from datetime import datetime
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, JsonValue

JsonObject: TypeAlias = tuple[tuple[str, JsonValue], ...]
_KEY_VALUE_ITEM_LENGTH = 2


class FrozenModel(BaseModel):
    """为 Core schema 提供统一的严格外壳。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


def require_aware_datetime(value: datetime) -> datetime:
    """拒绝没有明确时区的时间。

    Args:
        value: 待验证的时间。

    Returns:
        原始且已确认带时区的时间。

    Raises:
        ValueError: 时间没有明确时区。

    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime 必须包含明确时区。")
    return value


def freeze_json_object(value: object) -> JsonObject:
    """把 JSON object 规范化为按键排序的 tuple。

    Args:
        value: dict 或已规范化的键值 tuple。

    Returns:
        不可变且按键排序的 JSON 键值 tuple。

    Raises:
        ValueError: 输入不是字符串键 JSON object 或包含重复键。

    """
    if isinstance(value, dict):
        items = tuple(value.items())
    elif isinstance(value, (tuple, list)):
        items = tuple(value)
    else:
        raise ValueError("metadata 必须是 JSON object。")
    normalized: list[tuple[str, JsonValue]] = []
    keys: set[str] = set()
    for item in items:
        if (
            not isinstance(item, (tuple, list))
            or len(item) != _KEY_VALUE_ITEM_LENGTH
        ):
            raise ValueError("JSON object 项必须是二元键值对。")
        key, item_value = item
        if not isinstance(key, str) or not key:
            raise ValueError("JSON object 键必须是非空字符串。")
        if key in keys:
            raise ValueError("JSON object 禁止重复键。")
        keys.add(key)
        normalized.append((key, item_value))
    return tuple(sorted(normalized, key=lambda pair: pair[0]))
