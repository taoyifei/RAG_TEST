from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_package_rejects_app_image_from_old_revision(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    deployment = repository / "deployment"
    deployment.mkdir(parents=True)
    source = Path(__file__).parents[1] / "deployment/package.sh"
    package = deployment / "package.sh"
    shutil.copyfile(source, package)
    package.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_executable(
        binaries / "git",
        """#!/usr/bin/env bash
if [[ "$*" == *"rev-parse HEAD"* ]]; then
  printf '%040d\n' 1
elif [[ "$*" == *"status --porcelain"* ]]; then
  exit 0
else
  exit 2
fi
""",
    )
    _write_executable(
        binaries / "docker",
        """#!/usr/bin/env bash
if [[ "$*" == *".Architecture"* ]]; then
  echo amd64
elif [[ "$*" == *".Os"* ]]; then
  echo linux
elif [[ "$*" == *".Id"* ]]; then
  printf 'sha256:%064d\n' 2
elif [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
  printf '%040d\n' 0
else
  exit 2
fi
""",
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{binaries}:/usr/bin:/bin"

    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", str(package)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "revision" in completed.stderr
