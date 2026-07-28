"""为受控配置提供统一的重复字段拒绝语义。"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["load_json_file"]


class _DuplicateKeyError(ValueError):
    """内部重复字段信号，不携带字段名或配置内容。"""


def load_json_file(path: Path, *, label: str) -> object:
    """读取 UTF-8 JSON，并在模型校验前拒绝任意层级的重复字段。

    Args:
        path: 待读取的本地 JSON 文件。
        label: 不含路径、密钥或配置值的公开配置类别。

    Returns:
        保持数组顺序的 JSON 兼容对象。

    Raises:
        ValueError: 文件不可读、JSON 非法或存在重复字段。

    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"{label} 配置文件不可读。") from error
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except _DuplicateKeyError as error:
        raise ValueError(f"{label} 配置包含重复 JSON 字段。") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} 配置不是合法 JSON。") from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateKeyError
        payload[key] = value
    return payload
