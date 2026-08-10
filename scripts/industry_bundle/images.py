"""构建并交叉校验 Industry app、OCR 与 Qdrant 镜像归档。"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.build_simple_bundle import (
    _QDRANT_SAVE_IMAGE,
    _require_linux_amd64_image,
    _require_qdrant_image,
    _save_compressed_image,
    build_app_archive,
)
from scripts.docker_archive_identity import inspect_docker_archive
from scripts.docker_archive_loaded_identity import (
    validate_loaded_image_identity,
)

__all__ = [
    "ExistingImageIdentity",
    "ImageArtifact",
    "IndustryImageError",
    "build_app_image_archive",
    "build_image_archives",
    "existing_image_identity",
]

_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_PLATFORM = "linux/amd64"
_HASH_BLOCK_BYTES = 1024 * 1024


class IndustryImageError(RuntimeError):
    """表示 Industry 镜像或离线归档身份不可信。"""


@dataclass(frozen=True, slots=True)
class ImageArtifact:
    """单个已验证镜像及其归档身份。"""

    name: str
    ref: str
    image_id: str
    platform: str
    revision: str | None
    archive_name: str
    archive_sha256: str
    manifest_digest: str
    config_digest: str

    def manifest_dict(self) -> dict[str, object]:
        """转换为 release manifest 的安全镜像身份。

        Args:
            无参数；字段取自当前镜像归档身份。

        Returns:
            不含本机路径的镜像字段。

        """
        value = asdict(self)
        value["id"] = value.pop("image_id")
        value["delivery"] = "archive"
        return value


@dataclass(frozen=True, slots=True)
class ExistingImageIdentity:
    """目标服务器上必须已存在的固定镜像身份。"""

    name: str
    ref: str
    image_id: str
    platform: str
    revision: str | None

    def manifest_dict(self) -> dict[str, object]:
        """转换为不含归档字段的 release manifest 身份。

        Args:
            无参数；字段取自当前服务器既有镜像身份。

        Returns:
            目标服务器必须逐项匹配的镜像字段。

        """
        return {
            "delivery": "server-existing",
            "id": self.image_id,
            "name": self.name,
            "platform": self.platform,
            "ref": self.ref,
            "revision": self.revision,
        }


def build_app_image_archive(
    *,
    repository_root: Path,
    revision: str,
    output_dir: Path,
) -> ImageArtifact:
    """构建并验证不含 corpus 的 app 镜像归档。

    Args:
        repository_root: clean Industry Git 根目录。
        revision: app wheel 与 OCI label 的完整 Git SHA。
        output_dir: release staging 根目录。

    Returns:
        已验证的 app 镜像及归档身份。

    Raises:
        IndustryImageError: revision 或归档身份无效。

    """
    if _FULL_REVISION.fullmatch(revision) is None:
        raise IndustryImageError("app revision 必须是完整 Git SHA。")
    app_archive = output_dir / "app-image.tar.gz"
    app_ref = build_app_archive(
        repository_root=repository_root,
        revision=revision,
        destination=app_archive,
    )
    _require_app_has_no_corpus(app_ref, repository_root)
    return _inspect_artifact(
        name="app",
        image_ref=app_ref,
        archive=app_archive,
        expected_revision=revision,
        repository_root=repository_root,
    )


def existing_image_identity(
    *,
    name: str,
    ref: str,
    image_id: str,
    revision: str | None,
) -> ExistingImageIdentity:
    """记录将在目标服务器严格复核的既有镜像身份。

    Args:
        name: 仅允许 `ocr` 或 `qdrant`。
        ref: 目标服务器现有固定镜像 tag。
        image_id: `docker image inspect` 返回的完整 SHA256 ID。
        revision: OCI revision；Qdrant 等无该 label 时为 `None`。

    Returns:
        不携带镜像归档的服务器镜像身份。

    Raises:
        IndustryImageError: 字段不能形成确定、可复核的镜像身份。

    """
    if name not in {"ocr", "qdrant"}:
        raise IndustryImageError("既有镜像仅允许 OCR 或 Qdrant。")
    if not ref or any(character.isspace() for character in ref):
        raise IndustryImageError("既有镜像 tag 无效。")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise IndustryImageError("既有镜像 ID 必须是完整 SHA256。")
    if revision is not None and _FULL_REVISION.fullmatch(revision) is None:
        raise IndustryImageError("既有镜像 revision 必须是完整 Git SHA。")
    return ExistingImageIdentity(
        name=name,
        ref=ref,
        image_id=image_id,
        platform=_PLATFORM,
        revision=revision,
    )


def build_image_archives(
    *,
    repository_root: Path,
    revision: str,
    output_dir: Path,
    ocr_image: str,
) -> tuple[ImageArtifact, ImageArtifact, ImageArtifact]:
    """构建 app 并保存、验证三个 linux/amd64 镜像归档。

    Args:
        repository_root: clean Industry Git 根目录。
        revision: app wheel 与 OCI label 的完整 Git SHA。
        output_dir: release staging 根目录。
        ocr_image: 操作员明确选择的本地固定 OCR tag。

    Returns:
        app、OCR、Qdrant 的已验证身份。

    Raises:
        IndustryImageError: tag、平台、revision 或归档身份无效。

    """
    app = build_app_image_archive(
        repository_root=repository_root,
        revision=revision,
        output_dir=output_dir,
    )
    _require_linux_amd64_image(ocr_image, repository_root)
    _require_qdrant_image(repository_root)
    _save_compressed_image(
        ocr_image,
        output_dir / "ocr-image.tar.gz",
        repository_root,
    )
    _save_compressed_image(
        _QDRANT_SAVE_IMAGE,
        output_dir / "qdrant-image.tar.gz",
        repository_root,
    )
    ocr_revision = _image_revision(ocr_image, repository_root)
    if ocr_revision is None or _FULL_REVISION.fullmatch(ocr_revision) is None:
        raise IndustryImageError("OCR image 缺少完整 revision label。")
    ocr = _inspect_artifact(
        name="ocr",
        image_ref=ocr_image,
        archive=output_dir / "ocr-image.tar.gz",
        expected_revision=ocr_revision,
        repository_root=repository_root,
    )
    qdrant = _inspect_artifact(
        name="qdrant",
        image_ref=_QDRANT_SAVE_IMAGE,
        archive=output_dir / "qdrant-image.tar.gz",
        expected_revision=None,
        repository_root=repository_root,
    )
    return app, ocr, qdrant


def _inspect_artifact(
    *,
    name: str,
    image_ref: str,
    archive: Path,
    expected_revision: str | None,
    repository_root: Path,
) -> ImageArtifact:
    with tempfile.NamedTemporaryFile(
        dir=archive.parent,
        prefix=f".{name}-image-",
    ) as raw:
        with gzip.open(archive, "rb") as compressed:
            while block := compressed.read(_HASH_BLOCK_BYTES):
                raw.write(block)
        raw.flush()
        identity = inspect_docker_archive(
            Path(raw.name),
            expected_tag=image_ref,
            expected_platform=_PLATFORM,
            expected_revision=expected_revision,
        )
    inspect_payload = json.loads(
        _run_output(
            ("docker", "image", "inspect", image_ref),
            cwd=repository_root,
        )
    )
    validate_loaded_image_identity(
        inspect_payload,
        expected_manifest_digest=identity.manifest_digest,
        expected_config_digest=identity.config_digest,
        expected_platform=_PLATFORM,
        expected_revision=expected_revision,
    )
    image_id = _run_output(
        ("docker", "image", "inspect", "--format", "{{.Id}}", image_ref),
        cwd=repository_root,
    ).strip()
    if not image_id.startswith("sha256:"):
        raise IndustryImageError("Docker inspect image ID 无效。")
    return ImageArtifact(
        name=name,
        ref=image_ref,
        image_id=image_id,
        platform=_PLATFORM,
        revision=expected_revision,
        archive_name=archive.name,
        archive_sha256=_sha256(archive),
        manifest_digest=identity.manifest_digest,
        config_digest=identity.config_digest,
    )


def _image_revision(image_ref: str, root: Path) -> str | None:
    value = _run_output(
        (
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            image_ref,
        ),
        cwd=root,
    ).strip()
    return value or None


def _require_app_has_no_corpus(image_ref: str, root: Path) -> None:
    scanner = (
        "import pathlib,sys; "
        "bad=[p for p in pathlib.Path('/app').rglob('*') "
        "if p.is_file() and (p.suffix.lower() in {'.doc','.docx'} "
        "or p.name.startswith('GM-'))]; "
        "sys.exit(1 if bad else 0)"
    )
    _run_output(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            image_ref,
            "-c",
            scanner,
        ),
        cwd=root,
    )


def _run_output(arguments: tuple[str, ...], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            arguments,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise IndustryImageError(f"命令失败：{arguments[0]}") from error
    return completed.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()
