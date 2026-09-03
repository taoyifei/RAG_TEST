"""P08 Run 目录、规范 JSON 和安全 Manifest I/O。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

_FORBIDDEN_MANIFEST_KEYS = {
    "api_key",
    "authorization",
    "content",
    "document_text",
    "prompt",
    "query",
    "raw_response",
    "secret",
    "vector",
}
_WINDOWS_PATH_PREFIX_LENGTH = 2


def create_run_directory(root: Path, run_id: str) -> Path:
    """以排他方式创建永不覆盖的 Run 目录。

    Args:
        root: `evaluation/reports` 根目录。
        run_id: 已由调用方生成并校验的 Run ID。

    Returns:
        新建的唯一 Run 目录。

    Raises:
        FileExistsError: 同名 Run 已存在。

    """
    root.mkdir(parents=True, exist_ok=True)
    run_directory = root / run_id
    run_directory.mkdir(exist_ok=False)
    return run_directory


def write_json(path: Path, value: object) -> str:
    """写出 canonical JSON 并返回带前缀摘要。

    Args:
        path: 尚不存在的目标文件。
        value: Pydantic 模型或 JSON 兼容对象。

    Returns:
        实际写出字节的 `sha256:` 摘要。

    Raises:
        FileExistsError: 目标已存在。

    """
    payload = _json_value(value)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    with path.open("xb") as output:
        output.write(encoded)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def write_jsonl(path: Path, values: Sequence[BaseModel]) -> str:
    """以稳定顺序排他写出模型 JSONL。

    Args:
        path: 尚不存在的目标文件。
        values: 已按业务稳定顺序排列的模型。

    Returns:
        实际写出字节的 `sha256:` 摘要。

    Raises:
        FileExistsError: 目标已存在。

    """
    lines = [
        json.dumps(
            item.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for item in values
    ]
    encoded = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
    with path.open("xb") as output:
        output.write(encoded)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def write_text(path: Path, value: str) -> str:
    """排他写出 UTF-8 文本并返回摘要。

    Args:
        path: 尚不存在的目标文件。
        value: 待写出的文本。

    Returns:
        实际字节的 `sha256:` 摘要。

    Raises:
        FileExistsError: 目标已存在。

    """
    encoded = value.encode("utf-8")
    with path.open("xb") as output:
        output.write(encoded)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def validate_manifest_safety(value: BaseModel) -> None:
    """拒绝 Manifest 中的正文、Secret、向量或绝对路径字段。

    Args:
        value: 待写出的 Run Manifest。

    Returns:
        无返回值；安全时正常结束。

    Raises:
        ValueError: 出现被禁止的键或绝对路径值。

    """
    _inspect_value(value.model_dump(mode="json"), path=())


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _inspect_value(value: object, *, path: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_MANIFEST_KEYS:
                location = ".".join((*path, str(key)))
                raise ValueError(f"Manifest 禁止字段：{location}")
            _inspect_value(item, path=(*path, str(key)))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _inspect_value(item, path=(*path, str(index)))
        return
    if isinstance(value, str) and (
        value.startswith("/")
        or (
            len(value) > _WINDOWS_PATH_PREFIX_LENGTH
            and value[1:3] in {":\\", ":/"}
        )
    ):
        location = ".".join(path)
        raise ValueError(f"Manifest 禁止绝对路径：{location}")


__all__ = [
    "create_run_directory",
    "validate_manifest_safety",
    "write_json",
    "write_jsonl",
    "write_text",
]
