"""下载、核验并安全解包固定 PaddleOCR 资产。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

_ALLOWED_HOSTS = frozenset(
    {
        "paddle-model-ecology.bj.bcebos.com",
        "raw.githubusercontent.com",
    }
)
_MAX_ARCHIVE_MEMBERS = 32
_MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
_DOWNLOAD_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """一项固定来源、大小和摘要的离线资产。"""

    name: str
    kind: str
    url: str
    sha256: str
    bytes: int
    top_level_directory: str | None = None
    spdx: str | None = None


def main(arguments: Sequence[str] | None = None) -> int:
    """下载全部固定资产并输出本地 manifest。

    Args:
        arguments: 可选命令行参数；`None` 表示进程参数。

    Returns:
        所有下载、解包和摘要校验成功时返回 0。

    """
    options = _arguments(arguments)
    specs = _load_specs(options.sources)
    downloads = options.output / "downloads"
    models = options.output / "models"
    licenses = options.output / "licenses"
    for directory in (downloads, models, licenses):
        directory.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        archive = downloads / spec.name
        _download_or_verify(spec, archive)
        if spec.kind == "model_archive":
            if spec.top_level_directory is None:
                raise ValueError("模型归档缺少顶层目录约束。")
            safe_extract_tar(
                archive,
                models,
                expected_top_level=spec.top_level_directory,
            )
        elif spec.kind == "license":
            _copy_if_identical(archive, licenses / spec.name)
        else:
            raise ValueError(f"未知 OCR 资产类型：{spec.kind}")
    manifest_path = options.output / "MANIFEST.sha256"
    _write_manifest(options.output, manifest_path)
    print(
        json.dumps(
            {
                "assets": len(specs),
                "manifest": str(manifest_path),
                "models": sum(
                    spec.kind == "model_archive" for spec in specs
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def safe_extract_tar(
    archive: Path,
    output_root: Path,
    *,
    expected_top_level: str,
) -> None:
    """不跟随链接且不覆盖不一致文件地解包 tar。

    Args:
        archive: 已完成 SHA256 校验的 tar 文件。
        output_root: ignored 模型资产根目录。
        expected_top_level: 归档唯一允许的顶层目录。

    Returns:
        无返回值。

    Raises:
        ValueError: 归档越界、含链接/特殊文件或超过资源上限。

    """
    root = output_root.resolve()
    expected_files: set[Path] = set()
    total_bytes = 0
    with tarfile.open(archive, mode="r:") as tar:
        members = tar.getmembers()
        if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
            raise ValueError("OCR tar 成员数量无效。")
        for member in members:
            relative = _safe_member_path(
                member,
                expected_top_level=expected_top_level,
            )
            destination = root / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            total_bytes += member.size
            if total_bytes > _MAX_EXTRACTED_BYTES:
                raise ValueError("OCR tar 解包总量超过上限。")
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ValueError("OCR tar 普通文件无法读取。")
            expected_files.add(relative)
            _write_member(extracted, destination, member.size)
    model_root = root / expected_top_level
    actual_files = {
        path.relative_to(root)
        for path in model_root.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError("OCR 模型目录含归档外文件或缺少文件。")


def _safe_member_path(
    member: tarfile.TarInfo,
    *,
    expected_top_level: str,
) -> Path:
    if not (member.isdir() or member.isreg()):
        raise ValueError("OCR tar 不允许链接或特殊文件。")
    pure = PurePosixPath(member.name)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or pure.parts[0] != expected_top_level
    ):
        raise ValueError("OCR tar 成员路径越界。")
    return Path(*pure.parts)


def _write_member(
    source: IO[bytes],
    destination: Path,
    expected_bytes: int,
) -> None:
    content = source.read(expected_bytes + 1)
    if len(content) != expected_bytes:
        raise ValueError("OCR tar 成员大小与 header 不一致。")
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != content:
            raise ValueError(f"OCR 资产拒绝覆盖：{destination.name}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        output.write(content)
    destination.chmod(0o444)


def _download_or_verify(spec: AssetSpec, destination: Path) -> None:
    if destination.exists():
        _verify_asset(spec, destination)
        return
    _validate_url(spec.url)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(  # noqa: S310
        spec.url,
        headers={"User-Agent": "docx-rag-asset-fetch/1"},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=60,
        ) as response:
            _validate_url(response.geturl())
            with temporary.open("xb") as output:
                while block := response.read(_DOWNLOAD_BLOCK_BYTES):
                    output.write(block)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    _verify_asset(spec, temporary)
    temporary.replace(destination)
    destination.chmod(0o444)


def _verify_asset(spec: AssetSpec, path: Path) -> None:
    if path.stat().st_size != spec.bytes:
        raise ValueError(f"OCR 资产大小不一致：{spec.name}")
    if _sha256(path) != spec.sha256:
        raise ValueError(f"OCR 资产 SHA256 不一致：{spec.name}")


def _copy_if_identical(source: Path, destination: Path) -> None:
    content = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() != content:
            raise ValueError("OCR 许可证文件不一致。")
        return
    destination.write_bytes(content)
    destination.chmod(0o444)


def _write_manifest(root: Path, manifest_path: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path == manifest_path:
            continue
        lines.append(f"{_sha256(path)}  {path.relative_to(root).as_posix()}")
    content = "\n".join(lines) + "\n"
    if manifest_path.exists() and manifest_path.read_text("utf-8") == content:
        return
    manifest_path.write_text(content, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_DOWNLOAD_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _validate_url(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("OCR 资产 URL 不在 HTTPS 来源白名单。")


def _load_specs(path: Path) -> tuple[AssetSpec, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list) or not assets:
        raise ValueError("OCR 资产来源清单为空。")
    return tuple(AssetSpec(**item) for item in assets)


def _arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("deployment/ocr/ASSET_SOURCES.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deployment/ocr/assets"),
    )
    return parser.parse_args(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
