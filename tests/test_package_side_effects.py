from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_REVISION = "1" * 40
_QDRANT_REGISTRY_DIGEST = (
    "sha256:"
    "0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286"
)
_QDRANT_IMAGE_ID = "sha256:" + "7" * 64
_QDRANT_REPO_DIGEST = f"qdrant/qdrant@{_QDRANT_REGISTRY_DIGEST}"


@dataclass(frozen=True)
class _PackageSandbox:
    repository: Path
    package: Path
    docker_log: Path
    binaries: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_sandbox(tmp_path: Path) -> _PackageSandbox:
    repository = tmp_path / "repository"
    deployment = repository / "deployment"
    deployment.mkdir(parents=True)
    source = Path(__file__).parents[1] / "deployment/package.sh"
    package = deployment / "package.sh"
    shutil.copyfile(source, package)
    package.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        binaries / "git",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"rev-parse HEAD"* ]]; then
  printf '%s\n' "{_REVISION}"
elif [[ "$*" == *"status --porcelain"* ]]; then
  exit 0
else
  exit 2
fi
""",
    )
    _write_executable(
        binaries / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${{FAKE_DOCKER_LOG}}"
if [[ "$1 $2" == "sbom --help" ]]; then
  [[ "${{FAKE_SBOM_AVAILABLE:-0}}" == "1" ]]
  exit
fi
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$*" == *".Architecture"* ]]; then
    if [[ "${{FAKE_BAD_INSPECT:-0}}" == "1" ]]; then
      echo arm64
    else
      echo amd64
    fi
  elif [[ "$*" == *".Os"* ]]; then
    echo linux
  elif [[ "$*" == *".Id"* ]]; then
    image="${{@: -1}}"
    if [[ "${{image}}" == *"qdrant/qdrant"* ]]; then
      echo "{_QDRANT_IMAGE_ID}"
    else
      printf 'sha256:%064d\n' 2
    fi
  elif [[ "$*" == *".RepoDigests"* ]]; then
    if [[ "${{FAKE_BAD_REPO_DIGEST:-0}}" == "1" ]]; then
      echo "qdrant/qdrant@sha256:$(printf '%064d' 9)"
    else
      echo "{_QDRANT_REPO_DIGEST}"
    fi
  elif [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
    printf '%s\n' "{_REVISION}"
  else
    echo "[]"
  fi
  exit 0
fi
if [[ "$1 $2" == "image tag" ]]; then
  exit 0
fi
if [[ "$1 $2" == "image save" ]]; then
  exit 0
fi
exit 3
""",
    )
    return _PackageSandbox(
        repository=repository,
        package=package,
        docker_log=docker_log,
        binaries=binaries,
    )


def _run_package(
    sandbox: _PackageSandbox,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
            "FAKE_DOCKER_LOG": str(sandbox.docker_log),
        }
    )
    environment.update(overrides)
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(sandbox.package)],
        cwd=sandbox.repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_sbom_failure_precedes_every_release_side_effect(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(sandbox)

    assert completed.returncode != 0
    calls = sandbox.docker_log.read_text(encoding="utf-8").splitlines()
    assert "sbom --help" in calls
    assert not [call for call in calls if call.startswith("image tag ")]
    assert not [call for call in calls if call.startswith("image save ")]
    assert not (sandbox.repository / "artifacts").exists()


def test_sbom_success_is_still_before_tag_and_save(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(sandbox, FAKE_SBOM_AVAILABLE="1")

    assert completed.returncode != 0
    calls = sandbox.docker_log.read_text(encoding="utf-8").splitlines()
    preflight = calls.index("sbom --help")
    tag = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("image tag ")
    )
    assert preflight < tag
    assert not [
        call
        for call in calls[:preflight]
        if call.startswith("image save ")
    ]


def test_bad_image_inspect_fails_before_preflight_or_output(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(sandbox, FAKE_BAD_INSPECT="1")

    assert completed.returncode != 0
    calls = sandbox.docker_log.read_text(encoding="utf-8").splitlines()
    assert "sbom --help" not in calls
    assert not [call for call in calls if call.startswith("image tag ")]
    assert not (sandbox.repository / "artifacts").exists()


def test_wrong_qdrant_repo_digest_fails_before_release_side_effect(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(
        sandbox,
        FAKE_BAD_REPO_DIGEST="1",
        FAKE_SBOM_AVAILABLE="1",
    )

    assert completed.returncode != 0
    calls = sandbox.docker_log.read_text(encoding="utf-8").splitlines()
    assert [call for call in calls if ".RepoDigests" in call]
    assert not [call for call in calls if call.startswith("image tag ")]
    assert not (sandbox.repository / "artifacts").exists()


def test_source_places_sbom_preflight_before_formal_output_code() -> None:
    package = (
        Path(__file__).parents[1] / "deployment/package.sh"
    ).read_text(encoding="utf-8")

    preflight = package.index("docker sbom --help")
    for side_effect in (
        "docker image tag",
        "docker image save",
        'mkdir -p "${artifact_root}"',
        'tar --format=posix -C "${stage}"',
        'write_sidecar "${runtime_archive}"',
    ):
        assert preflight < package.index(side_effect)
