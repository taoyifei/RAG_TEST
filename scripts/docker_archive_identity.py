"""校验 Docker OCI 离线归档的可移植镜像身份。"""

from __future__ import annotations

import argparse
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from scripts.docker_archive_reader import (
    DockerArchiveIdentityError,
    _ArchiveReader,
    _Descriptor,
    _digest_from_blob_name,
    _index_descriptors,
    _list,
    _mapping,
    _parse_descriptor,
    _required_string,
)

_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_SCHEMA_VERSION = 2

__all__ = [
    "DockerArchiveIdentity",
    "DockerArchiveIdentityError",
    "inspect_docker_archive",
]


@dataclass(frozen=True)
class DockerArchiveIdentity:
    """描述可由 containerd 稳定加载的单平台镜像身份。"""

    manifest_digest: str
    config_digest: str
    tag: str
    platform: str
    revision: str | None


@dataclass(frozen=True)
class _ImageCandidate:
    manifest_digest: str
    config_digest: str
    layer_digests: tuple[str, ...]
    platform: str
    revision: str | None
    is_attestation: bool


@dataclass(frozen=True)
class _LegacyEntry:
    config_digest: str
    layer_digests: tuple[str, ...]


@dataclass(frozen=True)
class _ExpectedReference:
    original: str
    canonical_name: str
    tag_component: str


def inspect_docker_archive(
    archive_path: Path,
    *,
    expected_tag: str,
    expected_platform: str = "linux/amd64",
    expected_revision: str | None = None,
) -> DockerArchiveIdentity:
    """检查 Docker 29 双布局 OCI 归档并返回可移植身份。

    Args:
        archive_path: `docker image save` 生成的未压缩 tar 路径。
        expected_tag: `manifest.json` 必须精确包含的镜像引用。
        expected_platform: 唯一可运行镜像必须匹配的平台。
        expected_revision: 可选的 OCI 源代码 revision 标签期望值。

    Returns:
        已由原始 manifest/config/layer blob 验证的镜像身份。

    Raises:
        DockerArchiveIdentityError: 归档结构、摘要或身份不可信。

    """
    if not archive_path.is_file() or archive_path.is_symlink():
        raise DockerArchiveIdentityError(
            f"归档不是普通文件：{archive_path}"
        )
    expected_reference = _parse_expected_reference(expected_tag)
    _validate_platform(expected_platform)
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            reader = _ArchiveReader(archive)
            _verify_oci_layout(reader)
            legacy_entry = _legacy_entry(reader, expected_tag)
            root_index = _mapping(
                reader.read_json_member("index.json"),
                "index.json",
            )
            root_descriptors = _index_descriptors(root_index, "index.json")
            groups = [
                (
                    descriptor,
                    _collect_candidates(reader, descriptor, set()),
                )
                for descriptor in root_descriptors
            ]
    except (tarfile.TarError, OSError) as error:
        raise DockerArchiveIdentityError(
            f"无法读取 Docker 归档：{archive_path}"
        ) from error
    tagged_groups = [
        candidates
        for descriptor, candidates in groups
        if _root_binds_expected_tag(
            descriptor,
            expected_reference,
        )
    ]
    if len(tagged_groups) != 1:
        raise DockerArchiveIdentityError(
            "期望 tag 必须恰好绑定一个 OCI 根描述符。"
        )
    runnable = [
        candidate
        for candidate in tagged_groups[0]
        if candidate.platform == expected_platform
        and not candidate.is_attestation
    ]
    if len(runnable) != 1:
        raise DockerArchiveIdentityError(
            "目标平台必须恰有一个可运行 image manifest。"
        )
    identity = runnable[0]
    if not _matches_legacy(identity, legacy_entry):
        raise DockerArchiveIdentityError(
            "OCI manifest 与兼容 manifest.json 身份不一致。"
        )
    if expected_revision is not None and identity.revision != expected_revision:
        raise DockerArchiveIdentityError(
            "镜像 revision 与期望值不一致："
            f"expected={expected_revision} actual={identity.revision or '-'}"
        )
    return DockerArchiveIdentity(
        manifest_digest=identity.manifest_digest,
        config_digest=identity.config_digest,
        tag=expected_tag,
        platform=identity.platform,
        revision=identity.revision,
    )


def _verify_oci_layout(reader: _ArchiveReader) -> None:
    layout = _mapping(reader.read_json_member("oci-layout"), "oci-layout")
    if layout.get("imageLayoutVersion") != "1.0.0":
        raise DockerArchiveIdentityError(
            "OCI layout version 必须等于 1.0.0。"
        )


def _legacy_entry(
    reader: _ArchiveReader,
    expected_tag: str,
) -> _LegacyEntry:
    entries = _list(
        reader.read_json_member("manifest.json"),
        "manifest.json",
    )
    matches: list[dict[str, object]] = []
    for index, value in enumerate(entries):
        entry = _mapping(value, f"manifest.json[{index}]")
        tags = _string_list(entry.get("RepoTags"), "RepoTags")
        if len(tags) != len(set(tags)):
            raise DockerArchiveIdentityError(
                "manifest.json RepoTags 含重复值。"
            )
        if expected_tag in tags:
            if tags != [expected_tag]:
                raise DockerArchiveIdentityError(
                    "manifest.json RepoTags 必须仅包含期望 tag。"
                )
            matches.append(entry)
    if len(matches) != 1:
        raise DockerArchiveIdentityError(
            f"manifest.json 未唯一包含 tag：{expected_tag}"
        )
    entry = matches[0]
    config_path = _required_string(entry, "Config", "manifest.json Config")
    layer_paths = _string_list(entry.get("Layers"), "manifest.json Layers")
    return _LegacyEntry(
        config_digest=_digest_from_blob_name(config_path),
        layer_digests=tuple(
            _digest_from_blob_name(path) for path in layer_paths
        ),
    )


def _collect_candidates(
    reader: _ArchiveReader,
    descriptor: _Descriptor,
    ancestors: set[str],
) -> list[_ImageCandidate]:
    if descriptor.digest in ancestors:
        raise DockerArchiveIdentityError(
            f"OCI 描述符形成循环：{descriptor.digest}"
        )
    next_ancestors = {*ancestors, descriptor.digest}
    if descriptor.media_type in _INDEX_MEDIA_TYPES:
        payload = _mapping(
            reader.read_blob_json(descriptor),
            descriptor.digest,
        )
        candidates: list[_ImageCandidate] = []
        for child in _index_descriptors(payload, descriptor.digest):
            candidates.extend(
                _collect_candidates(reader, child, next_ancestors)
            )
        return candidates
    if descriptor.media_type in _MANIFEST_MEDIA_TYPES:
        return [_image_candidate(reader, descriptor)]
    reader.verify_blob(descriptor)
    return []


def _image_candidate(
    reader: _ArchiveReader,
    descriptor: _Descriptor,
) -> _ImageCandidate:
    manifest = _mapping(
        reader.read_blob_json(descriptor),
        descriptor.digest,
    )
    if manifest.get("schemaVersion") != _SCHEMA_VERSION:
        raise DockerArchiveIdentityError(
            f"image manifest schemaVersion 无效：{descriptor.digest}"
        )
    manifest_media_type = manifest.get("mediaType")
    if (
        manifest_media_type is not None
        and manifest_media_type != descriptor.media_type
    ):
        raise DockerArchiveIdentityError(
            f"image manifest mediaType 不一致：{descriptor.digest}"
        )
    config_descriptor = _parse_descriptor(
        _mapping(manifest.get("config"), "image manifest config")
    )
    config = _mapping(
        reader.read_blob_json(config_descriptor),
        config_descriptor.digest,
    )
    operating_system = _required_string(config, "os", "image config os")
    architecture = _required_string(
        config,
        "architecture",
        "image config architecture",
    )
    platform = f"{operating_system}/{architecture}"
    if descriptor.platform is not None and descriptor.platform != platform:
        raise DockerArchiveIdentityError(
            "descriptor 与 config 平台不一致："
            f"{descriptor.digest}"
        )
    layer_descriptors = [
        _parse_descriptor(_mapping(layer, "image layer descriptor"))
        for layer in _list(manifest.get("layers"), "image manifest layers")
    ]
    for layer_descriptor in layer_descriptors:
        reader.verify_blob(layer_descriptor)
    return _ImageCandidate(
        manifest_digest=descriptor.digest,
        config_digest=config_descriptor.digest,
        layer_digests=tuple(
            layer_descriptor.digest
            for layer_descriptor in layer_descriptors
        ),
        platform=platform,
        revision=_config_revision(config),
        is_attestation=descriptor.is_attestation,
    )


def _config_revision(config: dict[str, object]) -> str | None:
    container_config = config.get("config")
    if container_config is None:
        return None
    config_mapping = _mapping(container_config, "image config.config")
    labels = config_mapping.get("Labels")
    if labels is None:
        return None
    label_mapping = _mapping(labels, "image config labels")
    value = label_mapping.get("org.opencontainers.image.revision")
    if value is None:
        return None
    if not isinstance(value, str):
        raise DockerArchiveIdentityError("镜像 revision 标签不是字符串。")
    return value


def _matches_legacy(
    candidate: _ImageCandidate,
    legacy: _LegacyEntry,
) -> bool:
    return (
        candidate.config_digest == legacy.config_digest
        and candidate.layer_digests == legacy.layer_digests
    )


def _parse_expected_reference(expected_tag: str) -> _ExpectedReference:
    if (
        not expected_tag
        or "@" in expected_tag
        or any(character.isspace() for character in expected_tag)
    ):
        raise DockerArchiveIdentityError("期望镜像 tag 无效。")
    last_component = expected_tag.rsplit("/", maxsplit=1)[-1]
    if ":" not in last_component:
        raise DockerArchiveIdentityError("期望镜像必须包含显式 tag。")
    repository, tag_component = expected_tag.rsplit(":", maxsplit=1)
    if not repository or not tag_component:
        raise DockerArchiveIdentityError("期望镜像 tag 无效。")
    first_component = repository.split("/", maxsplit=1)[0]
    if "/" not in repository:
        canonical_repository = f"docker.io/library/{repository}"
    elif (
        "." not in first_component
        and ":" not in first_component
        and first_component != "localhost"
    ):
        canonical_repository = f"docker.io/{repository}"
    elif first_component == "index.docker.io":
        canonical_repository = repository.replace(
            "index.docker.io/",
            "docker.io/",
            1,
        )
    else:
        canonical_repository = repository
    return _ExpectedReference(
        original=expected_tag,
        canonical_name=f"{canonical_repository}:{tag_component}",
        tag_component=tag_component,
    )


def _root_binds_expected_tag(
    descriptor: _Descriptor,
    expected: _ExpectedReference,
) -> bool:
    if descriptor.is_attestation:
        if descriptor.image_name is not None \
            or descriptor.reference_name is not None:
            raise DockerArchiveIdentityError(
                "attestation 根描述符不得绑定镜像 tag。"
            )
        return False
    if descriptor.image_name is None and descriptor.reference_name is None:
        return False
    if descriptor.image_name is None or descriptor.reference_name is None:
        raise DockerArchiveIdentityError(
            "OCI 根描述符的镜像名称与 ref.name 必须同时存在。"
        )
    allowed_names = {expected.original, expected.canonical_name}
    if (
        descriptor.image_name not in allowed_names
        or descriptor.reference_name != expected.tag_component
    ):
        raise DockerArchiveIdentityError(
            "OCI 根描述符 tag 与期望镜像不一致。"
        )
    return True


def _string_list(value: object, label: str) -> list[str]:
    values = _list(value, label)
    strings: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise DockerArchiveIdentityError(f"{label} 仅允许字符串。")
        strings.append(item)
    return strings


def _validate_platform(platform: str) -> None:
    parts = platform.split("/")
    if len(parts) != _SCHEMA_VERSION or not all(parts):
        raise DockerArchiveIdentityError(
            f"平台必须使用 os/architecture 格式：{platform}"
        )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="校验 Docker OCI 归档并输出 portable identity。",
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--expected-revision")
    return parser.parse_args()


def main() -> int:
    """运行 Docker OCI 归档身份校验命令。

    Args:
        无参数；命令行选项从当前进程读取。

    Returns:
        校验成功返回 0，否则返回 1。

    """
    arguments = _arguments()
    try:
        identity = inspect_docker_archive(
            arguments.archive,
            expected_tag=arguments.tag,
            expected_platform=arguments.platform,
            expected_revision=arguments.expected_revision,
        )
    except DockerArchiveIdentityError as error:
        print(f"DOCKER_ARCHIVE_IDENTITY_INVALID: {error}", file=sys.stderr)
        return 1
    print(
        "\t".join(
            (
                identity.manifest_digest,
                identity.config_digest,
                identity.platform,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
