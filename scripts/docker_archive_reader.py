"""提供 Docker OCI 归档的低层安全读取能力。"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_NAME_ANNOTATION = "io.containerd.image.name"
_REFERENCE_NAME_ANNOTATION = "org.opencontainers.image.ref.name"
_DOCKER_INDEX_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
_DOCKER_MANIFEST_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.v2+json"
)
_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    _DOCKER_INDEX_MEDIA_TYPE,
}
_JSON_SIZE_LIMIT = 16 * 1024 * 1024
_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    _DOCKER_MANIFEST_MEDIA_TYPE,
}
_READ_CHUNK_SIZE = 1024 * 1024
_SCHEMA_VERSION = 2


class DockerArchiveIdentityError(ValueError):
    """表示 Docker 归档身份或结构不可信。"""


@dataclass(frozen=True)
class _Descriptor:
    digest: str
    media_type: str
    size: int
    platform: str | None
    is_attestation: bool
    image_name: str | None
    reference_name: str | None


class _ArchiveReader:
    """提供无解包、按内容寻址的安全归档读取。"""

    def __init__(self, archive: tarfile.TarFile) -> None:
        self._archive = archive
        self._members: dict[str, tarfile.TarInfo] = {}
        self._verified_blobs: set[str] = set()
        self._blob_cache: dict[str, bytes] = {}
        for member in archive.getmembers():
            self._register_member(member)

    def _register_member(self, member: tarfile.TarInfo) -> None:
        name = member.name
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise DockerArchiveIdentityError(
                f"归档包含不安全路径：{name!r}"
            )
        if name in self._members:
            raise DockerArchiveIdentityError(
                f"归档包含重复成员：{name}"
            )
        if not member.isfile() and not member.isdir():
            raise DockerArchiveIdentityError(
                f"归档包含链接或特殊成员：{name}"
            )
        self._members[name] = member

    def read_json_member(self, name: str) -> object:
        """读取并解析一个归档顶层 JSON 文件。

        Args:
            name: 归档内的精确 POSIX 路径。

        Returns:
            解析后的 JSON 值。

        """
        content = self._read_regular_member(name, _JSON_SIZE_LIMIT)
        return _decode_json(content, name)

    def read_blob_json(self, descriptor: _Descriptor) -> object:
        """校验并解析 descriptor 指向的 JSON blob。

        Args:
            descriptor: 包含预期摘要和大小的 OCI descriptor。

        Returns:
            解析后的 JSON 值。

        """
        content = self._read_blob(descriptor, retain=True)
        if content is None:
            raise DockerArchiveIdentityError(
                f"未保留 JSON blob：{descriptor.digest}"
            )
        return _decode_json(content, descriptor.digest)

    def verify_blob(self, descriptor: _Descriptor) -> None:
        """流式校验 descriptor 指向的 blob，不保留内容。

        Args:
            descriptor: 包含预期摘要和大小的 OCI descriptor。

        Returns:
            无。

        """
        self._read_blob(descriptor, retain=False)

    def _read_blob(
        self,
        descriptor: _Descriptor,
        *,
        retain: bool,
    ) -> bytes | None:
        member = self._regular_member(_blob_name(descriptor.digest))
        if member.size != descriptor.size:
            raise DockerArchiveIdentityError(
                "descriptor size 与 blob 大小不一致："
                f"{descriptor.digest}"
            )
        if descriptor.digest in self._verified_blobs:
            cached = self._blob_cache.get(descriptor.digest)
            if retain and cached is None:
                return self._hash_member(
                    member,
                    descriptor.digest,
                    retain=True,
                )
            return cached
        content = self._hash_member(
            member,
            descriptor.digest,
            retain=retain,
        )
        self._verified_blobs.add(descriptor.digest)
        if content is not None:
            self._blob_cache[descriptor.digest] = content
        return content

    def _hash_member(
        self,
        member: tarfile.TarInfo,
        expected_digest: str,
        *,
        retain: bool,
    ) -> bytes | None:
        if retain and member.size > _JSON_SIZE_LIMIT:
            raise DockerArchiveIdentityError(
                f"JSON blob 超过大小上限：{expected_digest}"
            )
        extracted = self._archive.extractfile(member)
        if extracted is None:
            raise DockerArchiveIdentityError(
                f"无法读取归档成员：{member.name}"
            )
        hasher = hashlib.sha256()
        chunks: list[bytes] | None = [] if retain else None
        with extracted:
            while chunk := extracted.read(_READ_CHUNK_SIZE):
                hasher.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
        actual_digest = f"sha256:{hasher.hexdigest()}"
        if actual_digest != expected_digest:
            raise DockerArchiveIdentityError(
                "blob 内容摘要不一致："
                f"expected={expected_digest} actual={actual_digest}"
            )
        return b"".join(chunks) if chunks is not None else None

    def _read_regular_member(self, name: str, size_limit: int) -> bytes:
        member = self._regular_member(name)
        if member.size > size_limit:
            raise DockerArchiveIdentityError(
                f"归档成员超过大小上限：{name}"
            )
        extracted = self._archive.extractfile(member)
        if extracted is None:
            raise DockerArchiveIdentityError(
                f"无法读取归档成员：{name}"
            )
        with extracted:
            return extracted.read()

    def _regular_member(self, name: str) -> tarfile.TarInfo:
        member = self._members.get(name)
        if member is None or not member.isfile():
            raise DockerArchiveIdentityError(
                f"归档缺少普通文件：{name}"
            )
        return member


def _index_descriptors(
    payload: dict[str, object],
    label: str,
) -> list[_Descriptor]:
    if payload.get("schemaVersion") != _SCHEMA_VERSION:
        raise DockerArchiveIdentityError(
            f"OCI index schemaVersion 无效：{label}"
        )
    media_type = payload.get("mediaType")
    if media_type is not None and media_type not in _INDEX_MEDIA_TYPES:
        raise DockerArchiveIdentityError(
            f"OCI index mediaType 无效：{label}"
        )
    values = _list(payload.get("manifests"), f"{label} manifests")
    if not values:
        raise DockerArchiveIdentityError(f"OCI index 为空：{label}")
    return [
        _parse_descriptor(_mapping(value, f"{label} descriptor"))
        for value in values
    ]


def _parse_descriptor(payload: dict[str, object]) -> _Descriptor:
    digest = _required_string(payload, "digest", "descriptor digest")
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        raise DockerArchiveIdentityError(
            f"descriptor 仅允许 sha256：{digest}"
        )
    media_type = _required_string(
        payload,
        "mediaType",
        "descriptor mediaType",
    )
    size = payload.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise DockerArchiveIdentityError(
            f"descriptor size 无效：{digest}"
        )
    platform_payload = payload.get("platform")
    platform: str | None = None
    if platform_payload is not None:
        platform_mapping = _mapping(
            platform_payload,
            "descriptor platform",
        )
        operating_system = _required_string(
            platform_mapping,
            "os",
            "platform os",
        )
        architecture = _required_string(
            platform_mapping,
            "architecture",
            "platform architecture",
        )
        platform = f"{operating_system}/{architecture}"
    annotations = payload.get("annotations")
    is_attestation = False
    image_name: str | None = None
    reference_name: str | None = None
    if annotations is not None:
        annotation_mapping = _mapping(annotations, "descriptor annotations")
        if any(
            not isinstance(value, str)
            for value in annotation_mapping.values()
        ):
            raise DockerArchiveIdentityError(
                "descriptor annotation value 必须是字符串。"
            )
        is_attestation = (
            annotation_mapping.get("vnd.docker.reference.type")
            == "attestation-manifest"
        )
        annotated_image = annotation_mapping.get(_IMAGE_NAME_ANNOTATION)
        annotated_reference = annotation_mapping.get(
            _REFERENCE_NAME_ANNOTATION
        )
        if isinstance(annotated_image, str):
            image_name = annotated_image
        if isinstance(annotated_reference, str):
            reference_name = annotated_reference
    return _Descriptor(
        digest=digest,
        media_type=media_type,
        size=size,
        platform=platform,
        is_attestation=is_attestation,
        image_name=image_name,
        reference_name=reference_name,
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise DockerArchiveIdentityError(f"{label} 必须是 JSON object。")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise DockerArchiveIdentityError(f"{label} 必须是 JSON array。")
    return value


def _required_string(
    payload: dict[str, object],
    key: str,
    label: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DockerArchiveIdentityError(f"{label} 无效。")
    return value


def _decode_json(content: bytes, label: str) -> object:
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DockerArchiveIdentityError(
            f"JSON 内容无效：{label}"
        ) from error


def _blob_name(digest: str) -> str:
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        raise DockerArchiveIdentityError(f"blob digest 无效：{digest}")
    return f"blobs/sha256/{digest.removeprefix('sha256:')}"


def _digest_from_blob_name(name: str) -> str:
    prefix = "blobs/sha256/"
    if not name.startswith(prefix):
        raise DockerArchiveIdentityError(
            f"正式 release 仅允许 OCI blob 路径：{name}"
        )
    digest = f"sha256:{name.removeprefix(prefix)}"
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        raise DockerArchiveIdentityError(f"OCI blob 路径无效：{name}")
    return digest
