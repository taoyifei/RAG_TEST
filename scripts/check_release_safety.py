"""按仓库、暂存区或真实交付候选执行发布安全检查。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterable, Sequence
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
    r"(?:/home/(?i:[a-z_][a-z0-9_-]{0,31})/|"
    r"/Users/[A-Za-z0-9._-]+/|"
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
_MANIFEST_ROW_PATTERN = re.compile(
    r"^(?P<digest>[0-9a-f]{64}) (?P<marker>[ *])(?P<path>.+)$"
)
_RELEASE_ARCHIVE_PATTERN = re.compile(
    r"^rag-runtime-[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.tar\.gz$"
)
_CORPUS_ARCHIVE_PATTERN = re.compile(
    r"^rag-corpus-[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.tar\.gz$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CORPUS_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_APPROVED_RUNTIME_ARCHIVES = frozenset(
    {
        "images/docx-rag-linux-amd64.tar",
        "images/docx-rag-ocr-linux-amd64.tar",
        "images/qdrant-linux-amd64.tar",
    }
)
_GIT_EXECUTABLE = shutil.which("git")


class ReleaseSafetyError(ValueError):
    """表示扫描输入或 manifest 完整性不满足硬约束。"""


@dataclass(frozen=True, slots=True)
class _Candidate:
    """保存一个待扫描路径及其不可变候选内容。"""

    path: str
    content: bytes
    approved_binary: bool = False
    check_private_path: bool = True


@dataclass(frozen=True, slots=True)
class ReleaseSafetyReport:
    """发布安全检查的机器可读结果。"""

    mode: str
    tracked_files: int
    private_paths: tuple[str, ...]
    private_network_matches: tuple[str, ...]
    local_path_matches: tuple[str, ...]
    secret_matches: tuple[str, ...]
    binary_files: tuple[str, ...]
    large_files: tuple[str, ...]
    integrity_errors: tuple[str, ...] = ()

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
                self.integrity_errors,
            )
        )

    def as_dict(self) -> dict[str, int | bool | str | list[str]]:
        """转换为稳定的 JSON 兼容结构。

        Args:
            无参数。

        Returns:
            包含模式、各类别数量、详情和总结论的字典。

        """
        categories = {
            "private_paths": self.private_paths,
            "private_network_matches": self.private_network_matches,
            "local_path_matches": self.local_path_matches,
            "secret_matches": self.secret_matches,
            "binary_files": self.binary_files,
            "large_files": self.large_files,
            "integrity_errors": self.integrity_errors,
        }
        payload: dict[str, int | bool | str | list[str]] = {
            "mode": self.mode,
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
    """扫描 Git index 中的完整 tracked tree。

    Args:
        repository: 已初始化 Git 的仓库根目录。
        max_file_bytes: 单个文本候选允许的最大字节数。

    Returns:
        完整仓库审计报告。

    Raises:
        ValueError: 文件上限不是正整数。
        RuntimeError: Git index 无法读取。

    """
    root = repository.resolve(strict=True)
    paths = _tracked_paths(root)
    candidates = tuple(
        _Candidate(path=path, content=_index_blob(root, path)) for path in paths
    )
    return _scan_candidates(
        "repository",
        candidates,
        max_file_bytes=max_file_bytes,
    )


def scan_staged(
    repository: Path,
    *,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> ReleaseSafetyReport:
    """只扫描 Git index 中本次 ACMR 目标内容。

    Args:
        repository: 已初始化 Git 的仓库根目录。
        max_file_bytes: 单个文本候选允许的最大字节数。

    Returns:
        本次待提交候选的审计报告；纯删除和空 index 均为空报告。

    Raises:
        ValueError: 文件上限不是正整数。
        RuntimeError: Git index 无法读取。

    """
    root = repository.resolve(strict=True)
    paths = _staged_target_paths(root)
    candidates = tuple(
        _Candidate(path=path, content=_index_blob(root, path)) for path in paths
    )
    return _scan_candidates(
        "staged",
        candidates,
        max_file_bytes=max_file_bytes,
    )


def scan_release(
    *,
    delivery_root: Path,
    runtime_root: Path,
    corpus_root: Path,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> ReleaseSafetyReport:
    """验证并扫描真实 delivery、runtime 与 corpus payload。

    Args:
        delivery_root: 含顶层七文件的 canonical 真实目录。
        runtime_root: 安全解包后的 canonical runtime 目录。
        corpus_root: 安全解包后的 canonical corpus 目录。
        max_file_bytes: 非批准文本候选允许的最大字节数。

    Returns:
        真实发布候选的安全报告。

    Raises:
        ReleaseSafetyError: root、manifest、路径或 corpus 契约无效。
        ValueError: 文件上限不是正整数。

    """
    delivery = _canonical_directory(delivery_root, "delivery_root")
    runtime = _canonical_directory(runtime_root, "runtime_root")
    corpus = _canonical_directory(corpus_root, "corpus_root")
    delivery_entries = _verified_manifest_entries(
        delivery,
        "RELEASE_MANIFEST.sha256",
        "delivery",
    )
    runtime_entries = _verified_manifest_entries(
        runtime,
        "MANIFEST.sha256",
        "runtime",
    )
    corpus_entries = _verified_manifest_entries(
        corpus,
        "MANIFEST.sha256",
        "corpus",
    )
    delivery_archives = _validate_delivery_set(delivery_entries)
    if not _APPROVED_RUNTIME_ARCHIVES.issubset(runtime_entries):
        raise ReleaseSafetyError("runtime 缺少三份精确批准的镜像归档。")
    corpus_documents = _verified_corpus_documents(corpus, corpus_entries)
    candidates: list[_Candidate] = []
    candidates.extend(
        _manifest_candidates(
            delivery,
            delivery_entries,
            "delivery",
            approved_binaries=delivery_archives,
        )
    )
    candidates.extend(
        _manifest_candidates(
            runtime,
            runtime_entries,
            "runtime",
            approved_binaries=_APPROVED_RUNTIME_ARCHIVES,
        )
    )
    candidates.extend(
        _manifest_candidates(
            corpus,
            corpus_entries,
            "corpus",
            approved_binaries=corpus_documents,
            private_path_exemptions=corpus_documents,
        )
    )
    return _scan_candidates(
        "release",
        tuple(candidates),
        max_file_bytes=max_file_bytes,
    )


def _scan_candidates(
    mode: str,
    candidates: Sequence[_Candidate],
    *,
    max_file_bytes: int,
) -> ReleaseSafetyReport:
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes 必须是正整数。")
    private_paths: list[str] = []
    private_network_matches: list[str] = []
    local_path_matches: list[str] = []
    secret_matches: list[str] = []
    binary_files: list[str] = []
    large_files: list[str] = []
    for candidate in candidates:
        if candidate.check_private_path and _is_private_path(candidate.path):
            private_paths.append(candidate.path)
        if candidate.approved_binary:
            continue
        if len(candidate.content) > max_file_bytes:
            large_files.append(candidate.path)
        probe = candidate.content[:_BINARY_PROBE_BYTES]
        if _is_binary(candidate.path, probe):
            binary_files.append(candidate.path)
            continue
        try:
            text = candidate.content.decode("utf-8")
        except UnicodeDecodeError:
            binary_files.append(candidate.path)
            continue
        if _RFC1918_PATTERN.search(text):
            private_network_matches.append(candidate.path)
        if _LOCAL_PATH_PATTERN.search(text):
            local_path_matches.append(candidate.path)
        if _contains_secret(text):
            secret_matches.append(candidate.path)
    return ReleaseSafetyReport(
        mode=mode,
        tracked_files=len(candidates),
        private_paths=tuple(private_paths),
        private_network_matches=tuple(private_network_matches),
        local_path_matches=tuple(local_path_matches),
        secret_matches=tuple(secret_matches),
        binary_files=tuple(binary_files),
        large_files=tuple(large_files),
    )


def _git_bytes(repository: Path, arguments: Sequence[str]) -> bytes:
    if _GIT_EXECUTABLE is None:
        raise RuntimeError("找不到 git 可执行文件。")
    completed = subprocess.run(  # noqa: S603
        [_GIT_EXECUTABLE, "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"无法读取 Git index：{message}")
    return completed.stdout


def _tracked_paths(repository: Path) -> tuple[str, ...]:
    output = _git_bytes(repository, ("ls-files", "-z"))
    return tuple(
        sorted(item.decode("utf-8") for item in output.split(b"\0") if item)
    )


def _staged_target_paths(repository: Path) -> tuple[str, ...]:
    output = _git_bytes(
        repository,
        (
            "diff",
            "--cached",
            "--name-status",
            "-z",
            "--diff-filter=ACMR",
            "--find-renames",
            "--find-copies",
            "--",
        ),
    )
    fields = [item for item in output.split(b"\0") if item]
    targets: list[str] = []
    index = 0
    while index < len(fields):
        status_value = fields[index].decode("ascii")
        index += 1
        if not status_value or status_value[0] not in "ACMR":
            raise RuntimeError("Git staged 状态输出无效。")
        if status_value[0] in "CR":
            if index + 1 >= len(fields):
                raise RuntimeError("Git rename/copy 状态输出不完整。")
            index += 1
        if index >= len(fields):
            raise RuntimeError("Git staged 目标路径缺失。")
        targets.append(fields[index].decode("utf-8"))
        index += 1
    return tuple(sorted(set(targets)))


def _index_blob(repository: Path, relative_path: str) -> bytes:
    return _git_bytes(repository, ("show", f":{relative_path}"))


def _canonical_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ReleaseSafetyError(f"{label} 必须是 canonical 绝对目录。")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ReleaseSafetyError(
            f"{label} 必须是 canonical 真实目录。"
        ) from error
    if path != resolved or not resolved.is_dir():
        raise ReleaseSafetyError(f"{label} 必须是 canonical 真实目录。")
    current = Path(resolved.anchor)
    for component in resolved.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ReleaseSafetyError(f"{label} 不能含符号链接祖先。")
    return resolved


def _regular_relative_files(root: Path, label: str) -> tuple[str, ...]:
    files: list[str] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ReleaseSafetyError(f"{label} 不能含符号链接。")
            if not stat.S_ISDIR(mode):
                raise ReleaseSafetyError(f"{label} 不能含特殊目录项。")
        for name in names:
            path = current_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ReleaseSafetyError(f"{label} 不能含符号链接。")
            if not stat.S_ISREG(mode):
                raise ReleaseSafetyError(f"{label} 不能含特殊文件。")
            files.append(path.relative_to(root).as_posix())
    return tuple(sorted(files))


def _verified_manifest_entries(
    root: Path,
    manifest_name: str,
    label: str,
) -> dict[str, str]:
    actual = _regular_relative_files(root, label)
    if manifest_name not in actual:
        raise ReleaseSafetyError(f"{label} 缺少 {manifest_name}。")
    manifest = root / manifest_name
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseSafetyError(
            f"{label} manifest 不是 UTF-8 普通文件。"
        ) from error
    entries: dict[str, str] = {}
    for line in lines:
        match = _MANIFEST_ROW_PATTERN.fullmatch(line)
        if match is None:
            raise ReleaseSafetyError(f"{label} manifest 行格式无效。")
        relative_path = _canonical_manifest_path(match.group("path"))
        if relative_path in entries or relative_path == manifest_name:
            raise ReleaseSafetyError(f"{label} manifest 路径重复或自包含。")
        entries[relative_path] = match.group("digest")
    expected = set(actual) - {manifest_name}
    if set(entries) != expected:
        raise ReleaseSafetyError(f"{label} manifest 与目录 exact set 不一致。")
    for relative_path, expected_digest in entries.items():
        if _sha256(root / relative_path) != expected_digest:
            raise ReleaseSafetyError(
                f"{label} manifest SHA256 不一致：{relative_path}"
            )
    return entries


def _canonical_manifest_path(value: str) -> str:
    relative = value[2:] if value.startswith("./") else value
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or path.as_posix() != relative
        or "." in path.parts
        or ".." in path.parts
        or "\\" in relative
        or "\x00" in relative
    ):
        raise ReleaseSafetyError("manifest 路径越界或不规范。")
    return relative


def _validate_delivery_set(entries: dict[str, str]) -> frozenset[str]:
    runtime_archives = tuple(
        path for path in entries if _RELEASE_ARCHIVE_PATTERN.fullmatch(path)
    )
    corpus_archives = tuple(
        path for path in entries if _CORPUS_ARCHIVE_PATTERN.fullmatch(path)
    )
    if len(runtime_archives) != 1 or len(corpus_archives) != 1:
        raise ReleaseSafetyError("delivery 必须恰有两份命名明确的 tar.gz。")
    archives = frozenset((*runtime_archives, *corpus_archives))
    expected = {
        *archives,
        *(f"{path}.sha256" for path in archives),
        "offline_bundle.py",
        "offline_bundle.py.sha256",
    }
    if set(entries) != expected:
        raise ReleaseSafetyError("delivery 顶层必须恰好是七文件契约。")
    return archives


def _verified_corpus_documents(
    corpus_root: Path,
    entries: dict[str, str],
) -> frozenset[str]:
    manifest_path = corpus_root / "CORPUS_MANIFEST.json"
    try:
        raw_manifest = manifest_path.read_bytes()
        value = json.loads(raw_manifest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseSafetyError(
            "corpus manifest 不是有效 UTF-8 JSON。"
        ) from error
    expected_fields = {
        "corpus_digest",
        "corpus_id",
        "document_count",
        "documents",
        "schema_version",
        "total_bytes",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ReleaseSafetyError("corpus manifest schema 无效。")
    items = value.get("documents")
    corpus_id = value.get("corpus_id")
    canonical_manifest = (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if (
        value.get("schema_version") != "1"
        or not isinstance(corpus_id, str)
        or _CORPUS_ID_PATTERN.fullmatch(corpus_id) is None
        or not isinstance(items, list)
        or raw_manifest != canonical_manifest
    ):
        raise ReleaseSafetyError("corpus manifest version 或 documents 无效。")
    documents: list[dict[str, object]] = []
    approved: list[str] = []
    total_bytes = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "size",
        }:
            raise ReleaseSafetyError("corpus document schema 无效。")
        relative = item["path"]
        digest = item["sha256"]
        size = item["size"]
        if not isinstance(relative, str):
            raise ReleaseSafetyError("corpus document path 类型无效。")
        relative = _canonical_docx_path(relative)
        if (
            not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
            or type(size) is not int
            or size < 0
        ):
            raise ReleaseSafetyError("corpus document 摘要或大小无效。")
        payload_path = f"docs/{relative}"
        candidate = corpus_root / payload_path
        if (
            payload_path not in entries
            or candidate.stat().st_size != size
            or _sha256(candidate) != digest
        ):
            raise ReleaseSafetyError("corpus DOCX 与冻结 manifest 不一致。")
        documents.append({"path": relative, "sha256": digest, "size": size})
        approved.append(payload_path)
        total_bytes += size
    if (
        not approved
        or approved != sorted(set(approved))
        or len({path.casefold() for path in approved}) != len(approved)
    ):
        raise ReleaseSafetyError("corpus document 路径为空、无序或重复。")
    digest = hashlib.sha256(
        json.dumps(
            documents,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if (
        value.get("document_count") != len(documents)
        or value.get("total_bytes") != total_bytes
        or value.get("corpus_digest") != digest
        or {
            path for path in entries if path.startswith("docs/")
        }
        != set(approved)
    ):
        raise ReleaseSafetyError(
            "corpus manifest count、digest 或 exact set 无效。"
        )
    return frozenset(approved)


def _canonical_docx_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
        or path.suffix.casefold() != ".docx"
        or any("zone.identifier" in part.casefold() for part in path.parts)
    ):
        raise ReleaseSafetyError("corpus document 路径越界或不规范。")
    return value


def _manifest_candidates(
    root: Path,
    entries: dict[str, str],
    label: str,
    *,
    approved_binaries: Iterable[str],
    private_path_exemptions: Iterable[str] = (),
) -> tuple[_Candidate, ...]:
    approved = frozenset(approved_binaries)
    exemptions = frozenset(private_path_exemptions)
    manifest_name = (
        "RELEASE_MANIFEST.sha256" if label == "delivery" else "MANIFEST.sha256"
    )
    paths = (*entries, manifest_name)
    candidates: list[_Candidate] = []
    for relative_path in paths:
        approved_binary = relative_path in approved
        candidates.append(
            _Candidate(
                path=f"{label}/{relative_path}",
                content=(
                    b""
                    if approved_binary
                    else (root / relative_path).read_bytes()
                ),
                approved_binary=approved_binary,
                check_private_path=relative_path not in exemptions,
            )
        )
    return tuple(candidates)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


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


def _add_max_file_bytes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=_DEFAULT_MAX_FILE_BYTES,
    )


def _arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("repository", "staged"):
        subparser = subparsers.add_parser(mode)
        subparser.add_argument("repository", type=Path)
        _add_max_file_bytes(subparser)
    release = subparsers.add_parser("release")
    release.add_argument("--delivery-root", type=Path, required=True)
    release.add_argument("--runtime-root", type=Path, required=True)
    release.add_argument("--corpus-root", type=Path, required=True)
    _add_max_file_bytes(release)
    return parser.parse_args(arguments)


def _integrity_failure(
    mode: str,
    error: ReleaseSafetyError,
) -> ReleaseSafetyReport:
    return ReleaseSafetyReport(
        mode=mode,
        tracked_files=0,
        private_paths=(),
        private_network_matches=(),
        local_path_matches=(),
        secret_matches=(),
        binary_files=(),
        large_files=(),
        integrity_errors=(str(error),),
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """运行显式扫描模式并输出单行 JSON。

    Args:
        arguments: 可选命令行参数；`None` 表示读取进程参数。

    Returns:
        安全时返回 0，发现违规或完整性错误时返回 1。

    """
    options = _arguments(arguments)
    try:
        if options.mode == "repository":
            report = scan_repository(
                options.repository,
                max_file_bytes=options.max_file_bytes,
            )
        elif options.mode == "staged":
            report = scan_staged(
                options.repository,
                max_file_bytes=options.max_file_bytes,
            )
        else:
            report = scan_release(
                delivery_root=options.delivery_root,
                runtime_root=options.runtime_root,
                corpus_root=options.corpus_root,
                max_file_bytes=options.max_file_bytes,
            )
    except ReleaseSafetyError as error:
        report = _integrity_failure(options.mode, error)
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
