"""从已构建 Product 镜像生成并验证单一离线部署包。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from rag_app.product.asset_manifest import (  # noqa: E402
    load_product_asset_manifest,
)
from scripts.offline_bundle import safe_extract_bundle  # noqa: E402
from scripts.release_context import (  # noqa: E402
    require_clean_committed_head,
)
from scripts.secret_scan import Finding, scan_bytes  # noqa: E402

__all__ = [
    "BundleVerificationReport",
    "CleanRoomAcceptanceReport",
    "ProductBundleError",
    "build_product_offline_bundle",
    "verify_product_offline_bundle",
]

_SCHEMA_VERSION = "product-offline-bundle-v1"
_TOP_LEVEL = "product-offline-bundle-v1"
_DEFAULT_APP_IMAGE = "docx-rag:v1-candidate"
_DEFAULT_QDRANT_IMAGE = "qdrant/qdrant:v1.18.3"
_PRODUCT_MANIFEST_PATH = "/app/product-assets.json"
_HASH_BLOCK_BYTES = 1024 * 1024
_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
_EXPECTED_BUNDLE_FILES = {
    ".env",
    "MANIFEST.sha256",
    "bundle-receipt.json",
    "compose.clean-room.yaml",
    "compose.yaml",
    "images/product-images.tar",
    "product-assets.json",
}
_RECEIPT_KEYS = {
    "app_image",
    "bundle_files",
    "qdrant_image",
    "schema_version",
    "source_revision",
}
_IMAGE_KEYS = {
    "architecture",
    "image_id",
    "os",
    "reference",
    "repo_digests",
    "revision",
    "user",
}
_BUNDLE_FILE_KEYS = {
    "compose_clean_room_sha256",
    "compose_sha256",
    "environment_sha256",
    "image_archive_bytes",
    "image_archive_sha256",
    "product_asset_manifest_sha256",
}
_CLEAN_ROOM_OVERRIDE = """services: {}
networks:
  rag-egress:
    internal: true
"""


class ProductBundleError(RuntimeError):
    """表示 Product 离线包不满足构建或验证合同。"""


@dataclass(frozen=True, slots=True)
class ImageIdentity:
    """离线包中单个 OCI 镜像的可验证身份。"""

    reference: str
    image_id: str
    repo_digests: tuple[str, ...]
    user: str
    revision: str | None
    os: str
    architecture: str

    def payload(self) -> dict[str, object]:
        """返回可写入规范 JSON 的镜像身份。

        Args:
            无参数；读取当前镜像身份。

        Returns:
            仅含规范公开身份字段的字典。

        """
        return {
            "architecture": self.architecture,
            "image_id": self.image_id,
            "os": self.os,
            "reference": self.reference,
            "repo_digests": list(self.repo_digests),
            "revision": self.revision,
            "user": self.user,
        }


@dataclass(frozen=True, slots=True)
class BundleVerificationReport:
    """离线包静态验证后的非敏感结果。"""

    archive_sha256: str
    source_revision: str
    app_image_id: str
    qdrant_image_id: str
    product_asset_manifest_sha256: str
    extracted_path: str


@dataclass(frozen=True, slots=True)
class CleanRoomAcceptanceReport:
    """Product 离线包 clean-room 验收的非敏感收据。"""

    schema_version: str
    status: str
    archive_sha256: str
    source_revision: str
    app_image_id: str
    qdrant_image_id: str
    app_user: str
    product_asset_manifest_sha256: str
    network_egress: str
    source_bind_mounts: int
    pull_policy: str
    active_revision_id: str
    trace_id: str
    steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CleanRoomSmokeIdentity:
    """clean-room 离线检索返回的短期运行身份。"""

    active_revision_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class _BundleInputs:
    """一次 bundle staging 已冻结的输入身份。"""

    root: Path
    revision: str
    app_image: str
    qdrant_image: str
    app_identity: ImageIdentity
    qdrant_identity: ImageIdentity
    selfcheck: Mapping[str, object]


def build_product_offline_bundle(
    *,
    repository_root: Path,
    output: Path,
    app_image: str | None = None,
    qdrant_image: str | None = None,
) -> tuple[Path, Path]:
    """从当前 HEAD 对应的同一预构建镜像生成离线包。

    该入口只允许 image inspect、无网络 selfcheck、image save 与 Compose
    config，不执行 Docker build、npm 或任何前端二次构建。

    Args:
        repository_root: 当前 Product 仓库根目录。
        output: 待创建的 ``tar.gz`` 归档路径。
        app_image: 可选的已构建 Product 镜像引用。
        qdrant_image: 可选的已存在 Qdrant 镜像引用。

    Returns:
        归档路径和绑定该归档名的外层 SHA256 sidecar。

    Raises:
        ProductBundleError: Git、镜像、资产或输出不满足离线包合同。

    """
    root = repository_root.resolve(strict=True)
    try:
        revision = require_clean_committed_head(root)
    except (OSError, RuntimeError) as error:
        raise ProductBundleError(
            "Product 离线包要求干净且已提交的 HEAD。"
        ) from error
    if _FULL_REVISION.fullmatch(revision) is None:
        raise ProductBundleError("Product 离线包要求完整 40 位 Git SHA。")
    selected_app = app_image or _read_image_default(
        root / ".env.example",
        "RAG_APP_IMAGE",
        _DEFAULT_APP_IMAGE,
    )
    selected_qdrant = qdrant_image or _read_image_default(
        root / ".env.example",
        "RAG_QDRANT_IMAGE",
        _DEFAULT_QDRANT_IMAGE,
    )
    _validate_image_reference(selected_app)
    _validate_image_reference(selected_qdrant)
    docker = _required_executable("docker")
    app_identity = _inspect_image(
        docker,
        selected_app,
        expected_revision=revision,
        expected_user="rag:rag",
        cwd=root,
    )
    qdrant_identity = _inspect_image(
        docker,
        selected_qdrant,
        expected_revision=None,
        expected_user=None,
        cwd=root,
    )
    selfcheck = _run_product_selfcheck(
        docker,
        selected_app,
        revision,
        cwd=root,
    )
    output_path, sidecar_path = _prepare_output_paths(output)
    with tempfile.TemporaryDirectory(
        dir=output_path.parent,
        prefix=".product-offline-bundle-",
    ) as temporary_name:
        temporary = Path(temporary_name)
        inputs = _BundleInputs(
            root=root,
            revision=revision,
            app_image=selected_app,
            qdrant_image=selected_qdrant,
            app_identity=app_identity,
            qdrant_identity=qdrant_identity,
            selfcheck=selfcheck,
        )
        stage = _stage_product_bundle(
            temporary=temporary,
            docker=docker,
            inputs=inputs,
        )
        temporary_archive = temporary / output_path.name
        temporary_sidecar = temporary / sidecar_path.name
        _write_deterministic_archive(stage, temporary_archive)
        _write_sidecar(temporary_archive, temporary_sidecar)
        verification = temporary / "verification"
        verify_product_offline_bundle(
            archive=temporary_archive,
            sidecar=temporary_sidecar,
            destination=verification,
        )
        temporary_archive.replace(output_path)
        temporary_sidecar.replace(sidecar_path)
    return output_path, sidecar_path


def _prepare_output_paths(output: Path) -> tuple[Path, Path]:
    output_path = output.absolute()
    sidecar_path = output_path.with_name(f"{output_path.name}.sha256")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or output_path.is_symlink():
        raise ProductBundleError(f"Product 离线包输出已存在：{output_path}")
    if sidecar_path.exists() or sidecar_path.is_symlink():
        raise ProductBundleError(
            f"Product 离线包 sidecar 已存在：{sidecar_path}"
        )
    return output_path, sidecar_path


def _stage_product_bundle(
    *,
    temporary: Path,
    docker: str,
    inputs: _BundleInputs,
) -> Path:
    stage = temporary / "stage" / _TOP_LEVEL
    (stage / "images").mkdir(parents=True)
    product_manifest_path = stage / "product-assets.json"
    product_manifest_path.write_bytes(
        _capture(
            (
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "cat",
                inputs.app_image,
                _PRODUCT_MANIFEST_PATH,
            ),
            cwd=inputs.root,
        )
    )
    product_manifest = load_product_asset_manifest(product_manifest_path)
    product_manifest_sha256 = _sha256_file(product_manifest_path)
    if product_manifest.source_revision != inputs.revision:
        raise ProductBundleError("镜像内 Product 资产清单与当前 HEAD 不一致。")
    if inputs.selfcheck.get("manifest_sha256") != product_manifest_sha256:
        raise ProductBundleError(
            "镜像 selfcheck 与提取的 Product 资产清单不一致。"
        )
    source_compose = inputs.root / "compose.yaml"
    if source_compose.is_symlink() or not source_compose.is_file():
        raise ProductBundleError("Product 根 Compose 必须是普通文件。")
    shutil.copyfile(source_compose, stage / "compose.yaml")
    clean_room = stage / "compose.clean-room.yaml"
    clean_room.write_text(_CLEAN_ROOM_OVERRIDE, encoding="utf-8")
    environment_path = stage / ".env"
    environment_path.write_text(
        _render_environment(
            revision=inputs.revision,
            app_image=inputs.app_image,
            qdrant_image=inputs.qdrant_image,
        ),
        encoding="utf-8",
    )
    _validate_compose_contract(stage / "compose.yaml", clean_room)
    _run_compose_config(docker, stage, environment_path)
    image_archive = stage / "images/product-images.tar"
    _run_checked(
        (
            docker,
            "image",
            "save",
            "--output",
            str(image_archive),
            inputs.app_image,
            inputs.qdrant_image,
        ),
        cwd=inputs.root,
    )
    if image_archive.is_symlink() or not image_archive.is_file():
        raise ProductBundleError("Docker 未生成 Product 镜像归档。")
    if image_archive.stat().st_size <= 0:
        raise ProductBundleError("Product 镜像归档不能为空。")
    _write_bundle_receipt(
        stage=stage,
        app_identity=inputs.app_identity,
        qdrant_identity=inputs.qdrant_identity,
        revision=inputs.revision,
        product_manifest_sha256=product_manifest_sha256,
    )
    _scan_secret_shapes(
        docker=docker,
        stage=stage,
        image_references=(inputs.app_image, inputs.qdrant_image),
        cwd=inputs.root,
    )
    _write_file_manifest(stage)
    _verify_stage(stage)
    return stage


def _write_bundle_receipt(
    *,
    stage: Path,
    app_identity: ImageIdentity,
    qdrant_identity: ImageIdentity,
    revision: str,
    product_manifest_sha256: str,
) -> None:
    image_archive = stage / "images/product-images.tar"
    receipt = {
        "app_image": app_identity.payload(),
        "bundle_files": {
            "compose_clean_room_sha256": _sha256_file(
                stage / "compose.clean-room.yaml"
            ),
            "compose_sha256": _sha256_file(stage / "compose.yaml"),
            "environment_sha256": _sha256_file(stage / ".env"),
            "image_archive_bytes": image_archive.stat().st_size,
            "image_archive_sha256": _sha256_file(image_archive),
            "product_asset_manifest_sha256": product_manifest_sha256,
        },
        "qdrant_image": qdrant_identity.payload(),
        "schema_version": _SCHEMA_VERSION,
        "source_revision": revision,
    }
    (stage / "bundle-receipt.json").write_bytes(_canonical_json_bytes(receipt))


def verify_product_offline_bundle(
    *,
    archive: Path,
    sidecar: Path,
    destination: Path,
) -> BundleVerificationReport:
    """安全解包并验证 Product 离线包的全部静态身份。

    Args:
        archive: 待验证的 Product ``tar.gz``。
        sidecar: 绑定归档名和内容的 SHA256 sidecar。
        destination: 尚不包含固定顶层目录的解包父目录。

    Returns:
        可继续用于 Docker clean-room 验收的验证结果。

    Raises:
        ProductBundleError: 收据、Compose 或镜像归档身份无效。
        ValueError: 外层摘要、tar 成员或内部文件清单无效。

    """
    archive_sha256 = _sha256_file(archive)
    extracted = safe_extract_bundle(
        archive,
        sidecar,
        destination,
        expected_top_level=_TOP_LEVEL,
    )
    _verify_stage(extracted)
    receipt = _load_receipt(extracted / "bundle-receipt.json")
    files = cast(dict[str, object], receipt["bundle_files"])
    _require_file_identity(
        extracted / "product-assets.json",
        cast(str, files["product_asset_manifest_sha256"]),
        label="Product 资产清单",
    )
    product_manifest = load_product_asset_manifest(
        extracted / "product-assets.json"
    )
    source_revision = cast(str, receipt["source_revision"])
    if product_manifest.source_revision != source_revision:
        raise ProductBundleError("Product 资产清单与 bundle revision 不一致。")
    _require_file_identity(
        extracted / "compose.yaml",
        cast(str, files["compose_sha256"]),
        label="Compose",
    )
    _require_file_identity(
        extracted / "compose.clean-room.yaml",
        cast(str, files["compose_clean_room_sha256"]),
        label="clean-room Compose",
    )
    _require_file_identity(
        extracted / ".env",
        cast(str, files["environment_sha256"]),
        label="环境文件",
    )
    image_archive = extracted / "images/product-images.tar"
    _require_file_identity(
        image_archive,
        cast(str, files["image_archive_sha256"]),
        label="镜像归档",
    )
    expected_bytes = files["image_archive_bytes"]
    if type(expected_bytes) is not int or image_archive.stat().st_size != (
        expected_bytes
    ):
        raise ProductBundleError("镜像归档字节数与 bundle 收据不一致。")
    _validate_environment(extracted / ".env", receipt)
    _validate_compose_contract(
        extracted / "compose.yaml",
        extracted / "compose.clean-room.yaml",
    )
    app_identity = cast(dict[str, object], receipt["app_image"])
    qdrant_identity = cast(dict[str, object], receipt["qdrant_image"])
    return BundleVerificationReport(
        archive_sha256=archive_sha256,
        source_revision=source_revision,
        app_image_id=cast(str, app_identity["image_id"]),
        qdrant_image_id=cast(str, qdrant_identity["image_id"]),
        product_asset_manifest_sha256=cast(
            str,
            files["product_asset_manifest_sha256"],
        ),
        extracted_path=str(extracted),
    )


def run_clean_room_acceptance(
    report: BundleVerificationReport,
) -> CleanRoomAcceptanceReport:
    """加载已验证镜像并执行无源码挂载的临时 Compose 验收。

    Args:
        report: ``verify_product_offline_bundle`` 返回的静态验证结果。

    Returns:
        只含公开运行身份与已完成步骤的规范收据。

    Raises:
        ProductBundleError: 镜像身份、自检、负向篡改或 HTTP smoke 失败。

    """
    extracted = Path(report.extracted_path).resolve(strict=True)
    receipt = _load_receipt(extracted / "bundle-receipt.json")
    docker = _required_executable("docker")
    _run_checked(
        (
            docker,
            "image",
            "load",
            "--input",
            str(extracted / "images/product-images.tar"),
        ),
        cwd=extracted,
    )
    source_revision = cast(str, receipt["source_revision"])
    app = _identity_from_receipt(receipt, "app_image")
    qdrant = _identity_from_receipt(receipt, "qdrant_image")
    loaded_app = _inspect_image(
        docker,
        app.reference,
        expected_revision=source_revision,
        expected_user="rag:rag",
        cwd=extracted,
    )
    loaded_qdrant = _inspect_image(
        docker,
        qdrant.reference,
        expected_revision=None,
        expected_user=None,
        cwd=extracted,
    )
    if loaded_app.image_id != app.image_id:
        raise ProductBundleError("加载后的 Product 镜像 ID 与收据不一致。")
    if loaded_qdrant.image_id != qdrant.image_id:
        raise ProductBundleError("加载后的 Qdrant 镜像 ID 与收据不一致。")
    selfcheck = _run_product_selfcheck(
        docker,
        app.reference,
        source_revision,
        cwd=extracted,
    )
    if selfcheck.get("manifest_sha256") != (
        report.product_asset_manifest_sha256
    ):
        raise ProductBundleError("clean-room 镜像资产自检身份不一致。")
    _negative_tamper_selfcheck(
        docker=docker,
        image=app.reference,
        revision=source_revision,
        cwd=extracted,
    )
    _scan_secret_shapes(
        docker=docker,
        stage=extracted,
        image_references=(app.reference, qdrant.reference),
        cwd=extracted,
    )
    smoke_identity = _run_compose_smoke(
        docker=docker,
        extracted=extracted,
        app_image=app.reference,
        qdrant_image=qdrant.reference,
    )
    return CleanRoomAcceptanceReport(
        schema_version="product-offline-clean-room-v1",
        status="PASS",
        archive_sha256=report.archive_sha256,
        source_revision=source_revision,
        app_image_id=loaded_app.image_id,
        qdrant_image_id=loaded_qdrant.image_id,
        app_user=loaded_app.user,
        product_asset_manifest_sha256=(report.product_asset_manifest_sha256),
        network_egress="compose-internal-only",
        source_bind_mounts=0,
        pull_policy="never",
        active_revision_id=smoke_identity.active_revision_id,
        trace_id=smoke_identity.trace_id,
        steps=(
            "archive_static_verification",
            "image_identity",
            "asset_selfcheck",
            "asset_tamper_rejection",
            "secret_shape_scan",
            "compose_live",
            "product_login",
            "offline_docx_ingestion_query",
            "compose_cleanup",
        ),
    )


def _inspect_image(
    docker: str,
    reference: str,
    *,
    expected_revision: str | None,
    expected_user: str | None,
    cwd: Path,
) -> ImageIdentity:
    raw = _capture((docker, "image", "inspect", reference), cwd=cwd)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductBundleError("Docker image inspect 输出无效。") from error
    if not isinstance(payload, list) or len(payload) != 1:
        raise ProductBundleError("Docker image inspect 必须返回单个镜像。")
    item = payload[0]
    if not isinstance(item, dict):
        raise ProductBundleError("Docker image inspect 镜像结构无效。")
    config = item.get("Config")
    if not isinstance(config, dict):
        raise ProductBundleError("Docker 镜像缺少 Config。")
    image_id = item.get("Id")
    user = config.get("User") or ""
    labels = config.get("Labels") or {}
    repo_digests = item.get("RepoDigests") or []
    if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
        raise ProductBundleError("Docker 镜像缺少可信 content ID。")
    if not isinstance(user, str):
        raise ProductBundleError("Docker 镜像 User 字段无效。")
    if expected_user is not None and user != expected_user:
        raise ProductBundleError("Product 镜像必须使用 rag:rag 非 root 用户。")
    if not isinstance(labels, dict):
        raise ProductBundleError("Docker 镜像 Labels 字段无效。")
    revision = labels.get("org.opencontainers.image.revision")
    if revision is not None and not isinstance(revision, str):
        raise ProductBundleError("Docker 镜像 revision label 无效。")
    if expected_revision is not None and revision != expected_revision:
        raise ProductBundleError("Product 镜像 revision 与当前 HEAD 不一致。")
    if not isinstance(repo_digests, list) or not all(
        isinstance(value, str) for value in repo_digests
    ):
        raise ProductBundleError("Docker 镜像 RepoDigests 字段无效。")
    operating_system = item.get("Os")
    architecture = item.get("Architecture")
    if not isinstance(operating_system, str) or not isinstance(
        architecture,
        str,
    ):
        raise ProductBundleError("Docker 镜像平台身份无效。")
    return ImageIdentity(
        reference=reference,
        image_id=image_id,
        repo_digests=tuple(sorted(cast(list[str], repo_digests))),
        user=user,
        revision=revision,
        os=operating_system,
        architecture=architecture,
    )


def _run_product_selfcheck(
    docker: str,
    image: str,
    revision: str,
    *,
    cwd: Path,
) -> dict[str, object]:
    raw = _capture(
        (
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "product-asset-selfcheck",
            "--expected-revision",
            revision,
        ),
        cwd=cwd,
    )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductBundleError("Product 镜像 selfcheck 输出无效。") from error
    if not isinstance(payload, dict):
        raise ProductBundleError("Product 镜像 selfcheck 必须返回 JSON 对象。")
    if payload.get("source_revision") != revision:
        raise ProductBundleError("Product 镜像 selfcheck revision 不一致。")
    manifest_sha256 = payload.get("manifest_sha256")
    if (
        not isinstance(manifest_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
    ):
        raise ProductBundleError("Product 镜像 selfcheck 缺少 manifest SHA。")
    return cast(dict[str, object], payload)


def _load_receipt(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ProductBundleError("Product bundle 缺少普通收据文件。")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductBundleError("Product bundle 收据 JSON 无效。") from error
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS:
        raise ProductBundleError("Product bundle 收据顶层字段无效。")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ProductBundleError("Product bundle 收据 schema 无效。")
    revision = payload.get("source_revision")
    if (
        not isinstance(revision, str)
        or _FULL_REVISION.fullmatch(revision) is None
    ):
        raise ProductBundleError("Product bundle 收据 revision 无效。")
    _validate_receipt_image(
        payload.get("app_image"),
        expected_revision=revision,
        expected_user="rag:rag",
    )
    _validate_receipt_image(
        payload.get("qdrant_image"),
        expected_revision=None,
        expected_user=None,
    )
    files = payload.get("bundle_files")
    if not isinstance(files, dict) or set(files) != _BUNDLE_FILE_KEYS:
        raise ProductBundleError("Product bundle 文件收据字段无效。")
    for key, value in files.items():
        if key == "image_archive_bytes":
            if type(value) is not int or value <= 0:
                raise ProductBundleError("Product bundle 镜像字节数无效。")
        elif (
            not isinstance(value, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                value,
            )
            is None
        ):
            raise ProductBundleError(f"Product bundle 文件摘要无效：{key}")
    if _canonical_json_bytes(payload) != raw:
        raise ProductBundleError("Product bundle 收据不是规范 JSON。")
    return cast(dict[str, object], payload)


def _validate_receipt_image(
    payload: object,
    *,
    expected_revision: str | None,
    expected_user: str | None,
) -> None:
    if not isinstance(payload, dict) or set(payload) != _IMAGE_KEYS:
        raise ProductBundleError("Product bundle 镜像身份字段无效。")
    reference = payload.get("reference")
    image_id = payload.get("image_id")
    revision = payload.get("revision")
    repo_digests = payload.get("repo_digests")
    if not isinstance(reference, str):
        raise ProductBundleError("Product bundle 镜像引用无效。")
    _validate_image_reference(reference)
    if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
        raise ProductBundleError("Product bundle 镜像 ID 无效。")
    if expected_revision is not None and revision != expected_revision:
        raise ProductBundleError("Product bundle App revision 无效。")
    if revision is not None and not isinstance(revision, str):
        raise ProductBundleError("Product bundle 镜像 revision 类型无效。")
    if not isinstance(repo_digests, list) or repo_digests != sorted(
        repo_digests
    ):
        raise ProductBundleError("Product bundle RepoDigests 无效。")
    if not all(isinstance(value, str) for value in repo_digests):
        raise ProductBundleError("Product bundle RepoDigests 类型无效。")
    if len(repo_digests) != len(set(repo_digests)):
        raise ProductBundleError("Product bundle RepoDigests 含重复项。")
    for key in ("architecture", "os", "user"):
        if not isinstance(payload.get(key), str):
            raise ProductBundleError(f"Product bundle 镜像 {key} 无效。")
    if payload.get("os") != "linux":
        raise ProductBundleError("Product bundle 镜像必须为 Linux。")
    if expected_user is not None and payload.get("user") != expected_user:
        raise ProductBundleError("Product bundle App 必须为 rag:rag 用户。")


def _identity_from_receipt(
    receipt: Mapping[str, object],
    key: str,
) -> ImageIdentity:
    payload = cast(dict[str, object], receipt[key])
    return ImageIdentity(
        reference=cast(str, payload["reference"]),
        image_id=cast(str, payload["image_id"]),
        repo_digests=tuple(cast(list[str], payload["repo_digests"])),
        user=cast(str, payload["user"]),
        revision=cast(str | None, payload["revision"]),
        os=cast(str, payload["os"]),
        architecture=cast(str, payload["architecture"]),
    )


def _validate_compose_contract(compose_path: Path, override_path: Path) -> None:
    for path in (compose_path, override_path):
        if path.is_symlink() or not path.is_file():
            raise ProductBundleError("Product bundle Compose 必须是普通文件。")
    compose = compose_path.read_text(encoding="utf-8")
    override = override_path.read_text(encoding="utf-8")
    if "\t" in compose or "\r" in compose:
        raise ProductBundleError("Product bundle Compose 格式无效。")
    if override != _CLEAN_ROOM_OVERRIDE:
        raise ProductBundleError("clean-room Compose 网络隔离合同无效。")
    service_lines = _compose_section(compose, "services")
    volume_lines = _compose_section(compose, "volumes")
    services = _mapping_keys(service_lines, indent=2)
    volumes = _mapping_keys(volume_lines, indent=2)
    if services != {"app", "qdrant"}:
        raise ProductBundleError("Product bundle Compose 服务集合无效。")
    if not volumes:
        raise ProductBundleError("Product bundle Compose 缺少命名卷。")
    for service_name in sorted(services):
        service = _mapping_block(service_lines, service_name, indent=2)
        _validate_compose_service(service_name, service, volumes)


def _validate_compose_service(
    service_name: str,
    service_lines: Sequence[str],
    named_volumes: set[str],
) -> None:
    keys = _mapping_keys(service_lines, indent=4)
    if "image" not in keys:
        raise ProductBundleError(
            f"Product bundle Compose 服务缺少 image：{service_name}"
        )
    if {"build", "develop"} & keys:
        raise ProductBundleError("Product bundle Compose 禁止源码构建。")
    if "volumes" not in keys:
        raise ProductBundleError(
            f"Product bundle Compose 服务缺少命名卷：{service_name}"
        )
    volume_block = _mapping_block(service_lines, "volumes", indent=4)
    sources: set[str] = set()
    for line in volume_block:
        if not line.startswith("      - "):
            if line.strip():
                raise ProductBundleError(
                    "Product bundle Compose volume 结构无效。"
                )
            continue
        mount = line.removeprefix("      - ").strip()
        source = mount.split(":", maxsplit=1)[0]
        if source not in named_volumes:
            raise ProductBundleError(
                f"Product bundle Compose 含非命名卷：{service_name}"
            )
        sources.add(source)
    expected_sources = (
        {"rag_data", "rag_secrets"}
        if service_name == "app"
        else {"qdrant_data", "rag_secrets"}
    )
    if sources != expected_sources:
        raise ProductBundleError(
            f"Product bundle Compose 命名卷集合无效：{service_name}"
        )


def _compose_section(content: str, name: str) -> tuple[str, ...]:
    lines = content.splitlines()
    heading = f"{name}:"
    if lines.count(heading) != 1:
        raise ProductBundleError(f"Product bundle Compose 缺少 {name}。")
    start = lines.index(heading) + 1
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line and not line[0].isspace():
            end = index
            break
    return tuple(lines[start:end])


def _mapping_keys(lines: Sequence[str], *, indent: int) -> set[str]:
    prefix = " " * indent
    keys: list[str] = []
    for line in lines:
        if not line.startswith(prefix) or line.startswith(f"{prefix} "):
            continue
        match = re.fullmatch(r"[ ]+([A-Za-z0-9_-]+):(?:[ ]+.*)?", line)
        if match is not None:
            keys.append(match.group(1))
    if len(keys) != len(set(keys)):
        raise ProductBundleError("Product bundle Compose 含重复字段。")
    return set(keys)


def _mapping_block(
    lines: Sequence[str],
    name: str,
    *,
    indent: int,
) -> tuple[str, ...]:
    heading = f"{' ' * indent}{name}:"
    candidates = [
        index
        for index, line in enumerate(lines)
        if line == heading or line.startswith(f"{heading} ")
    ]
    if len(candidates) != 1:
        raise ProductBundleError(f"Product bundle Compose 字段数量无效：{name}")
    start = candidates[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line and len(line) - len(line.lstrip(" ")) <= indent:
            end = index
            break
    return tuple(lines[start:end])


def _validate_environment(
    path: Path,
    receipt: Mapping[str, object],
) -> None:
    values = _read_environment(path)
    expected_keys = {
        "RAG_APP_IMAGE",
        "RAG_PORT",
        "RAG_QDRANT_IMAGE",
        "RAG_RELEASE_REVISION",
        "RAG_TRUSTED_ORIGINS",
        "RAG_TRUSTED_PROXIES",
    }
    if set(values) != expected_keys:
        raise ProductBundleError("Product bundle 环境文件字段无效。")
    app = cast(dict[str, object], receipt["app_image"])
    qdrant = cast(dict[str, object], receipt["qdrant_image"])
    if values["RAG_APP_IMAGE"] != app["reference"]:
        raise ProductBundleError("Product bundle App image 环境身份不一致。")
    if values["RAG_QDRANT_IMAGE"] != qdrant["reference"]:
        raise ProductBundleError("Product bundle Qdrant 环境身份不一致。")
    if values["RAG_RELEASE_REVISION"] != receipt["source_revision"]:
        raise ProductBundleError("Product bundle 环境 revision 不一致。")
    if values["RAG_PORT"] != "8088":
        raise ProductBundleError("Product bundle 默认端口无效。")
    if values["RAG_TRUSTED_ORIGINS"] != (
        "http://127.0.0.1:8088,http://localhost:8088"
    ):
        raise ProductBundleError("Product bundle 默认可信源无效。")
    if values["RAG_TRUSTED_PROXIES"]:
        raise ProductBundleError("Product bundle 默认代理必须为空。")


def _render_environment(
    *,
    revision: str,
    app_image: str,
    qdrant_image: str,
) -> str:
    return (
        f"RAG_APP_IMAGE={app_image}\n"
        f"RAG_QDRANT_IMAGE={qdrant_image}\n"
        f"RAG_RELEASE_REVISION={revision}\n"
        "RAG_PORT=8088\n"
        "RAG_TRUSTED_ORIGINS=http://127.0.0.1:8088,"
        "http://localhost:8088\n"
        "RAG_TRUSTED_PROXIES=\n"
    )


def _read_environment(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ProductBundleError("Product bundle 环境文件无效。")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            raise ProductBundleError("Product bundle 环境文件格式无效。")
        key, value = line.split("=", maxsplit=1)
        if (
            not key
            or key in values
            or any(character.isspace() for character in key)
        ):
            raise ProductBundleError("Product bundle 环境文件键无效。")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ProductBundleError("Product bundle 环境文件值无效。")
        values[key] = value
    return values


def _read_image_default(path: Path, key: str, fallback: str) -> str:
    if not path.exists():
        return fallback
    values = _read_environment(path)
    return values.get(key, fallback)


def _run_compose_config(docker: str, stage: Path, env_path: Path) -> None:
    _run_checked(
        (
            docker,
            "compose",
            "--env-file",
            str(env_path),
            "--file",
            str(stage / "compose.yaml"),
            "--file",
            str(stage / "compose.clean-room.yaml"),
            "config",
            "--quiet",
        ),
        cwd=stage,
    )


def _run_compose_smoke(
    *,
    docker: str,
    extracted: Path,
    app_image: str,
    qdrant_image: str,
) -> _CleanRoomSmokeIdentity:
    port = _free_loopback_port()
    origin = f"http://127.0.0.1:{port}"
    project = f"rag-v305-{uuid.uuid4().hex[:12]}"
    environment = dict(os.environ)
    environment.update(
        {
            "RAG_APP_IMAGE": app_image,
            "RAG_PORT": str(port),
            "RAG_QDRANT_IMAGE": qdrant_image,
            "RAG_TRUSTED_ORIGINS": origin,
        }
    )
    compose = (
        docker,
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(extracted / ".env"),
        "--file",
        str(extracted / "compose.yaml"),
        "--file",
        str(extracted / "compose.clean-room.yaml"),
    )
    try:
        _run_checked(
            (
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "app",
                "init-secrets",
                "--directory",
                "/run/rag-secrets",
            ),
            cwd=extracted,
            environment=environment,
        )
        _run_checked(
            (*compose, "up", "--detach", "--no-build", "--pull", "never"),
            cwd=extracted,
            environment=environment,
        )
        return _run_container_http_smoke(
            compose=compose,
            request_origin=origin,
            cwd=extracted,
            environment=environment,
        )
    finally:
        _run_checked(
            (*compose, "down", "--volumes", "--remove-orphans"),
            cwd=extracted,
            environment=environment,
        )


def _run_container_http_smoke(
    *,
    compose: Sequence[str],
    request_origin: str,
    cwd: Path,
    environment: Mapping[str, str],
) -> _CleanRoomSmokeIdentity:
    """在隔离 App 容器内执行完整 HTTP 验收。

    Args:
        compose: 已限定随机项目与 clean-room 配置的 Compose 命令。
        request_origin: Product 写请求允许的回环 Origin。
        cwd: 已验证的离线包解包目录。
        environment: 含随机回环端口的 Compose 环境。

    Returns:
        活动索引 Revision 与查询 Trace 身份。

    Raises:
        ProductBundleError: 容器内探针失败或返回非规范身份。

    """
    raw = _capture(
        (
            *compose,
            "exec",
            "--no-tty",
            "app",
            "python",
            "-m",
            "rag_app.product.offline_probe",
            "--base-url",
            "http://127.0.0.1:8088",
            "--request-origin",
            request_origin,
            "--bootstrap-token-file",
            "/run/rag-secrets/admin-bootstrap-token",
        ),
        cwd=cwd,
        environment=environment,
    )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductBundleError(
            "clean-room 容器内探针未返回有效 JSON。"
        ) from error
    if not isinstance(payload, dict) or set(payload) != {
        "active_revision_id",
        "trace_id",
    }:
        raise ProductBundleError("clean-room 容器内探针身份字段无效。")
    active_revision_id = payload.get("active_revision_id")
    trace_id = payload.get("trace_id")
    if (
        not isinstance(active_revision_id, str)
        or not active_revision_id.startswith("irev_")
        or not isinstance(trace_id, str)
        or not trace_id.startswith("trace_")
    ):
        raise ProductBundleError("clean-room 容器内探针身份无效。")
    return _CleanRoomSmokeIdentity(
        active_revision_id=active_revision_id,
        trace_id=trace_id,
    )


def _negative_tamper_selfcheck(
    *,
    docker: str,
    image: str,
    revision: str,
    cwd: Path,
) -> None:
    container_id = (
        _capture(
            (
                docker,
                "create",
                "--network",
                "none",
                "--entrypoint",
                "rag-app",
                image,
                "product-asset-selfcheck",
                "--expected-revision",
                revision,
            ),
            cwd=cwd,
        )
        .decode("ascii")
        .strip()
    )
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise ProductBundleError("Docker 未返回可信的负向测试容器 ID。")
    try:
        with tempfile.TemporaryDirectory(
            prefix="product-asset-tamper-"
        ) as name:
            replacement = Path(name) / "index.html"
            replacement.write_text("tampered\n", encoding="utf-8")
            _run_checked(
                (
                    docker,
                    "cp",
                    str(replacement),
                    f"{container_id}:/app/frontend/index.html",
                ),
                cwd=cwd,
            )
        _run_checked((docker, "start", container_id), cwd=cwd)
        wait_output = _capture((docker, "wait", container_id), cwd=cwd)
        try:
            exit_code = int(wait_output.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError) as error:
            raise ProductBundleError("Docker 负向测试退出码无效。") from error
        if exit_code == 0:
            raise ProductBundleError(
                "篡改 Product 前端资产后 selfcheck 仍成功。"
            )
    finally:
        _run_checked((docker, "rm", "--force", container_id), cwd=cwd)


def _scan_secret_shapes(
    *,
    docker: str,
    stage: Path,
    image_references: Sequence[str],
    cwd: Path,
) -> None:
    findings = list(_scan_stage_secret_shapes(stage))
    for reference in image_references:
        metadata = _capture(
            (docker, "image", "inspect", reference),
            cwd=cwd,
        ) + _capture(
            (docker, "history", "--no-trunc", reference),
            cwd=cwd,
        )
        findings.extend(
            scan_bytes(metadata, location=f"docker-image:{reference}")
        )
    if findings:
        rules = ",".join(sorted({finding.rule for finding in findings}))
        raise ProductBundleError(f"Product 离线包发现 Secret 形状：{rules}")


def _scan_stage_secret_shapes(stage: Path) -> tuple[Finding, ...]:
    """扫描包内文本元数据，同时避开可能很大的二进制镜像归档。"""
    findings: list[Finding] = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix != ".tar":
            findings.extend(
                scan_bytes(
                    path.read_bytes(),
                    location=path.relative_to(stage).as_posix(),
                )
            )
    return tuple(findings)


def _write_file_manifest(root: Path) -> None:
    manifest_path = root / "MANIFEST.sha256"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ProductBundleError("Product bundle 内部 manifest 已存在。")
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProductBundleError("Product bundle staging 禁止 symlink。")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            lines.append(f"{_sha256_file(path)}  {relative}\n")
        elif not path.is_dir():
            raise ProductBundleError("Product bundle staging 含特殊文件。")
    manifest_path.write_text("".join(lines), encoding="ascii")


def _verify_stage(stage: Path) -> None:
    actual = {
        path.relative_to(stage).as_posix()
        for path in stage.rglob("*")
        if path.is_file()
    }
    if actual != _EXPECTED_BUNDLE_FILES:
        raise ProductBundleError("Product bundle 文件集合不完整。")
    if any(path.is_symlink() for path in stage.rglob("*")):
        raise ProductBundleError("Product bundle 禁止 symlink。")
    _load_receipt(stage / "bundle-receipt.json")
    load_product_asset_manifest(stage / "product-assets.json")
    _validate_compose_contract(
        stage / "compose.yaml",
        stage / "compose.clean-room.yaml",
    )
    findings = _scan_stage_secret_shapes(stage)
    if findings:
        rules = ",".join(sorted({finding.rule for finding in findings}))
        raise ProductBundleError(f"Product 离线包发现 Secret 形状：{rules}")


def _write_deterministic_archive(source: Path, output: Path) -> None:
    with (
        output.open("xb") as raw,
        gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=9,
            mtime=0,
        ) as compressed,
        tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.USTAR_FORMAT,
        ) as archive,
    ):
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source).as_posix()
            member = tarfile.TarInfo(f"{_TOP_LEVEL}/{relative}")
            member.size = path.stat().st_size
            member.mode = 0o644
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            with path.open("rb") as stream:
                archive.addfile(member, stream)


def _write_sidecar(archive: Path, sidecar: Path) -> None:
    sidecar.write_text(
        f"{_sha256_file(archive)}  {archive.name}\n",
        encoding="ascii",
    )


def _require_file_identity(path: Path, expected: str, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ProductBundleError(f"{label} 必须是普通文件。")
    if _sha256_file(path) != expected:
        raise ProductBundleError(f"{label} SHA256 与收据不一致。")


def _validate_image_reference(reference: str) -> None:
    if _IMAGE_REFERENCE.fullmatch(reference) is None:
        raise ProductBundleError("Docker 镜像引用格式无效。")


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ProductBundleError(f"缺少 Product 离线包命令：{name}")
    return executable


def _capture(
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    try:
        completed = subprocess.run(  # noqa: S603
            list(arguments),
            cwd=cwd,
            env=None if environment is None else dict(environment),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProductBundleError(
            f"Product 离线包命令失败：{Path(arguments[0]).name}"
        ) from error
    return completed.stdout


def _run_checked(
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> None:
    try:
        subprocess.run(  # noqa: S603
            list(arguments),
            cwd=cwd,
            env=None if environment is None else dict(environment),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProductBundleError(
            f"Product 离线包命令失败：{Path(arguments[0]).name}"
        ) from error


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser(
        "build",
        help="只消费当前预构建镜像生成离线包。",
    )
    build.add_argument(
        "--repository-root",
        type=Path,
        default=_REPOSITORY_ROOT,
    )
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--app-image")
    build.add_argument("--qdrant-image")
    verify = commands.add_parser(
        "verify",
        help="安全解包并验证 Product 离线包。",
    )
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--sidecar", type=Path)
    verify.add_argument("--destination", type=Path, required=True)
    verify.add_argument(
        "--clean-room",
        action="store_true",
        help="加载镜像并运行隔离 Compose、登录和离线 smoke。",
    )
    verify.add_argument(
        "--report-output",
        type=Path,
        help="把规范静态或 clean-room 收据写入新 JSON 文件。",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行 Product 离线包构建或验证命令。

    Args:
        argv: 可选命令行参数。

    Returns:
        成功时返回 0；合同或环境不满足时返回 1。

    """
    arguments = _arguments(argv)
    try:
        if arguments.command == "build":
            archive, sidecar = build_product_offline_bundle(
                repository_root=arguments.repository_root,
                output=arguments.output,
                app_image=arguments.app_image,
                qdrant_image=arguments.qdrant_image,
            )
            print(
                _canonical_json_bytes(
                    {"archive": str(archive), "sidecar": str(sidecar)}
                ).decode(),
                end="",
            )
            return 0
        sidecar = arguments.sidecar or arguments.archive.with_name(
            f"{arguments.archive.name}.sha256"
        )
        if arguments.clean_room:
            destination = arguments.destination
            if destination.exists() and any(destination.iterdir()):
                raise ProductBundleError("clean-room 目标目录必须为空。")
        report = verify_product_offline_bundle(
            archive=arguments.archive,
            sidecar=sidecar,
            destination=arguments.destination,
        )
        output_report: BundleVerificationReport | CleanRoomAcceptanceReport = (
            report
        )
        if arguments.clean_room:
            output_report = run_clean_room_acceptance(report)
        output_bytes = _canonical_json_bytes(asdict(output_report))
        if arguments.report_output is not None:
            report_output = arguments.report_output.absolute()
            report_output.parent.mkdir(parents=True, exist_ok=True)
            try:
                with report_output.open("xb") as stream:
                    stream.write(output_bytes)
            except FileExistsError as error:
                raise ProductBundleError(
                    f"Product 离线包收据已存在：{report_output}"
                ) from error
        print(output_bytes.decode(), end="")
        return 0
    except (OSError, ProductBundleError, ValueError) as error:
        print(f"PRODUCT_OFFLINE_BUNDLE_FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
