"""稳定 ID、规范化 JSON 和 SHA-256 工具。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections.abc import Mapping, Sequence

_PREFIXES = frozenset(
    {
        "prj",
        "kb",
        "doc",
        "dver",
        "node",
        "chunk",
        "irev",
        "job",
        "trace",
        "bref",
        "gcplan",
    }
)
_ID_PATTERN = re.compile(
    r"^(?P<prefix>prj|kb|doc|dver|node|chunk|irev|job|trace|bref|gcplan)_"
    r"(?P<digest>[0-9a-f]{32})$"
)
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


def canonical_sha256(value: object) -> str:
    """计算规范化 JSON 的带算法前缀摘要。

    Args:
        value: 只含 JSON 值的对象。

    Returns:
        `sha256:` 前缀的小写十六进制摘要。

    """
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def new_id(prefix: str) -> str:
    """生成受控随机逻辑 ID。

    Args:
        prefix: 允许的 ID 前缀，不含下划线。

    Returns:
        前缀加 128 位随机小写十六进制的 ID。

    Raises:
        ValueError: 前缀不在受控集合中。

    """
    _validate_prefix(prefix)
    return f"{prefix}_{secrets.token_hex(16)}"


def deterministic_id(prefix: str, *identity_parts: object) -> str:
    """由规范化逻辑身份生成确定性 ID。

    Args:
        prefix: 允许的 ID 前缀，不含下划线。
        *identity_parts: 不含绝对路径或显示名的 JSON 身份片段。

    Returns:
        相同输入跨进程保持一致的 ID。

    Raises:
        ValueError: 前缀无效或输入不是 JSON 值。

    """
    _validate_prefix(prefix)
    payload = {"prefix": prefix, "parts": identity_parts}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def validate_id(prefix: str, value: str) -> str:
    """校验 ID 的前缀和受控字形。

    Args:
        prefix: 期望前缀，不含下划线。
        value: 待验证 ID。

    Returns:
        原始且已验证的 ID。

    Raises:
        ValueError: 前缀不受支持或 ID 不匹配。

    """
    _validate_prefix(prefix)
    match = _ID_PATTERN.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise ValueError(f"ID 必须匹配前缀 {prefix}_ 和 32 位十六进制。")
    return value


def document_version_id(document_id: str, content_sha256: str) -> str:
    """从逻辑文档身份和内容摘要生成版本 ID。

    Args:
        document_id: 全局唯一的逻辑文档 ID。
        content_sha256: 64 位小写十六进制内容摘要。

    Returns:
        确定性的 `dver_` ID。

    Raises:
        ValueError: 内容摘要格式无效。

    """
    validate_id("doc", document_id)
    _require_sha256(content_sha256)
    return deterministic_id("dver", document_id, content_sha256)


def node_id(
    document_version: str,
    part_uri: str,
    structural_path: Sequence[str],
    node_type: str,
    content_sha256: str,
) -> str:
    """生成格式中立文档节点 ID。

    Args:
        document_version: 文档版本 ID。
        part_uri: 容器内逻辑 part URI，不是机器路径。
        structural_path: 文档内结构路径。
        node_type: 格式中立节点类型。
        content_sha256: 节点内容摘要。

    Returns:
        确定性的 `node_` ID。

    """
    validate_id("dver", document_version)
    _require_sha256(content_sha256)
    return deterministic_id(
        "node",
        document_version,
        part_uri,
        tuple(structural_path),
        node_type,
        content_sha256,
    )


def chunk_id(
    document_version: str,
    chunker_fingerprint: str,
    ordered_source_spans: Sequence[object],
    citation_text_sha256: str,
) -> str:
    """生成绑定版本、策略、来源跨度和引用文本的 chunk ID。

    Args:
        document_version: 文档版本 ID。
        chunker_fingerprint: 带算法前缀的 chunker 指纹。
        ordered_source_spans: 有序来源跨度身份。
        citation_text_sha256: 引用文本内容摘要。

    Returns:
        确定性的 `chunk_` ID。

    """
    validate_id("dver", document_version)
    _require_sha256(citation_text_sha256)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", chunker_fingerprint) is None:
        raise ValueError("chunker_fingerprint 必须是 sha256 指纹。")
    return deterministic_id(
        "chunk",
        document_version,
        chunker_fingerprint,
        tuple(ordered_source_spans),
        citation_text_sha256,
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


def _validate_prefix(prefix: str) -> None:
    if prefix not in _PREFIXES:
        raise ValueError(f"不支持的 ID 前缀：{prefix}。")


def _require_sha256(value: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("内容摘要必须是 64 位小写十六进制。")
