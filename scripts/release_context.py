"""从不可变 Git 提交导出最小、可审计的镜像构建上下文。"""

from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts.secret_scan import scan_bytes

_MAX_CONTEXT_FILE_BYTES = 16 * 1024 * 1024
_ALLOWED_MODES = frozenset({"100644", "100755", "120000"})
_TEXT_SUFFIXES = frozenset(
    {
        ".css",
        ".dockerignore",
        ".html",
        ".js",
        ".json",
        ".jsonl",
        ".lock",
        ".py",
        ".pyi",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
    }
)
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".db",
        ".doc",
        ".docx",
        ".env",
        ".gz",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".whl",
        ".xls",
        ".xlsx",
        ".zip",
    }
)
_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".data",
        "artifacts",
        "backups",
        "evidence",
        "private",
        "secrets",
    }
)


@dataclass(frozen=True, slots=True)
class ReleaseContextPolicy:
    """定义 Dockerfile 真正需要且允许进入上下文的提交路径。"""

    exact_paths: frozenset[str]
    directory_paths: frozenset[str]
    required_exact_paths: frozenset[str]
    required_directory_paths: frozenset[str]
    policy_version: int = 1


DEFAULT_POLICY = ReleaseContextPolicy(
    exact_paths=frozenset(
        {
            "Dockerfile",
            "Dockerfile.dockerignore",
            "compatibility-manifest.json",
            "docs/public/openapi-v1.json",
            "evaluation/__init__.py",
            "evaluation/gates/p08-gates.json",
            "evaluation/p11_pilot.py",
            "evaluation/p11_pilot_data.py",
            "evaluation/p11_pilot_runtime.py",
            "frontend/index.html",
            "frontend/package-lock.json",
            "frontend/package.json",
            "frontend/tsconfig.json",
            "frontend/vite.config.ts",
            "pyproject.toml",
            "requirements.runtime.lock",
        }
    ),
    directory_paths=frozenset(
        {
            "evaluation/datasets/p11-pilot",
            "evaluation/v2",
            "frontend/src",
            "migrations",
            "src",
        }
    ),
    required_exact_paths=frozenset(
        {
            "Dockerfile",
            "Dockerfile.dockerignore",
            "compatibility-manifest.json",
            "docs/public/openapi-v1.json",
            "evaluation/__init__.py",
            "evaluation/gates/p08-gates.json",
            "evaluation/p11_pilot.py",
            "evaluation/p11_pilot_data.py",
            "evaluation/p11_pilot_runtime.py",
            "frontend/index.html",
            "frontend/package-lock.json",
            "frontend/package.json",
            "frontend/tsconfig.json",
            "frontend/vite.config.ts",
            "pyproject.toml",
            "requirements.runtime.lock",
        }
    ),
    required_directory_paths=frozenset(
        {
            "evaluation/datasets/p11-pilot",
            "evaluation/v2",
            "frontend/src",
            "migrations",
            "src",
        }
    ),
)


@dataclass(frozen=True, slots=True)
class _GitEntry:
    mode: str
    object_id: str
    path: str
    content: bytes


def _git(
    repository: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """运行不经过 shell 的 Git 命令。"""
    executable = shutil.which("git")
    if executable is None:
        raise OSError("缺少发布命令：git")
    return subprocess.run(  # noqa: S603
        (executable, *arguments),
        cwd=repository,
        check=check,
        capture_output=True,
    )


def require_clean_committed_head(repository: Path) -> str:
    """验证候选来自当前干净 checkout 的完整提交。

    Args:
        repository: 待构建的 Git 仓库。

    Returns:
        当前 HEAD 的完整提交 SHA。

    Raises:
        OSError: Git 不可用。
        RuntimeError: 工作树不干净或 HEAD 不是提交。

    """
    root = repository.resolve(strict=True)
    revision = _git(root, ("rev-parse", "--verify", "HEAD^{commit}"))
    source_revision = revision.stdout.decode("ascii").strip()
    status = _git(
        root,
        ("status", "--porcelain=v1", "-z", "--untracked-files=normal"),
    )
    if status.stdout:
        raise RuntimeError("RELEASE_CONTEXT_DIRTY_WORKTREE")
    return source_revision


def _policy_paths(policy: ReleaseContextPolicy) -> tuple[str, ...]:
    return tuple(sorted(policy.exact_paths | policy.directory_paths))


def _path_allowed(path: str, policy: ReleaseContextPolicy) -> bool:
    if path in policy.exact_paths:
        return True
    return any(path.startswith(f"{root}/") for root in policy.directory_paths)


def _safe_path(path: str) -> PurePosixPath:
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or "\\" in path
    ):
        raise RuntimeError("RELEASE_CONTEXT_PATH_INVALID")
    return parsed


def _validate_candidate_path(path: str) -> None:
    parsed = _safe_path(path)
    folded = {part.casefold() for part in parsed.parts}
    if folded & _FORBIDDEN_COMPONENTS:
        raise RuntimeError(f"RELEASE_CONTEXT_PRIVATE_PATH:{path}")
    suffix = parsed.suffix.casefold()
    if suffix in _FORBIDDEN_SUFFIXES or parsed.name.casefold().startswith(
        ".env"
    ):
        raise RuntimeError(f"RELEASE_CONTEXT_FORBIDDEN_FILE:{path}")
    if suffix and suffix not in _TEXT_SUFFIXES:
        raise RuntimeError(f"RELEASE_CONTEXT_UNAPPROVED_TYPE:{path}")


def _read_entries(
    repository: Path,
    revision: str,
    policy: ReleaseContextPolicy,
) -> tuple[_GitEntry, ...]:
    listing = _git(
        repository,
        (
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            revision,
            "--",
            *_policy_paths(policy),
        ),
    ).stdout
    entries: list[_GitEntry] = []
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        metadata, encoded_path = raw.split(b"\t", 1)
        mode, object_type, encoded_object_id = metadata.split(b" ", 2)
        path = encoded_path.decode("utf-8")
        git_mode = mode.decode("ascii")
        object_id = encoded_object_id.decode("ascii")
        if object_type != b"blob" or git_mode not in _ALLOWED_MODES:
            raise RuntimeError(f"RELEASE_CONTEXT_GIT_TYPE_INVALID:{path}")
        if not _path_allowed(path, policy):
            raise RuntimeError(f"RELEASE_CONTEXT_POLICY_MISMATCH:{path}")
        _validate_candidate_path(path)
        content = _git(
            repository, ("cat-file", "blob", object_id)
        ).stdout
        if len(content) > _MAX_CONTEXT_FILE_BYTES:
            raise RuntimeError(f"RELEASE_CONTEXT_FILE_TOO_LARGE:{path}")
        findings = scan_bytes(content, location=path)
        if findings:
            rules = ",".join(sorted(item.rule for item in findings))
            raise RuntimeError(f"RELEASE_CONTEXT_SECRET_SHAPE:{path}:{rules}")
        entries.append(_GitEntry(git_mode, object_id, path, content))
    _validate_required_inputs(entries, policy)
    _validate_symlinks(entries)
    return tuple(entries)


def _validate_required_inputs(
    entries: Sequence[_GitEntry], policy: ReleaseContextPolicy
) -> None:
    paths = {entry.path for entry in entries}
    missing_exact = sorted(policy.required_exact_paths - paths)
    missing_directories = sorted(
        root
        for root in policy.required_directory_paths
        if not any(path.startswith(f"{root}/") for path in paths)
    )
    if missing_exact or missing_directories:
        missing = ",".join((*missing_exact, *missing_directories))
        raise RuntimeError(f"RELEASE_CONTEXT_REQUIRED_INPUT_MISSING:{missing}")


def _validate_symlinks(entries: Sequence[_GitEntry]) -> None:
    paths = {entry.path for entry in entries}
    for entry in entries:
        if entry.mode != "120000":
            continue
        try:
            target = entry.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("RELEASE_CONTEXT_SYMLINK_INVALID") from error
        if not target or PurePosixPath(target).is_absolute():
            raise RuntimeError(f"RELEASE_CONTEXT_SYMLINK_OUTSIDE:{entry.path}")
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(entry.path), target)
        )
        outside = resolved.startswith("../") or resolved == ".."
        if outside or resolved not in paths:
            raise RuntimeError(f"RELEASE_CONTEXT_SYMLINK_OUTSIDE:{entry.path}")


def _materialize(entries: Sequence[_GitEntry], destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for entry in entries:
        target = destination.joinpath(*PurePosixPath(entry.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.mode == "120000":
            target.symlink_to(entry.content.decode("utf-8"))
            continue
        target.write_bytes(entry.content)
        target.chmod(0o755 if entry.mode == "100755" else 0o644)


def _manifest(
    revision: str,
    policy: ReleaseContextPolicy,
    entries: Sequence[_GitEntry],
) -> dict[str, object]:
    files = [
        {
            "path": entry.path,
            "git_mode": entry.mode,
            "git_object_id": entry.object_id,
            "bytes": len(entry.content),
            "sha256": hashlib.sha256(entry.content).hexdigest(),
        }
        for entry in entries
    ]
    return {
        "schema_version": 1,
        "policy_version": policy.policy_version,
        "source_revision": revision,
        "dockerfile": "Dockerfile",
        "effective_ignore": "Dockerfile.dockerignore",
        "file_count": len(files),
        "total_bytes": sum(len(entry.content) for entry in entries),
        "files": files,
    }


def export_release_context(
    repository: Path,
    revision: str,
    destination: Path,
    manifest_path: Path,
    *,
    policy: ReleaseContextPolicy = DEFAULT_POLICY,
) -> dict[str, object]:
    """从指定提交导出受控上下文并写出确定性 manifest。

    Args:
        repository: 源 Git 仓库。
        revision: 必须使用完整规范 SHA 的提交。
        destination: 仓库外且尚不存在的上下文目录。
        manifest_path: 确定性 manifest 输出路径。
        policy: Dockerfile 输入白名单与必需项。

    Returns:
        manifest 内容以及 manifest 自身 SHA-256。

    Raises:
        OSError: Git 或文件系统操作失败。
        RuntimeError: 提交、路径、类型、Secret 或必需输入违反合同。

    """
    root = repository.resolve(strict=True)
    commit = _git(
        root, ("rev-parse", "--verify", f"{revision}^{{commit}}")
    ).stdout.decode("ascii").strip()
    if commit != revision:
        raise RuntimeError("RELEASE_CONTEXT_REVISION_NOT_CANONICAL")
    target = destination.resolve()
    if target == root or target.is_relative_to(root):
        raise RuntimeError("RELEASE_CONTEXT_DESTINATION_INSIDE_REPOSITORY")
    entries = _read_entries(root, commit, policy)
    _materialize(entries, target)
    manifest = _manifest(commit, policy, entries)
    manifest_content = (
        json.dumps(
            manifest, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_content)
    digest = hashlib.sha256(manifest_content).hexdigest()
    manifest_path.with_suffix(f"{manifest_path.suffix}.sha256").write_text(
        f"{digest}  {manifest_path.name}\n", encoding="ascii"
    )
    return {**manifest, "manifest_sha256": digest}


def executable_mode(path: Path) -> bool:
    """供回归测试确认导出文件保留 Git 可执行位。

    Args:
        path: 已导出的普通文件。

    Returns:
        文件具有用户可执行位时为 `True`。

    """
    return bool(path.stat().st_mode & stat.S_IXUSR)
