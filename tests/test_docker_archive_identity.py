from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.docker_archive_identity import (
    DockerArchiveIdentityError,
    inspect_docker_archive,
)

_ATTESTATION_ANNOTATION = "vnd.docker.reference.type"
_ATTESTATION_VALUE = "attestation-manifest"
_OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_OCI_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
_OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_IMAGE_NAME_ANNOTATION = "io.containerd.image.name"
_REFERENCE_NAME_ANNOTATION = "org.opencontainers.image.ref.name"
_TAG = "docx-rag:test"
_CANONICAL_TAG = "docker.io/library/docx-rag:test"
_TAG_COMPONENT = "test"
_TARGET_PLATFORM = "linux/amd64"


@dataclass(frozen=True)
class _ImageArtifacts:
    """保存测试镜像的描述符、内容 blob 与身份摘要。"""

    descriptor: dict[str, object]
    blobs: dict[str, bytes]
    manifest_digest: str
    config_digest: str
    layer_digest: str
    reserialized_manifest_digest: str


@dataclass(frozen=True)
class _ArchiveFixture:
    """保存归档路径及测试断言所需的独立摘要。"""

    path: Path
    container_index_digest: str
    manifest_digest: str
    config_digest: str
    reserialized_manifest_digest: str


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_bytes(
    payload: object,
    *,
    pretty: bool = False,
) -> bytes:
    if pretty:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
    else:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return f"{text}\n".encode()


def _descriptor(
    content: bytes,
    media_type: str,
    *,
    platform: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "digest": _digest(content),
        "mediaType": media_type,
        "size": len(content),
    }
    if platform is not None:
        descriptor["platform"] = platform
    if annotations is not None:
        descriptor["annotations"] = annotations
    return descriptor


def _blob_path(digest: str) -> str:
    algorithm, value = digest.split(":", maxsplit=1)
    return f"blobs/{algorithm}/{value}"


def _make_runnable_image(
    operating_system: str,
    architecture: str,
    suffix: str,
    *,
    config_architecture: str | None = None,
) -> _ImageArtifacts:
    layer = f"layer:{operating_system}:{architecture}:{suffix}".encode()
    layer_digest = _digest(layer)
    config_payload = {
        "architecture": config_architecture or architecture,
        "config": {},
        "os": operating_system,
        "rootfs": {
            "diff_ids": [layer_digest],
            "type": "layers",
        },
    }
    config = _json_bytes(config_payload)
    config_digest = _digest(config)
    manifest_payload = {
        "schemaVersion": 2,
        "mediaType": _OCI_MANIFEST_MEDIA_TYPE,
        "config": _descriptor(config, _OCI_CONFIG_MEDIA_TYPE),
        "layers": [_descriptor(layer, _OCI_LAYER_MEDIA_TYPE)],
    }
    # 保留非规范化空白，确保实现对原始 blob 求摘要而不是重编码 JSON。
    manifest = _json_bytes(manifest_payload, pretty=True)
    canonical_manifest = _json_bytes(manifest_payload)
    manifest_digest = _digest(manifest)
    descriptor = _descriptor(
        manifest,
        _OCI_MANIFEST_MEDIA_TYPE,
        platform={
            "architecture": architecture,
            "os": operating_system,
        },
    )
    return _ImageArtifacts(
        descriptor=descriptor,
        blobs={
            config_digest: config,
            layer_digest: layer,
            manifest_digest: manifest,
        },
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        layer_digest=layer_digest,
        reserialized_manifest_digest=_digest(canonical_manifest),
    )


def _make_attestation(subject_digest: str) -> _ImageArtifacts:
    image = _make_runnable_image(
        "unknown",
        "unknown",
        f"attestation:{subject_digest}",
    )
    descriptor = dict(image.descriptor)
    descriptor["annotations"] = {
        _ATTESTATION_ANNOTATION: _ATTESTATION_VALUE,
        "vnd.docker.reference.digest": subject_digest,
    }
    return _ImageArtifacts(
        descriptor=descriptor,
        blobs=image.blobs,
        manifest_digest=image.manifest_digest,
        config_digest=image.config_digest,
        layer_digest=image.layer_digest,
        reserialized_manifest_digest=image.reserialized_manifest_digest,
    )


def _write_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w") as archive:
        for member_name in sorted(members):
            content = members[member_name]
            member = tarfile.TarInfo(member_name)
            member.mode = 0o644
            member.mtime = 0
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def _legacy_identity_digests(
    variants: tuple[str, ...],
    target: _ImageArtifacts,
    blobs: dict[str, bytes],
) -> tuple[str, str]:
    config_digest = target.config_digest
    layer_digest = target.layer_digest
    if not {
        "legacy_config_mismatch",
        "legacy_layer_mismatch",
    }.intersection(variants):
        return config_digest, layer_digest
    legacy_image = _make_runnable_image(
        "linux",
        "amd64",
        "legacy-mismatch",
    )
    blobs.update(legacy_image.blobs)
    if "legacy_config_mismatch" in variants:
        config_digest = legacy_image.config_digest
    if "legacy_layer_mismatch" in variants:
        layer_digest = legacy_image.layer_digest
    return config_digest, layer_digest


def _descriptor_size_delta(variants: tuple[str, ...]) -> int:
    if "descriptor_size_too_small" in variants:
        return -1
    if "descriptor_size_too_large" in variants:
        return 1
    return 0


def _build_archive(
    tmp_path: Path,
    *,
    variants: tuple[str, ...] = (),
    nested_index: bool = True,
) -> _ArchiveFixture:
    config_architecture = (
        "arm64" if "config_platform_mismatch" in variants else None
    )
    target = _make_runnable_image(
        "linux",
        "amd64",
        "target",
        config_architecture=config_architecture,
    )
    children = [target]
    if "other_platform" in variants:
        children.append(_make_runnable_image("linux", "arm64", "other"))
    if "attestation" in variants:
        children.append(_make_attestation(target.manifest_digest))
    if "duplicate_target" in variants:
        children.append(_make_runnable_image("linux", "amd64", "duplicate"))

    blobs: dict[str, bytes] = {}
    for image in children:
        blobs.update(image.blobs)

    legacy_config_digest, legacy_layer_digest = _legacy_identity_digests(
        variants,
        target,
        blobs,
    )
    descriptor_size_delta = _descriptor_size_delta(variants)

    if nested_index:
        child_descriptors = [dict(image.descriptor) for image in children]
        if descriptor_size_delta:
            child_descriptors[0]["size"] = (
                len(target.blobs[target.manifest_digest])
                + descriptor_size_delta
            )
        source_index = _json_bytes(
            {
                "manifests": child_descriptors,
                "mediaType": _OCI_INDEX_MEDIA_TYPE,
                "schemaVersion": 2,
            }
        )
        container_index_digest = _digest(source_index)
        blobs[container_index_digest] = source_index
        root_descriptor = _descriptor(
            source_index,
            _OCI_INDEX_MEDIA_TYPE,
            annotations={
                _IMAGE_NAME_ANNOTATION: (
                    "docker.io/library/docx-rag:wrong"
                    if "oci_tag_mismatch" in variants
                    else _CANONICAL_TAG
                ),
                _REFERENCE_NAME_ANNOTATION: _TAG_COMPONENT,
            },
        )
    else:
        container_index_digest = ""
        root_descriptor = dict(target.descriptor)
        root_descriptor["annotations"] = {
            _IMAGE_NAME_ANNOTATION: (
                "docker.io/library/docx-rag:wrong"
                if "oci_tag_mismatch" in variants
                else _CANONICAL_TAG
            ),
            _REFERENCE_NAME_ANNOTATION: _TAG_COMPONENT,
        }
        if descriptor_size_delta:
            root_descriptor["size"] = (
                len(target.blobs[target.manifest_digest])
                + descriptor_size_delta
            )

    root_index = _json_bytes(
        {
            "manifests": [root_descriptor],
            "mediaType": _OCI_INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    if not container_index_digest:
        container_index_digest = _digest(root_index)

    legacy_tag = (
        "docx-rag:wrong"
        if "legacy_tag_mismatch" in variants
        else _TAG
    )
    legacy_tags = [legacy_tag]
    if "extra_legacy_tag" in variants:
        legacy_tags.append("docx-rag:extra")
    legacy_manifest = _json_bytes(
        [
            {
                "Config": _blob_path(legacy_config_digest),
                "Layers": [_blob_path(legacy_layer_digest)],
                "RepoTags": legacy_tags,
            }
        ]
    )
    members = {
        "index.json": root_index,
        "manifest.json": legacy_manifest,
        "oci-layout": _json_bytes({"imageLayoutVersion": "1.0.0"}),
    }
    members.update(
        {
            _blob_path(digest): content
            for digest, content in blobs.items()
        }
    )
    if "tamper_config_blob" in variants:
        config_path = _blob_path(target.config_digest)
        config_blob = members[config_path]
        members[config_path] = bytes((config_blob[0] ^ 1,)) + config_blob[1:]

    archive_path = tmp_path / "image.tar"
    _write_tar(archive_path, members)
    return _ArchiveFixture(
        path=archive_path,
        container_index_digest=container_index_digest,
        manifest_digest=target.manifest_digest,
        config_digest=target.config_digest,
        reserialized_manifest_digest=target.reserialized_manifest_digest,
    )


@pytest.mark.parametrize("nested_index", [False, True])
def test_manifest_digest_is_the_portable_identity(
    tmp_path: Path,
    nested_index: bool,
) -> None:
    archive = _build_archive(tmp_path, nested_index=nested_index)

    identity = inspect_docker_archive(
        archive.path,
        expected_tag=_TAG,
        expected_platform=_TARGET_PLATFORM,
    )

    assert identity.manifest_digest == archive.manifest_digest
    assert identity.config_digest == archive.config_digest
    assert identity.tag == _TAG
    assert identity.platform == _TARGET_PLATFORM
    assert identity.manifest_digest not in {
        archive.container_index_digest,
        archive.config_digest,
        archive.reserialized_manifest_digest,
    }


def test_inspection_selects_one_runnable_manifest_from_mixed_index(
    tmp_path: Path,
) -> None:
    archive = _build_archive(
        tmp_path,
        variants=("other_platform", "attestation"),
    )

    identity = inspect_docker_archive(
        archive.path,
        expected_tag=_TAG,
        expected_platform=_TARGET_PLATFORM,
    )

    assert identity.manifest_digest == archive.manifest_digest
    assert identity.config_digest == archive.config_digest


def test_inspection_rejects_wrong_tag(tmp_path: Path) -> None:
    archive = _build_archive(tmp_path)

    with pytest.raises(
        DockerArchiveIdentityError,
        match=r"manifest\.json 未唯一包含 tag",
    ):
        inspect_docker_archive(
            archive.path,
            expected_tag="docx-rag:wrong",
            expected_platform=_TARGET_PLATFORM,
        )


def test_inspection_rejects_wrong_platform(tmp_path: Path) -> None:
    archive = _build_archive(tmp_path)

    with pytest.raises(DockerArchiveIdentityError):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform="linux/arm64",
        )


def test_inspection_rejects_descriptor_and_config_platform_mismatch(
    tmp_path: Path,
) -> None:
    archive = _build_archive(
        tmp_path,
        variants=("config_platform_mismatch",),
    )

    with pytest.raises(DockerArchiveIdentityError):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )


def test_inspection_rejects_tampered_reachable_blob(tmp_path: Path) -> None:
    archive = _build_archive(
        tmp_path,
        variants=("tamper_config_blob",),
    )

    with pytest.raises(
        DockerArchiveIdentityError,
        match="blob 内容摘要不一致",
    ):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )


@pytest.mark.parametrize(
    "variant",
    ("descriptor_size_too_small", "descriptor_size_too_large"),
)
def test_inspection_rejects_descriptor_size_mismatch(
    tmp_path: Path,
    variant: str,
) -> None:
    archive = _build_archive(tmp_path, variants=(variant,))

    with pytest.raises(
        DockerArchiveIdentityError,
        match="descriptor size 与 blob 大小不一致",
    ):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )


def test_inspection_rejects_multiple_target_platform_manifests(
    tmp_path: Path,
) -> None:
    archive = _build_archive(
        tmp_path,
        variants=("duplicate_target",),
    )

    with pytest.raises(
        DockerArchiveIdentityError,
        match="目标平台必须恰有一个",
    ):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )


@pytest.mark.parametrize(
    "variant",
    ("legacy_config_mismatch", "legacy_layer_mismatch"),
)
def test_inspection_rejects_legacy_and_oci_content_disagreement(
    tmp_path: Path,
    variant: str,
) -> None:
    archive = _build_archive(tmp_path, variants=(variant,))

    with pytest.raises(DockerArchiveIdentityError, match=r"manifest\.json"):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )


def test_inspection_rejects_legacy_and_oci_tag_disagreement(
    tmp_path: Path,
) -> None:
    archive = _build_archive(
        tmp_path,
        variants=("legacy_tag_mismatch",),
    )

    with pytest.raises(DockerArchiveIdentityError):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )


def test_inspection_rejects_oci_root_tag_mismatch(tmp_path: Path) -> None:
    archive = _build_archive(
        tmp_path,
        variants=("oci_tag_mismatch",),
    )

    with pytest.raises(DockerArchiveIdentityError):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )


def test_inspection_rejects_extra_legacy_repo_tag(tmp_path: Path) -> None:
    archive = _build_archive(
        tmp_path,
        variants=("extra_legacy_tag",),
    )

    with pytest.raises(DockerArchiveIdentityError):
        inspect_docker_archive(
            archive.path,
            expected_tag=_TAG,
            expected_platform=_TARGET_PLATFORM,
        )
