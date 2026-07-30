"""校验双包摘要并拒绝不安全 tar 成员。"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

_HASH_BLOCK_BYTES = 1024 * 1024
_PUBLISH_ARGUMENT_COUNT = 4
_SHA_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def verify_outer_sidecar(archive: Path, sidecar: Path) -> str:
    """校验外层 `.sha256` 只绑定当前归档文件。

    Args:
        archive: 待校验的 runtime 或 corpus 归档。
        sidecar: 与归档同名并追加 `.sha256` 的文件。

    Returns:
        实际归档 SHA256。

    Raises:
        ValueError: sidecar 格式、文件名或摘要不匹配。

    """
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError("外层 SHA256 sidecar 必须恰有一行。")
    match = _SHA_LINE.fullmatch(lines[0])
    if match is None or match.group(2) != archive.name:
        raise ValueError("外层 SHA256 sidecar 未绑定当前归档名。")
    actual = _sha256_file(archive)
    if match.group(1) != actual:
        raise ValueError("外层 SHA256 与归档内容不一致。")
    return actual


def verify_file_manifest(
    root: Path,
    manifest: Path,
    *,
    require_exact: bool = False,
) -> int:
    """校验清单点名的普通文件及可选的精确文件集合。

    Args:
        root: 清单中相对路径的可信根目录。
        manifest: UTF-8 SHA256 清单。
        require_exact: 是否拒绝清单外的其他普通文件。

    Returns:
        已校验文件数。

    Raises:
        ValueError: 路径、文件集合或任一摘要无效。

    """
    resolved_root = root.resolve(strict=True)
    manifest_path = manifest.resolve(strict=True)
    expected: dict[PurePosixPath, str] = {}
    for line_number, line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        match = _SHA_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"SHA256 清单第 {line_number} 行无效。")
        relative = _safe_relative_path(match.group(2))
        if relative in expected:
            raise ValueError("SHA256 清单含重复路径。")
        expected[relative] = match.group(1)
    if not expected:
        raise ValueError("SHA256 清单不能为空。")
    for relative, digest in expected.items():
        path = resolved_root.joinpath(*relative.parts)
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"SHA256 清单文件不存在：{relative}")
        if _sha256_file(path) != digest:
            raise ValueError(f"文件 SHA256 不一致：{relative}")
    if require_exact:
        actual = {
            PurePosixPath(path.relative_to(resolved_root).as_posix())
            for path in resolved_root.rglob("*")
            if path.is_file()
            and path.resolve() != manifest_path
        }
        if actual != set(expected):
            raise ValueError("归档文件集合与 MANIFEST.sha256 不一致。")
    return len(expected)


def safe_extract_bundle(
    archive: Path,
    sidecar: Path,
    destination: Path,
    *,
    expected_top_level: str,
) -> Path:
    """校验并安全解包单一 runtime 或 corpus 归档。

    Args:
        archive: 待解包的 tar.gz。
        sidecar: 归档对应的外层 SHA256。
        destination: 不含最终顶层目录的目标父目录。
        expected_top_level: 唯一允许的归档顶层目录。

    Returns:
        已完成逐文件 manifest 校验的最终目录。

    Raises:
        FileExistsError: 目标目录已存在。
        ValueError: 外层摘要、tar 成员或逐文件 manifest 无效。

    """
    verify_outer_sidecar(archive, sidecar)
    top_level = _safe_relative_path(expected_top_level)
    if len(top_level.parts) != 1:
        raise ValueError("expected_top_level 必须是单层目录名。")
    destination.mkdir(parents=True, exist_ok=True)
    final_path = destination / expected_top_level
    if final_path.exists():
        raise FileExistsError(f"解包目标已存在：{final_path}")
    with tempfile.TemporaryDirectory(
        dir=destination,
        prefix=".extract-",
    ) as temporary_name:
        temporary = Path(temporary_name)
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = bundle.getmembers()
            _validate_members(members, expected_top_level)
            _extract_regular_members(bundle, members, temporary)
        extracted = temporary / expected_top_level
        manifest = extracted / "MANIFEST.sha256"
        verify_file_manifest(extracted, manifest, require_exact=True)
        extracted.replace(final_path)
    return final_path


def publish_directory(source: Path, destination: Path) -> None:
    """在同一真实父目录内原子发布且拒绝覆盖。

    Args:
        source: 已完整生成的临时目录。
        destination: 尚不存在的正式目录。

    Returns:
        无。

    Raises:
        FileExistsError: 正式目录已存在。
        OSError: 路径、文件系统或 renameat2 调用无效。

    """
    if not source.is_dir() or source.is_symlink():
        raise OSError("原子发布源必须是真实目录。")
    source_parent = source.parent.resolve(strict=True)
    destination_parent = destination.parent.resolve(strict=True)
    if source_parent != destination_parent:
        raise OSError("原子发布要求源和目标位于同一真实父目录。")
    if source.name in {"", ".", ".."} or destination.name in {"", ".", ".."}:
        raise OSError("原子发布目录名无效。")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            "原子发布目标已存在。",
            destination,
        )
    raise OSError(error_number, os.strerror(error_number), destination)


def _validate_members(
    members: list[tarfile.TarInfo],
    expected_top_level: str,
) -> None:
    if not members:
        raise ValueError("离线归档不能为空。")
    seen: set[PurePosixPath] = set()
    for member in members:
        relative = _safe_relative_path(member.name)
        if relative.parts[0] != expected_top_level:
            raise ValueError("离线归档含错误顶层目录。")
        if relative in seen:
            raise ValueError("离线归档含重复成员。")
        seen.add(relative)
        if not (member.isdir() or member.isfile()):
            raise ValueError("离线归档只允许普通文件和目录。")


def _extract_regular_members(
    bundle: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    destination: Path,
) -> None:
    resolved_destination = destination.resolve(strict=True)
    for member in members:
        relative = _safe_relative_path(member.name)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(resolved_destination):
            raise ValueError("离线归档成员越界。")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        source = bundle.extractfile(member)
        if source is None:
            raise ValueError("离线归档普通文件无法读取。")
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=_HASH_BLOCK_BYTES)


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
    ):
        raise ValueError(f"离线归档路径越界或不规范：{value}")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("sidecar", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--top-level", required=True)
    return parser.parse_args()


def main() -> int:
    """校验并安全解包命令行指定的离线包。

    Args:
        无参数。

    Returns:
        成功时返回 0；异常由调用方看到并产生非零退出码。

    """
    if len(sys.argv) > 1 and sys.argv[1] == "publish":
        if len(sys.argv) != _PUBLISH_ARGUMENT_COUNT:
            raise ValueError("publish 必须提供 source 和 destination。")
        destination = Path(sys.argv[3])
        publish_directory(Path(sys.argv[2]), destination)
        print(f"published={destination}")
        return 0
    arguments = _arguments()
    extracted = safe_extract_bundle(
        arguments.archive,
        arguments.sidecar,
        arguments.destination,
        expected_top_level=arguments.top_level,
    )
    print(f"verified_and_extracted={extracted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
