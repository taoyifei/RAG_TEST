"""发布构建上下文只来自显式允许的不可变 Git 对象。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.release_context import (
    ReleaseContextPolicy,
    executable_mode,
    export_release_context,
    require_clean_committed_head,
)

_GIT = shutil.which("git")


def _run(repository: Path, *arguments: str) -> bytes:
    assert _GIT is not None
    return subprocess.run(  # noqa: S603
        (_GIT, *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    _run(repository, "init", "--quiet")
    _run(repository, "config", "user.name", "Context Test")
    _run(repository, "config", "user.email", "context@example.invalid")
    return repository


def _commit(repository: Path) -> str:
    _run(repository, "add", "--all")
    _run(repository, "commit", "--quiet", "-m", "fixture")
    return _run(repository, "rev-parse", "HEAD").decode("ascii").strip()


def _policy(*, required: frozenset[str] | None = None) -> ReleaseContextPolicy:
    return ReleaseContextPolicy(
        exact_paths=frozenset({"Dockerfile", "可 执行.py"}),
        directory_paths=frozenset({"app"}),
        required_exact_paths=required or frozenset({"Dockerfile"}),
        required_directory_paths=frozenset({"app"}),
    )


def test_export_uses_git_blobs_and_preserves_names_modes_and_manifest(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repository / "app").mkdir()
    (repository / "app/正常 文件.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    executable = repository / "可 执行.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    revision = _commit(repository)
    destination = tmp_path / "context"
    manifest_path = tmp_path / "evidence/context-manifest.json"

    result = export_release_context(
        repository,
        revision,
        destination,
        manifest_path,
        policy=_policy(),
    )

    assert (destination / "app/正常 文件.py").read_text(encoding="utf-8") == (
        "VALUE = 1\n"
    )
    assert executable_mode(destination / "可 执行.py")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_revision"] == revision == result["source_revision"]
    assert [item["path"] for item in manifest["files"]] == [
        "Dockerfile",
        "app/正常 文件.py",
        "可 执行.py",
    ]
    assert result["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()


def test_export_does_not_read_ignored_restricted_sentinel(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("EACCES 门禁必须由非特权身份执行")
    repository = _repository(tmp_path)
    (repository / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
    (repository / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repository / "app").mkdir()
    (repository / "app/normal.py").write_text("SAFE = True\n", encoding="utf-8")
    revision = _commit(repository)
    restricted = repository / "artifacts/backup-trust"
    restricted.mkdir(parents=True)
    sentinel = restricted / "do-not-export.txt"
    sentinel.write_text("SENSITIVE_SENTINEL", encoding="utf-8")
    restricted.chmod(0)
    try:
        destination = tmp_path / "context"
        export_release_context(
            repository,
            revision,
            destination,
            tmp_path / "manifest.json",
            policy=_policy(),
        )
        assert restricted.stat().st_mode & 0o777 == 0
        assert not (destination / "artifacts").exists()
        assert not any(
            b"SENSITIVE_SENTINEL" in path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        )
    finally:
        restricted.chmod(0o700)


def test_missing_required_input_fails_without_workspace_fallback(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repository / "app").mkdir()
    (repository / "app/normal.py").write_text("SAFE = True\n", encoding="utf-8")
    revision = _commit(repository)

    with pytest.raises(RuntimeError, match="REQUIRED_INPUT_MISSING"):
        export_release_context(
            repository,
            revision,
            tmp_path / "context",
            tmp_path / "manifest.json",
            policy=_policy(required=frozenset({"Dockerfile", "可 执行.py"})),
        )


def test_secret_shape_inside_allowed_path_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repository / "app").mkdir()
    (repository / "app/normal.py").write_text(
        'TOKEN = "ghp_' + ("A" * 30) + '"\n', encoding="utf-8"
    )
    revision = _commit(repository)

    with pytest.raises(RuntimeError, match="SECRET_SHAPE"):
        export_release_context(
            repository,
            revision,
            tmp_path / "context",
            tmp_path / "manifest.json",
            policy=_policy(),
        )


def test_outside_symlink_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repository / "app").mkdir()
    (repository / "app/escape.py").symlink_to("../../outside.py")
    revision = _commit(repository)

    with pytest.raises(RuntimeError, match="SYMLINK_OUTSIDE"):
        export_release_context(
            repository,
            revision,
            tmp_path / "context",
            tmp_path / "manifest.json",
            policy=_policy(),
        )


def test_dirty_checkout_cannot_claim_committed_release_identity(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    _commit(repository)
    (repository / "Dockerfile").write_text(
        "FROM scratch\n# changed\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="DIRTY_WORKTREE"):
        require_clean_committed_head(repository)
