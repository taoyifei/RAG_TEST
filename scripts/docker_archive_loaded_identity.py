"""校验 Docker 加载后的镜像身份，兼容 containerd 与 classic store。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Literal

StoreMode = Literal["containerd", "classic"]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_MAX_INSPECT_BYTES = 1024 * 1024


class LoadedImageIdentityError(ValueError):
    """表示已加载镜像不满足离线包记录的身份。"""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    """要求值为 JSON object。

    Args:
        value: 待检查值。
        label: 错误消息中的字段名。

    Returns:
        已验证的只读映射。

    Raises:
        LoadedImageIdentityError: 值不是 JSON object。

    """
    if not isinstance(value, Mapping):
        raise LoadedImageIdentityError(f"{label} 必须是 JSON object。")
    return value


def _required_string(
    payload: Mapping[str, object],
    key: str,
) -> str:
    """读取非空字符串字段。

    Args:
        payload: 字段所在映射。
        key: 字段名。

    Returns:
        已验证字符串。

    Raises:
        LoadedImageIdentityError: 字段缺失或类型错误。

    """
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LoadedImageIdentityError(f"Docker inspect {key} 无效。")
    return value


def _validate_expected_digest(value: str, label: str) -> None:
    """校验调用方提供的 SHA256 digest。

    Args:
        value: 待检查 digest。
        label: 错误消息中的字段名。

    Raises:
        LoadedImageIdentityError: digest 格式无效。

    """
    if _DIGEST.fullmatch(value) is None:
        raise LoadedImageIdentityError(f"{label} 无效。")


def _inspect_object(payload: object) -> Mapping[str, object]:
    """取得唯一 Docker image inspect 对象。

    Args:
        payload: Docker image inspect JSON。

    Returns:
        唯一镜像对象。

    Raises:
        LoadedImageIdentityError: 结果不是单镜像数组。

    """
    if (
        not isinstance(payload, Sequence)
        or isinstance(payload, (str, bytes, bytearray))
        or len(payload) != 1
    ):
        raise LoadedImageIdentityError(
            "Docker image inspect 必须恰好返回一个镜像。"
        )
    return _mapping(payload[0], "Docker image inspect[0]")


def _validate_revision(
    inspect: Mapping[str, object],
    expected_revision: str | None,
) -> None:
    """校验可选 OCI source revision。

    Args:
        inspect: 单镜像 inspect 对象。
        expected_revision: app/OCR 的完整 Git revision；Qdrant 传空。

    Raises:
        LoadedImageIdentityError: revision 缺失或不一致。

    """
    if expected_revision is None:
        return
    if _REVISION.fullmatch(expected_revision) is None:
        raise LoadedImageIdentityError("expected revision 无效。")
    config = _mapping(inspect.get("Config"), "Docker inspect Config")
    labels = _mapping(config.get("Labels"), "Docker inspect Config.Labels")
    if labels.get("org.opencontainers.image.revision") != expected_revision:
        raise LoadedImageIdentityError("镜像 OCI revision 不一致。")


def validate_loaded_image_identity(
    payload: object,
    *,
    expected_manifest_digest: str,
    expected_config_digest: str,
    expected_platform: str,
    expected_revision: str | None,
) -> StoreMode:
    """验证已加载镜像并判定 Docker image store 模式。

    containerd store 必须提供等于 manifest digest 的 Descriptor；其 `.Id`
    可等于 manifest 或 config digest。classic store 不提供 Descriptor，且
    `.Id` 必须等于 config digest。

    Args:
        payload: `docker image inspect` 的 JSON 输出。
        expected_manifest_digest: `IMAGE_ARCHIVES.tsv` 的 manifest digest。
        expected_config_digest: `IMAGE_ARCHIVES.tsv` 的 config digest。
        expected_platform: 固定目标平台，当前为 `linux/amd64`。
        expected_revision: app/OCR 的 OCI revision；Qdrant 传空。

    Returns:
        检出的 `containerd` 或 `classic` store 模式。

    Raises:
        LoadedImageIdentityError: 任一身份、平台或 revision 不一致。

    """
    _validate_expected_digest(expected_manifest_digest, "manifest digest")
    _validate_expected_digest(expected_config_digest, "config digest")
    inspect = _inspect_object(payload)
    image_id = _required_string(inspect, "Id")
    _validate_expected_digest(image_id, "Docker inspect Id")
    operating_system = _required_string(inspect, "Os")
    architecture = _required_string(inspect, "Architecture")
    actual_platform = "/".join(
        (operating_system, architecture)
    )
    if actual_platform != expected_platform:
        raise LoadedImageIdentityError(
            "镜像平台不一致："
            f"expected={expected_platform} actual={actual_platform}"
        )
    _validate_revision(inspect, expected_revision)

    descriptor = inspect.get("Descriptor")
    if descriptor is None:
        if image_id != expected_config_digest:
            raise LoadedImageIdentityError(
                "classic store 的 Docker inspect Id 必须等于 config digest。"
            )
        return "classic"

    descriptor_payload = _mapping(descriptor, "Docker inspect Descriptor")
    descriptor_digest = _required_string(descriptor_payload, "digest")
    _validate_expected_digest(descriptor_digest, "Descriptor.digest")
    if descriptor_digest != expected_manifest_digest:
        raise LoadedImageIdentityError(
            "Descriptor.digest 必须等于 manifest digest。"
        )
    if image_id not in {expected_manifest_digest, expected_config_digest}:
        raise LoadedImageIdentityError(
            "containerd store 的 Docker inspect Id "
            "不属于 manifest/config digest。"
        )
    return "containerd"


def _parser() -> argparse.ArgumentParser:
    """构建命令行解析器。

    Returns:
        已配置的参数解析器。

    """
    parser = argparse.ArgumentParser(
        description="校验从 stdin 读取的 docker image inspect JSON。"
    )
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--config-digest", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--expected-revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行加载后镜像身份校验。

    Args:
        argv: 可选命令行参数；为空时读取进程参数。

    Returns:
        成功为 0，输入或身份错误为 1。

    """
    arguments = _parser().parse_args(argv)
    raw_payload = sys.stdin.buffer.read(_MAX_INSPECT_BYTES + 1)
    try:
        if len(raw_payload) > _MAX_INSPECT_BYTES:
            raise LoadedImageIdentityError("Docker inspect JSON 超过大小上限。")
        payload = json.loads(raw_payload)
        mode = validate_loaded_image_identity(
            payload,
            expected_manifest_digest=arguments.manifest_digest,
            expected_config_digest=arguments.config_digest,
            expected_platform=arguments.platform,
            expected_revision=arguments.expected_revision,
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("Docker inspect JSON 无效。", file=sys.stderr)
        return 1
    except LoadedImageIdentityError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
