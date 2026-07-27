"""下载并严格核验 OCR 镜像所需的 CPython 3.10 wheels。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

_DOWNLOAD_TIMEOUT_SECONDS = 900
_HASH_BLOCK_BYTES = 1024 * 1024
_MANIFEST_FIELD_COUNT = 2
_SHA256_HEXDIGEST_LENGTH = 64


def verify_wheelhouse(
    wheelhouse: Path,
    expected: Mapping[str, str],
) -> None:
    """核验 wheelhouse 的文件集合与全部 SHA256。

    Args:
        wheelhouse: ignored 的本地 wheel 目录。
        expected: wheel 文件名到 SHA256 的固定映射。

    Returns:
        全部文件与摘要一致时返回 `None`。

    Raises:
        ValueError: 文件缺失、多余、名称越界或摘要漂移。

    """
    actual = {
        path.name: path
        for path in wheelhouse.iterdir()
        if path.is_file()
    }
    if set(actual) != set(expected):
        raise ValueError("OCR wheelhouse 文件集合与固定 manifest 不一致。")
    for name, expected_sha256 in expected.items():
        if Path(name).name != name or not name.endswith(".whl"):
            raise ValueError("OCR wheel manifest 含非法文件名。")
        if _sha256(actual[name]) != expected_sha256:
            raise ValueError(f"OCR wheel SHA256 不一致：{name}")


def main(arguments: Sequence[str] | None = None) -> int:
    """从 PyPI 下载固定 CPython 3.10 wheels 并核验。

    Args:
        arguments: 可选命令行参数；`None` 表示进程参数。

    Returns:
        下载结果与固定 manifest 完全一致时返回 0。

    Raises:
        ValueError: 现有或下载后的 wheelhouse 与 manifest 不一致。
        subprocess.SubprocessError: pip 下载失败或超时。

    """
    options = _arguments(arguments)
    expected = _read_manifest(options.manifest)
    options.output.mkdir(parents=True, exist_ok=True)
    if any(options.output.iterdir()):
        verify_wheelhouse(options.output, expected)
    else:
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--index-url",
            "https://pypi.org/simple",
            "--dest",
            str(options.output),
            "--platform",
            "manylinux2014_x86_64",
            "--python-version",
            "3.10",
            "--implementation",
            "cp",
            "--only-binary=:all:",
            "--no-deps",
            "--requirement",
            str(options.requirements),
        ]
        subprocess.run(  # noqa: S603
            command,
            check=True,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        )
        verify_wheelhouse(options.output, expected)
    print(
        json.dumps(
            {
                "platform": "linux/amd64",
                "python": "3.10",
                "verified_wheels": len(expected),
            },
            sort_keys=True,
        )
    )
    return 0


def _read_manifest(path: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        fields = line.split(maxsplit=1)
        if len(fields) != _MANIFEST_FIELD_COUNT:
            raise ValueError(f"OCR wheel manifest 第 {line_number} 行无效。")
        sha256, name = fields
        if (
            len(sha256) != _SHA256_HEXDIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in sha256)
            or name in expected
        ):
            raise ValueError(f"OCR wheel manifest 第 {line_number} 行无效。")
        expected[name] = sha256
    if not expected:
        raise ValueError("OCR wheel manifest 为空。")
    return expected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--requirements",
        type=Path,
        default=Path("deployment/ocr/requirements.lock"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("deployment/ocr/WHEELS.sha256"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deployment/ocr/assets/wheelhouse"),
    )
    return parser.parse_args(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
