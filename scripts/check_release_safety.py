"""检查 Git 候选内容是否适合发布到可能公开的仓库。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_DEFAULT_MAX_FILE_BYTES = 1_000_000
_BINARY_PROBE_BYTES = 8_192
_PRIVATE_COMPONENTS = frozenset(
    {
        "artifacts",
        "docs",
        "evidence",
        "frozen",
        "models",
        "results",
        "tokenizers",
        "wheelhouse",
    }
)
_BINARY_SUFFIXES = frozenset(
    {
        ".bin",
        ".doc",
        ".docx",
        ".engine",
        ".gz",
        ".onnx",
        ".pdf",
        ".pdiparams",
        ".pdmodel",
        ".plan",
        ".ppt",
        ".pptx",
        ".safetensors",
        ".so",
        ".tar",
        ".whl",
        ".xls",
        ".xlsx",
        ".zip",
    }
)
_RFC1918_PATTERN = re.compile(
    r"(?<![0-9])(?:"
    r"10(?:\.[0-9]{1,3}){3}|"
    r"192\.168(?:\.[0-9]{1,3}){2}|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2}"
    r")(?![0-9])"
)
_LOCAL_PATH_PATTERN = re.compile(
    r"(?:/(?:home|Users)/[A-Za-z0-9._-]+/|"
    r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\)"
)
_AWS_ACCESS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)\b(?:api[_-]?key|access[_-]?key|secret|token|password)"
    r"\b[ \t]*[:=][ \t]*(?P<quote>[\"']?)(?P<value>[^\s\"'#]+)"
)
_REFERENCE_VALUE_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*(?:\([^ \t\r\n]*\))?[,]?$"
)
_SAFE_CREDENTIAL_MARKERS = (
    "${",
    "CHANGEME",
    "DUMMY",
    "EXAMPLE",
    "PLACEHOLDER",
    "REPLACE",
)
_GIT_EXECUTABLE = shutil.which("git")


@dataclass(frozen=True, slots=True)
class ReleaseSafetyReport:
    """发布安全检查的机器可读结果。"""

    tracked_files: int
    private_paths: tuple[str, ...]
    private_network_matches: tuple[str, ...]
    local_path_matches: tuple[str, ...]
    secret_matches: tuple[str, ...]
    binary_files: tuple[str, ...]
    large_files: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """返回所有违规类别是否均为空。

        Args:
            无参数。

        Returns:
            所有违规类别均为空时返回 `True`。

        """
        return not any(
            (
                self.private_paths,
                self.private_network_matches,
                self.local_path_matches,
                self.secret_matches,
                self.binary_files,
                self.large_files,
            )
        )

    def as_dict(self) -> dict[str, int | bool | list[str]]:
        """转换为稳定的 JSON 兼容结构。

        Args:
            无参数。

        Returns:
            包含各类别数量、详情和总结论的字典。

        """
        categories = {
            "private_paths": self.private_paths,
            "private_network_matches": self.private_network_matches,
            "local_path_matches": self.local_path_matches,
            "secret_matches": self.secret_matches,
            "binary_files": self.binary_files,
            "large_files": self.large_files,
        }
        payload: dict[str, int | bool | list[str]] = {
            "passed": self.passed,
            "tracked_files": self.tracked_files,
            "violations": sum(len(items) for items in categories.values()),
        }
        for name, items in categories.items():
            payload[name] = len(items)
            payload[f"{name}_details"] = list(items)
        return payload


def scan_repository(
    repository: Path,
    *,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> ReleaseSafetyReport:
    """扫描 Git 索引中的全部候选文件。

    Args:
        repository: 已初始化 Git 的仓库根目录。
        max_file_bytes: 单个可跟踪文件允许的最大字节数。

    Returns:
        分类别且可序列化的发布安全报告。

    Raises:
        ValueError: 文件上限不是正整数。
        RuntimeError: Git 索引无法读取或候选文件缺失。

    """
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes 必须是正整数。")
    root = repository.resolve(strict=True)
    tracked_paths = _tracked_paths(root)
    private_paths: list[str] = []
    private_network_matches: list[str] = []
    local_path_matches: list[str] = []
    secret_matches: list[str] = []
    binary_files: list[str] = []
    large_files: list[str] = []
    for relative_path in tracked_paths:
        if _is_private_path(relative_path):
            private_paths.append(relative_path)
        candidate = root / relative_path
        if not candidate.is_file():
            raise RuntimeError(f"Git 候选文件不存在：{relative_path}")
        size = candidate.stat().st_size
        if size > max_file_bytes:
            large_files.append(relative_path)
        probe = candidate.read_bytes()[:_BINARY_PROBE_BYTES]
        if _is_binary(relative_path, probe):
            binary_files.append(relative_path)
            continue
        text = candidate.read_text(encoding="utf-8")
        if _RFC1918_PATTERN.search(text):
            private_network_matches.append(relative_path)
        if _LOCAL_PATH_PATTERN.search(text):
            local_path_matches.append(relative_path)
        if _contains_secret(text):
            secret_matches.append(relative_path)
    return ReleaseSafetyReport(
        tracked_files=len(tracked_paths),
        private_paths=tuple(private_paths),
        private_network_matches=tuple(private_network_matches),
        local_path_matches=tuple(local_path_matches),
        secret_matches=tuple(secret_matches),
        binary_files=tuple(binary_files),
        large_files=tuple(large_files),
    )


def _tracked_paths(repository: Path) -> tuple[str, ...]:
    if _GIT_EXECUTABLE is None:
        raise RuntimeError("找不到 git 可执行文件。")
    completed = subprocess.run(  # noqa: S603
        [_GIT_EXECUTABLE, "-C", str(repository), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"无法读取 Git 索引：{message}")
    return tuple(
        sorted(
            item.decode("utf-8")
            for item in completed.stdout.split(b"\0")
            if item
        )
    )


def _is_private_path(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    if path.name == ".env.example":
        return False
    if path.name == ".env" or path.name.startswith(".env."):
        return True
    if "Zone.Identifier" in path.name:
        return True
    if any(part in _PRIVATE_COMPONENTS for part in path.parts):
        return True
    return (
        path.parent == PurePosixPath("design")
        and path.name.startswith("acceptance-and-offline-deployment-")
    )


def _is_binary(relative_path: str, probe: bytes) -> bool:
    suffix = PurePosixPath(relative_path).suffix.lower()
    return suffix in _BINARY_SUFFIXES or b"\0" in probe


def _contains_secret(text: str) -> bool:
    if _AWS_ACCESS_KEY_PATTERN.search(text):
        return True
    if _PRIVATE_KEY_PATTERN.search(text):
        return True
    for match in _CREDENTIAL_ASSIGNMENT_PATTERN.finditer(text):
        raw_value = match.group("value")
        value = raw_value.upper()
        if not any(marker in value for marker in _SAFE_CREDENTIAL_MARKERS):
            if raw_value.startswith(("$", "{")):
                continue
            if match.group("quote"):
                return True
            if value in {"NONE", "NULL", "TRUE", "FALSE"}:
                continue
            if _REFERENCE_VALUE_PATTERN.fullmatch(
                raw_value
            ) or _REFERENCE_VALUE_PATTERN.fullmatch(
                raw_value.rstrip(")]},")
            ):
                continue
            return True
    return False


def _arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=_DEFAULT_MAX_FILE_BYTES,
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """运行发布检查并输出单行 JSON。

    Args:
        arguments: 可选的命令行参数；`None` 表示读取进程参数。

    Returns:
        安全时返回 0，否则返回 1。

    """
    options = _arguments(arguments)
    report = scan_repository(
        options.repository,
        max_file_bytes=options.max_file_bytes,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
