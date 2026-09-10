"""生成并验证当前 Product 镜像内的不可变资产清单。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = [
    "DEFAULT_PRODUCT_ASSET_PATHS",
    "PRODUCT_ASSET_SCHEMA_VERSION",
    "ProductAssetCheckReport",
    "ProductAssetEntry",
    "ProductAssetManifest",
    "load_product_asset_manifest",
    "verify_product_asset_manifest",
    "write_product_asset_manifest",
]

PRODUCT_ASSET_SCHEMA_VERSION = "product-assets-v1"
DEFAULT_PRODUCT_ASSET_PATHS = (
    "compatibility-manifest.json",
    "frontend",
    "migrations",
    "openapi/openapi-v1.json",
)

_HASH_BLOCK_BYTES = 1024 * 1024
_REVISION = re.compile(r"^(?:[0-9a-f]{40}|development-unset)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODE = re.compile(r"^0[0-7]{3}$")
_UNSAFE_MODE_MASK = (
    stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | stat.S_IWGRP | stat.S_IWOTH
)
_MANIFEST_KEYS = {
    "file_count",
    "files",
    "schema_version",
    "source_revision",
    "total_bytes",
}
_ENTRY_KEYS = {"mode", "path", "sha256", "size_bytes"}


@dataclass(frozen=True, slots=True)
class ProductAssetEntry:
    """单个 Product 资产的规范化身份。"""

    path: str
    sha256: str
    size_bytes: int
    mode: str


@dataclass(frozen=True, slots=True)
class ProductAssetManifest:
    """Product 镜像资产清单的内存表示。"""

    schema_version: str
    source_revision: str
    files: tuple[ProductAssetEntry, ...]

    @property
    def file_count(self) -> int:
        """返回清单中的普通文件数量。

        Args:
            无参数；读取当前清单。

        Returns:
            清单中的普通文件数量。

        """
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        """返回清单中普通文件的总字节数。

        Args:
            无参数；读取当前清单。

        Returns:
            全部普通文件的字节数总和。

        """
        return sum(item.size_bytes for item in self.files)

    def canonical_bytes(self) -> bytes:
        """返回稳定排序且无平台差异的规范 JSON。

        Args:
            无参数；序列化当前清单。

        Returns:
            带末尾换行的规范 UTF-8 JSON。

        """
        payload = {
            "file_count": self.file_count,
            "files": [
                {
                    "mode": item.mode,
                    "path": item.path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in self.files
            ],
            "schema_version": self.schema_version,
            "source_revision": self.source_revision,
            "total_bytes": self.total_bytes,
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()


@dataclass(frozen=True, slots=True)
class ProductAssetCheckReport:
    """一次 Product 镜像资产自检的非敏感结果。"""

    schema_version: str
    source_revision: str
    verified_files: int
    verified_bytes: int
    manifest_sha256: str


def write_product_asset_manifest(
    *,
    root: Path,
    manifest_path: Path,
    source_revision: str,
    include_paths: Sequence[str] = DEFAULT_PRODUCT_ASSET_PATHS,
) -> ProductAssetManifest:
    """根据镜像内实际文件生成权威 Product 资产清单。

    Args:
        root: Product 镜像资产的可信根目录。
        manifest_path: 待写入的固定清单路径，必须位于 ``root`` 内。
        source_revision: 当前 wheel 与 OCI 镜像绑定的源码身份。
        include_paths: 相对 ``root`` 的受管普通文件或目录。

    Returns:
        已按路径排序并写入磁盘的资产清单。

    Raises:
        ValueError: revision、路径、文件类型或权限不符合合同。

    """
    resolved_root = _validated_root(root)
    output = _validated_output_path(resolved_root, manifest_path)
    _validate_revision(source_revision)
    entries = _collect_entries(resolved_root, include_paths)
    manifest = ProductAssetManifest(
        schema_version=PRODUCT_ASSET_SCHEMA_VERSION,
        source_revision=source_revision,
        files=entries,
    )
    output.write_bytes(manifest.canonical_bytes())
    return manifest


def load_product_asset_manifest(manifest_path: Path) -> ProductAssetManifest:
    """读取并严格验证规范 Product 资产清单。

    Args:
        manifest_path: 待读取的 JSON 清单。

    Returns:
        已验证 schema、排序和汇总字段的清单。

    Raises:
        ValueError: 清单不是普通文件、schema 无效或 JSON 不规范。

    """
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Product 资产清单必须是普通文件。")
    content = manifest_path.read_bytes()
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Product 资产清单不是有效 UTF-8 JSON。") from error
    manifest = _manifest_from_payload(payload)
    if manifest.canonical_bytes() != content:
        raise ValueError("Product 资产清单不是规范 JSON。")
    return manifest


def verify_product_asset_manifest(
    *,
    root: Path,
    manifest_path: Path,
    expected_source_revision: str | None = None,
    include_paths: Sequence[str] = DEFAULT_PRODUCT_ASSET_PATHS,
) -> ProductAssetCheckReport:
    """验证清单身份及全部受管资产的集合、摘要、大小和权限。

    Args:
        root: Product 镜像资产的可信根目录。
        manifest_path: 镜像内固定的 Product 资产清单。
        expected_source_revision: 可选的 wheel 或 OCI 期望 revision。
        include_paths: 相对 ``root`` 的受管普通文件或目录。

    Returns:
        不包含资产正文和宿主绝对路径的自检报告。

    Raises:
        ValueError: revision、文件集合或任一文件身份不一致。

    """
    resolved_root = _validated_root(root)
    resolved_manifest = _validated_existing_manifest(
        resolved_root,
        manifest_path,
    )
    manifest = load_product_asset_manifest(resolved_manifest)
    if (
        expected_source_revision is not None
        and manifest.source_revision != expected_source_revision
    ):
        raise ValueError("Product 资产清单 source revision 不一致。")
    actual = _collect_entries(resolved_root, include_paths)
    expected_by_path = {item.path: item for item in manifest.files}
    actual_by_path = {item.path: item for item in actual}
    missing = sorted(set(expected_by_path) - set(actual_by_path))
    if missing:
        raise ValueError(f"Product 资产缺失：{missing[0]}")
    extra = sorted(set(actual_by_path) - set(expected_by_path))
    if extra:
        raise ValueError(f"Product 资产清单外存在额外成员：{extra[0]}")
    for path, expected in expected_by_path.items():
        observed = actual_by_path[path]
        if observed.sha256 != expected.sha256:
            raise ValueError(f"Product 资产 SHA256 不一致：{path}")
        if observed.size_bytes != expected.size_bytes:
            raise ValueError(f"Product 资产大小不一致：{path}")
        if observed.mode != expected.mode:
            raise ValueError(f"Product 资产权限不一致：{path}")
    return ProductAssetCheckReport(
        schema_version=manifest.schema_version,
        source_revision=manifest.source_revision,
        verified_files=manifest.file_count,
        verified_bytes=manifest.total_bytes,
        manifest_sha256=_sha256_file(resolved_manifest),
    )


def _manifest_from_payload(payload: object) -> ProductAssetManifest:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
        raise ValueError("Product 资产清单顶层字段无效。")
    source_revision = payload.get("source_revision")
    schema_version = payload.get("schema_version")
    raw_files = payload.get("files")
    if schema_version != PRODUCT_ASSET_SCHEMA_VERSION:
        raise ValueError("Product 资产清单 schema version 无效。")
    if not isinstance(source_revision, str):
        raise ValueError("Product 资产清单 source revision 无效。")
    _validate_revision(source_revision)
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("Product 资产清单 files 必须为非空数组。")
    entries = tuple(_entry_from_payload(item) for item in raw_files)
    paths = [item.path for item in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("Product 资产清单路径未稳定排序或含重复项。")
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("Product 资产清单含大小写碰撞路径。")
    file_count = payload.get("file_count")
    total_bytes = payload.get("total_bytes")
    if type(file_count) is not int or file_count != len(entries):
        raise ValueError("Product 资产清单 file_count 无效。")
    if type(total_bytes) is not int or total_bytes != sum(
        item.size_bytes for item in entries
    ):
        raise ValueError("Product 资产清单 total_bytes 无效。")
    return ProductAssetManifest(
        schema_version=schema_version,
        source_revision=source_revision,
        files=entries,
    )


def _entry_from_payload(payload: object) -> ProductAssetEntry:
    if not isinstance(payload, dict) or set(payload) != _ENTRY_KEYS:
        raise ValueError("Product 资产清单文件字段无效。")
    path = payload.get("path")
    digest = payload.get("sha256")
    size_bytes = payload.get("size_bytes")
    mode = payload.get("mode")
    if not isinstance(path, str):
        raise ValueError("Product 资产清单路径无效。")
    _safe_relative_path(path)
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"Product 资产 SHA256 格式无效：{path}")
    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError(f"Product 资产大小无效：{path}")
    if not isinstance(mode, str) or _MODE.fullmatch(mode) is None:
        raise ValueError(f"Product 资产权限格式无效：{path}")
    _validate_mode(int(mode, 8), path)
    return ProductAssetEntry(
        path=path,
        sha256=digest,
        size_bytes=size_bytes,
        mode=mode,
    )


def _collect_entries(
    root: Path,
    include_paths: Sequence[str],
) -> tuple[ProductAssetEntry, ...]:
    if not include_paths:
        raise ValueError("Product 资产受管路径不能为空。")
    entries: dict[str, ProductAssetEntry] = {}
    folded_paths: set[str] = set()
    for value in include_paths:
        relative = _safe_relative_path(value)
        candidate = root.joinpath(*relative.parts)
        _reject_symlink_components(root, candidate)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as error:
            raise ValueError(f"Product 资产受管路径不存在：{value}") from error
        collected_before = len(entries)
        if stat.S_ISREG(metadata.st_mode):
            _add_entry(root, candidate, entries, folded_paths)
        elif stat.S_ISDIR(metadata.st_mode):
            for current, directories, filenames in os.walk(
                candidate,
                topdown=True,
                followlinks=False,
            ):
                current_path = Path(current)
                for directory in directories:
                    child = current_path / directory
                    if child.is_symlink():
                        raise ValueError(
                            f"Product 资产目录包含 symlink："
                            f"{child.relative_to(root).as_posix()}"
                        )
                for filename in filenames:
                    _add_entry(
                        root,
                        current_path / filename,
                        entries,
                        folded_paths,
                    )
        else:
            raise ValueError(f"Product 资产受管路径类型无效：{value}")
        if len(entries) == collected_before:
            raise ValueError(f"Product 资产受管目录为空：{value}")
    return tuple(entries[path] for path in sorted(entries))


def _add_entry(
    root: Path,
    path: Path,
    entries: dict[str, ProductAssetEntry],
    folded_paths: set[str],
) -> None:
    if path.is_symlink():
        raise ValueError(
            f"Product 资产不得为 symlink：{path.relative_to(root).as_posix()}"
        )
    metadata = path.lstat()
    relative = path.relative_to(root).as_posix()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Product 资产必须是普通文件：{relative}")
    if relative in entries or relative.casefold() in folded_paths:
        raise ValueError(f"Product 资产路径重复或大小写碰撞：{relative}")
    permissions = stat.S_IMODE(metadata.st_mode)
    _validate_mode(permissions, relative)
    entries[relative] = ProductAssetEntry(
        path=relative,
        sha256=_sha256_file(path),
        size_bytes=metadata.st_size,
        mode=f"0{permissions:03o}",
    )
    folded_paths.add(relative.casefold())


def _validated_root(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Product 资产根必须是真实目录。")
    return root.resolve(strict=True)


def _validated_output_path(root: Path, manifest_path: Path) -> Path:
    candidate = (
        manifest_path if manifest_path.is_absolute() else root / manifest_path
    )
    if candidate.is_symlink():
        raise ValueError("Product 资产清单输出不得为 symlink。")
    if candidate.exists() and not candidate.is_file():
        raise ValueError("Product 资产清单输出必须是普通文件路径。")
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("Product 资产清单输出目录无效。") from error
    if not resolved_parent.is_relative_to(root):
        raise ValueError("Product 资产清单输出越出允许根目录。")
    if candidate.name in {"", ".", ".."}:
        raise ValueError("Product 资产清单输出名称无效。")
    output = resolved_parent / candidate.name
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("Product 资产清单输出目录无效。")
    _reject_symlink_components(root, output.parent)
    return output


def _validated_existing_manifest(root: Path, manifest_path: Path) -> Path:
    candidate = (
        manifest_path if manifest_path.is_absolute() else root / manifest_path
    )
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("Product 资产清单必须是普通文件。")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Product 资产清单越出允许根目录。")
    _reject_symlink_components(root, candidate)
    return resolved


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise ValueError("Product 资产路径越出允许根目录。") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(
                f"Product 资产路径包含 symlink："
                f"{current.relative_to(root).as_posix()}"
            )


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"Product 资产路径越界或不规范：{value}")
    return path


def _validate_revision(value: str) -> None:
    if _REVISION.fullmatch(value) is None:
        raise ValueError("Product 资产 source revision 格式无效。")


def _validate_mode(mode: int, path: str) -> None:
    if mode & _UNSAFE_MODE_MASK or not mode & stat.S_IRUSR:
        raise ValueError(f"Product 资产权限不安全：{path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()
