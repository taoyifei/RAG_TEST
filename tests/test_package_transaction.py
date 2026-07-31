from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.freeze_corpus_manifest import freeze_corpus_manifest

_REVISION = "1" * 40
_RELEASE_ID = _REVISION[:12]
_CORPUS_ID = "synthetic-corpus"
_QDRANT_DIGEST = (
    "0bd98fa7977f1e75694779359ca4e212"
    "822e5a71334e28421182f72f209d5286"
)


@dataclass(frozen=True)
class _PackageSandbox:
    repository: Path
    package: Path
    manifest: Path
    release: Path
    binaries: Path
    command_log: Path
    tar_log: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_file(root: Path, relative: str, content: str = "fixed\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _prepare_sandbox(tmp_path: Path) -> _PackageSandbox:
    repository = tmp_path / "repository"
    repository.mkdir()
    project = Path(__file__).parents[1]
    for relative in (
        "deployment/package.sh",
        "deployment/install.sh",
        "deployment/verify-offline.sh",
        "scripts/freeze_corpus_manifest.py",
        "scripts/offline_bundle.py",
        "scripts/verify_model_contracts.py",
    ):
        source = project / relative
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    package = repository / "deployment/package.sh"
    package.chmod(0o755)
    for relative in (
        "deployment/compose.yaml",
        "deployment/.env.example",
        "deployment/deploy.sh",
        "deployment/rollback.sh",
        "deployment/backup.sh",
        "deployment/ocr/THIRD_PARTY_NOTICES.md",
        "deployment/ocr/ASSET_SOURCES.json",
        "deployment/ocr/BASE_RUNTIME.json",
        "deployment/ocr/WHEELS.sha256",
        "deployment/ocr/requirements.lock",
        "deployment/ocr/pipeline.yaml",
        "design/public/offline-build-and-server-deployment.md",
        "scripts/load_test_chat.py",
        "scripts/benchmark_qdrant.py",
        "evaluation/evaluate.py",
        "evaluation/metrics.py",
        "evaluation/frozen/dataset.json",
        "evaluation/frozen/MANIFEST.sha256",
    ):
        _write_file(repository, relative)
    license_path = (
        repository
        / "deployment/ocr/assets/licenses/PaddleOCR-LICENSE.txt"
    )
    license_path.parent.mkdir(parents=True)
    license_path.write_text("license\n", encoding="utf-8")
    model = repository / "deployment/ocr/assets/model.bin"
    model.write_bytes(b"model")
    license_sha = hashlib.sha256(license_path.read_bytes()).hexdigest()
    model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
    _write_file(
        repository,
        "deployment/ocr/assets/MANIFEST.sha256",
        f"{license_sha}  licenses/PaddleOCR-LICENSE.txt\n",
    )
    _write_file(
        repository,
        "deployment/ocr/MODELS.sha256",
        f"{model_sha}  model.bin\n",
    )
    docs = repository / "docs"
    _write_file(repository, "docs/group/one.docx", "one")
    _write_file(repository, "docs/group/two.docx", "two")
    manifest = repository / "operator/corpus.json"
    freeze_corpus_manifest(
        docs_root=docs,
        corpus_id=_CORPUS_ID,
        output_path=manifest,
    )
    release = (
        repository
        / f"artifacts/releases/{_RELEASE_ID}-{_CORPUS_ID}"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    command_log = tmp_path / "docker.log"
    tar_log = tmp_path / "tar.log"
    _install_fake_commands(binaries)
    return _PackageSandbox(
        repository=repository,
        package=package,
        manifest=manifest,
        release=release,
        binaries=binaries,
        command_log=command_log,
        tar_log=tar_log,
    )


def _install_fake_commands(binaries: Path) -> None:
    _write_executable(
        binaries / "git",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"rev-parse HEAD"* ]]; then
  echo "{_REVISION}"
elif [[ "$*" == *"status --porcelain --untracked-files=all"* ]]; then
  exit 0
else
  exit 80
fi
""",
    )
    _write_executable(
        binaries / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${{FAKE_COMMAND_LOG}}"
image="${{@: -1}}"
image_id() {{
  case "${{image}}" in
    docx-rag:*) printf 'sha256:%064d\n' 1 ;;
    docx-rag-ocr:*) printf 'sha256:%064d\n' 2 ;;
    qdrant/qdrant:*) printf 'sha256:%064d\n' 3 ;;
    *) printf 'sha256:%064d\n' 4 ;;
  esac
}}
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$*" == *".Architecture"* ]]; then
    echo amd64
  elif [[ "$*" == *".Os"* ]]; then
    echo linux
  elif [[ "$*" == *".Id"* ]]; then
    image_id
  elif [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
    echo "{_REVISION}"
  elif [[ "$*" == *"RepoDigests"* ]]; then
    echo "qdrant/qdrant@sha256:{_QDRANT_DIGEST}"
  else
    echo '[{{}}]'
  fi
  exit 0
fi
if [[ "$1 $2" == "sbom --help" ]]; then
  exit 0
fi
if [[ "$1" == "sbom" ]]; then
  echo '{{"bom":true}}'
  exit 0
fi
if [[ "$1 $2" == "image tag" ]]; then
  exit 0
fi
if [[ "$1" == "run" ]]; then
  echo "nvidia-license"
  exit 0
fi
if [[ "$1 $2" == "image save" ]]; then
  while (($#)); do
    if [[ "$1" == "--output" ]]; then
      printf 'fake-image-archive\n' > "$2"
      exit 0
    fi
    shift
  done
fi
exit 81
""",
    )
    _write_executable(
        binaries / "tar",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_TAR_LOG}"
if [[ "${FAKE_CORPUS_TAR_FAIL:-0}" == "1" \
  && "$*" == *"rag-corpus-"* ]]; then
  exit 82
fi
exec /usr/bin/tar "$@"
""",
    )
    _write_executable(
        binaries / "sha256sum",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_SIDECAR_FAIL:-0}" == "1" \
  && "$*" == rag-corpus-*.tar.gz ]]; then
  exit 83
fi
exec /usr/bin/sha256sum "$@"
""",
    )
    _write_executable(
        binaries / "python3",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{FAKE_RENAME_RACE:-0}}" == "1" \
  && "$*" == *"-m scripts.offline_bundle publish"* ]]; then
  destination="${{@: -1}}"
  mkdir -p "${{destination}}"
  printf 'racer\n' > "${{destination}}/owner.txt"
fi
exec "{sys.executable}" "$@"
""",
    )


def _run_package(
    sandbox: _PackageSandbox,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CORPUS_MANIFEST": str(sandbox.manifest),
            "FAKE_COMMAND_LOG": str(sandbox.command_log),
            "FAKE_TAR_LOG": str(sandbox.tar_log),
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
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


def _extract_runtime(
    sandbox: _PackageSandbox,
    destination: Path,
) -> Path:
    destination.mkdir()
    archive = sandbox.release / f"rag-runtime-{_RELEASE_ID}.tar.gz"
    completed = subprocess.run(  # noqa: S603
        [
            "/usr/bin/tar",
            "-xzf",
            str(archive),
            "-C",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return destination / "runtime"


def _run_runtime_verifier(
    runtime: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(runtime / "verify-offline.sh")],
        cwd=runtime,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "failure",
    ("FAKE_CORPUS_TAR_FAIL", "FAKE_SIDECAR_FAIL"),
)
def test_failure_after_runtime_archive_leaves_no_formal_release(
    tmp_path: Path,
    failure: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(sandbox, **{failure: "1"})

    assert completed.returncode != 0
    assert not sandbox.release.exists()
    assert not tuple(
        sandbox.release.parent.glob(
            f".{_RELEASE_ID}-{_CORPUS_ID}.*"
        )
    )
    calls = sandbox.tar_log.read_text(encoding="utf-8")
    assert "rag-runtime-" in calls
    assert "rag-corpus-" in calls


def test_existing_release_is_rejected_without_overwrite(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    sandbox.release.mkdir(parents=True)
    marker = sandbox.release / "owner.txt"
    marker.write_text("existing", encoding="utf-8")

    completed = _run_package(sandbox)

    assert completed.returncode != 0
    assert marker.read_text(encoding="utf-8") == "existing"
    assert not sandbox.command_log.exists()


def test_atomic_rename_race_preserves_competing_output(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(sandbox, FAKE_RENAME_RACE="1")

    assert completed.returncode != 0
    assert (sandbox.release / "owner.txt").read_text(
        encoding="utf-8"
    ) == "racer\n"
    assert not (sandbox.release / "RELEASE_MANIFEST.sha256").exists()
    assert not tuple(
        sandbox.release.parent.glob(
            f".{_RELEASE_ID}-{_CORPUS_ID}.*"
        )
    )


def test_success_publishes_only_complete_verified_release(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(sandbox)

    assert completed.returncode == 0, completed.stderr
    expected = {
        "RELEASE_MANIFEST.sha256",
        "offline_bundle.py",
        "offline_bundle.py.sha256",
        f"rag-corpus-{_CORPUS_ID}.tar.gz",
        f"rag-corpus-{_CORPUS_ID}.tar.gz.sha256",
        f"rag-runtime-{_RELEASE_ID}.tar.gz",
        f"rag-runtime-{_RELEASE_ID}.tar.gz.sha256",
    }
    assert {path.name for path in sandbox.release.iterdir()} == expected
    verified = subprocess.run(
        ["/usr/bin/sha256sum", "-c", "RELEASE_MANIFEST.sha256"],
        cwd=sandbox.release,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    runtime_archive = (
        sandbox.release / f"rag-runtime-{_RELEASE_ID}.tar.gz"
    )
    contract_script = (
        "runtime/evaluation/runtime/scripts/verify_model_contracts.py"
    )
    with tarfile.open(runtime_archive, mode="r:gz") as archive:
        runtime_names = set(archive.getnames())
        manifest_file = archive.extractfile("runtime/MANIFEST.sha256")
        assert manifest_file is not None
        runtime_manifest = manifest_file.read().decode("utf-8")
    assert contract_script in runtime_names
    assert (
        "evaluation/runtime/scripts/verify_model_contracts.py"
        in runtime_manifest
    )
    for forbidden_prefix in (
        "runtime/.git",
        "runtime/src",
        "runtime/tests",
    ):
        assert not any(
            name == forbidden_prefix
            or name.startswith(f"{forbidden_prefix}/")
            for name in runtime_names
        )
    for forbidden_file in (
        "runtime/.env",
        "runtime/Dockerfile",
        "runtime/pyproject.toml",
        "runtime/rag.env",
    ):
        assert forbidden_file not in runtime_names
    assert not any(name.endswith(".docx") for name in runtime_names)
    assert not any("/tokenizers/" in name for name in runtime_names)
    corpus_archive = (
        sandbox.release / f"rag-corpus-{_CORPUS_ID}.tar.gz"
    )
    with tarfile.open(corpus_archive, mode="r:gz") as archive:
        names = set(archive.getnames())
    assert "corpus/CORPUS_MANIFEST.json" in names
    assert "corpus/docs/group/one.docx" in names
    assert "corpus/docs/group/two.docx" in names
    assert not any(
        name.endswith("verify_model_contracts.py") for name in names
    )
    assert not tuple(
        sandbox.release.parent.glob(
            f".{_RELEASE_ID}-{_CORPUS_ID}.*"
        )
    )


@pytest.mark.parametrize("tamper", ("delete", "replace"))
def test_runtime_verifier_detects_model_contract_script_tamper(
    tmp_path: Path,
    tamper: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    completed = _run_package(sandbox)
    assert completed.returncode == 0, completed.stderr
    runtime = _extract_runtime(sandbox, tmp_path / "extracted")
    contract_script = (
        runtime
        / "evaluation/runtime/scripts/verify_model_contracts.py"
    )

    verified = _run_runtime_verifier(runtime)
    assert verified.returncode == 0, verified.stderr
    if tamper == "delete":
        contract_script.unlink()
    else:
        contract_script.write_text("tampered\n", encoding="utf-8")

    rejected = _run_runtime_verifier(runtime)

    assert rejected.returncode != 0
