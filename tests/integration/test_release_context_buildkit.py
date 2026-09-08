"""真实 BuildKit 使用仓库外白名单上下文且不接触受限目录。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from scripts.release_context import ReleaseContextPolicy, export_release_context

pytestmark = pytest.mark.local_integration


def _run(repository: Path, *arguments: str) -> bytes:
    git = shutil.which("git")
    assert git is not None
    return subprocess.run(  # noqa: S603
        (git, *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout


def test_buildkit_image_excludes_restricted_sentinel(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("真实 EACCES 验证必须由非特权身份执行")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker 不可用")
    repository = tmp_path / "source"
    repository.mkdir()
    _run(repository, "init", "--quiet")
    _run(repository, "config", "user.name", "BuildKit Test")
    _run(repository, "config", "user.email", "buildkit@example.invalid")
    (repository / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
    (repository / "Dockerfile").write_text(
        "FROM scratch\nCOPY app/ /app/\n", encoding="utf-8"
    )
    (repository / "app").mkdir()
    (repository / "app/normal.py").write_text(
        "PUBLIC = True\n", encoding="utf-8"
    )
    _run(repository, "add", "--all")
    _run(repository, "commit", "--quiet", "-m", "fixture")
    revision = _run(repository, "rev-parse", "HEAD").decode().strip()
    restricted = repository / "artifacts/backup-trust"
    restricted.mkdir(parents=True)
    (restricted / "sentinel.txt").write_text(
        "SENSITIVE_SENTINEL", encoding="utf-8"
    )
    restricted.chmod(0)
    context = tmp_path / "context"
    image = f"rag-release-context-test:{os.getpid()}-{time.time_ns()}"
    container = ""
    try:
        export_release_context(
            repository,
            revision,
            context,
            tmp_path / "context-manifest.json",
            policy=ReleaseContextPolicy(
                exact_paths=frozenset({"Dockerfile"}),
                directory_paths=frozenset({"app"}),
                required_exact_paths=frozenset({"Dockerfile"}),
                required_directory_paths=frozenset({"app"}),
            ),
        )
        subprocess.run(  # noqa: S603
            (docker, "build", "--tag", image, str(context)),
            check=True,
            capture_output=True,
        )
        container = subprocess.run(  # noqa: S603
            (docker, "create", "--entrypoint", "/not-executed", image),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        archive = tmp_path / "image.tar"
        subprocess.run(  # noqa: S603
            (docker, "export", "--output", str(archive), container), check=True
        )
        with tarfile.open(archive) as payload:
            names = {name.lstrip("./") for name in payload.getnames()}
            assert "app/normal.py" in names
            assert not any("sentinel" in name for name in names)
        assert restricted.stat().st_mode & 0o777 == 0
    finally:
        if container:
            subprocess.run(  # noqa: S603
                (docker, "rm", container),
                check=False,
                capture_output=True,
            )
        subprocess.run(  # noqa: S603
            (docker, "image", "rm", image),
            check=False,
            capture_output=True,
        )
        restricted.chmod(0o700)
