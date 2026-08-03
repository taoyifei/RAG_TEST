from __future__ import annotations

import hashlib
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
    corpus_manifest: Path
    docker_log: Path
    binaries: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_sandbox(tmp_path: Path) -> _PackageSandbox:
    repository = tmp_path / "repository"
    deployment = repository / "deployment"
    deployment.mkdir(parents=True)
    ocr_assets = deployment / "ocr/assets"
    ocr_assets.mkdir(parents=True)
    empty_sha = (
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    )
    (ocr_assets / "asset.bin").write_bytes(b"")
    (ocr_assets / "model.bin").write_bytes(b"")
    (ocr_assets / "MANIFEST.sha256").write_text(
        f"{empty_sha}  asset.bin\n",
        encoding="ascii",
    )
    (ocr_assets.parent / "MODELS.sha256").write_text(
        f"{empty_sha}  model.bin\n",
        encoding="ascii",
    )
    for relative, content in (
        ("deployment/config/pipeline.json", "{}\n"),
        ("deployment/config/retrieval.json", '{"status":"frozen"}\n'),
        ("deployment/config/corpus-policy.json", "{}\n"),
        ("deployment/config/FREEZE_DECISION.json", "{}\n"),
        ("evaluation/frozen/dataset.json", "{}\n"),
    ):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    dataset = repository / "evaluation/frozen/dataset.json"
    dataset_sha = hashlib.sha256(dataset.read_bytes()).hexdigest()
    (repository / "evaluation/frozen/MANIFEST.sha256").write_text(
        f"{dataset_sha}  dataset.json\n",
        encoding="ascii",
    )
    source = Path(__file__).parents[1] / "deployment/package.sh"
    package = deployment / "package.sh"
    shutil.copyfile(source, package)
    shutil.copyfile(
        Path(__file__).parents[1] / "deployment/qdrant-policy.sh",
        deployment / "qdrant-policy.sh",
    )
    package.chmod(0o755)
    corpus_manifest = repository / "operator-corpus.json"
    corpus_manifest.write_text("{}\n", encoding="utf-8")
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
  exec /usr/bin/python3 "$@"
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
    _write_executable(
        binaries / "python3",
        """#!/usr/bin/env bash
if [[ "$*" == *"freeze_corpus_manifest id"* ]]; then
  echo frozen-corpus
elif [[ "$*" == *"freeze_corpus_manifest verify"* ]]; then
  exit 0
else
  exec /usr/bin/python3 "$@"
fi
""",
    )
    _write_executable(
        binaries / "cp",
        """#!/usr/bin/env bash
set -euo pipefail
source_path="${@: -2:1}"
destination="${@: -1}"
if [[ -d "${destination}" || "${destination}" == */ ]]; then
  destination="${destination}/$(basename "${source_path}")"
fi
mkdir -p "$(dirname "${destination}")"
printf 'placeholder\n' > "${destination}"
""",
    )
    return _PackageSandbox(
        repository=repository,
        package=package,
        corpus_manifest=corpus_manifest,
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
            "CORPUS_MANIFEST": str(sandbox.corpus_manifest),
            "FAKE_DOCKER_LOG": str(sandbox.docker_log),
            "RELEASE_TIER": "production",
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
    ) if any(call.startswith("image tag ") for call in calls) else -1
    assert tag >= 0, completed.stderr
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
        'mkdir -p "${release_parent}"',
        'tar --format=posix -C "${work}"',
        'write_sidecar "${runtime_archive}"',
    ):
        assert preflight < package.index(side_effect)
