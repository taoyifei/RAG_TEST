"""扫描源码、产物与容器元数据中的常见 Secret 形状。"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_MAX_FILE_BYTES = 16 * 1024 * 1024
_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jina-api-key": re.compile(rb"\bjina_[A-Za-z0-9_-]{20,}\b"),
    "provider-sk-key": re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "github-token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "aws-access-key": re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
}


@dataclass(frozen=True, slots=True)
class Finding:
    """不包含 Secret 原文的扫描结果。"""

    location: str
    rule: str


def scan_bytes(content: bytes, *, location: str) -> tuple[Finding, ...]:
    """扫描字节并只返回位置与规则。

    Args:
        content: 待检查内容。
        location: 用户可识别但不含 Secret 的位置。

    Returns:
        不含匹配值的发现列表。

    """
    return tuple(
        Finding(location=location, rule=rule)
        for rule, pattern in _PATTERNS.items()
        if pattern.search(content) is not None
    )


def _tracked_paths(repository: Path) -> tuple[Path, ...]:
    completed = subprocess.run(  # noqa: S603
        [_executable("git"), "ls-files", "-z"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return tuple(
        repository / item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _walk_path(path: Path) -> tuple[Path, ...]:
    if path.is_file() and not path.is_symlink():
        return (path,)
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"扫描路径无效：{path}")
    return tuple(item for item in path.rglob("*") if item.is_file())


def _scan_paths(paths: tuple[Path, ...]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for path in dict.fromkeys(paths):
        if path.is_symlink() or path.stat().st_size > _MAX_FILE_BYTES:
            continue
        findings.extend(scan_bytes(path.read_bytes(), location=str(path)))
    return tuple(findings)


def _container_metadata(image: str) -> bytes:
    docker = _executable("docker")
    inspect = subprocess.run(  # noqa: S603
        [docker, "image", "inspect", image],
        check=True,
        capture_output=True,
    )
    history = subprocess.run(  # noqa: S603
        [docker, "history", "--no-trunc", image],
        check=True,
        capture_output=True,
    )
    return inspect.stdout + b"\n" + history.stdout


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise OSError(f"缺少命令：{name}")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="追加扫描文件或目录；可重复。",
    )
    parser.add_argument(
        "--docker-image",
        help="同时扫描该镜像的 inspect 与完整 history。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行脱敏 Secret 扫描。

    Args:
        argv: 可选命令行参数。

    Returns:
        0 表示未发现，1 表示发现 Secret 形状，2 表示扫描失败。

    """
    args = _parser().parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    try:
        paths = list(_tracked_paths(repository))
        frontend_dist = repository / "frontend" / "dist"
        if frontend_dist.is_dir():
            paths.extend(_walk_path(frontend_dist))
        for value in args.path:
            paths.extend(_walk_path(Path(value).resolve(strict=True)))
        findings = list(_scan_paths(tuple(paths)))
        if args.docker_image:
            findings.extend(
                scan_bytes(
                    _container_metadata(args.docker_image),
                    location=f"docker-image:{args.docker_image}",
                )
            )
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"SECRET_SCAN_ERROR type={type(error).__name__}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"SECRET_FINDING rule={finding.rule} location={finding.location}")
    if findings:
        print(f"SECRET_SCAN_FAILED findings={len(findings)}")
        return 1
    print(f"SECRET_SCAN_OK files={len(dict.fromkeys(paths))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
