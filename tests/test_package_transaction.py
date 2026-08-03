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
        "deployment/qdrant-policy.sh",
        "deployment/verify-offline.sh",
        "scripts/freeze_corpus_manifest.py",
        "scripts/docker_archive_identity.py",
        "scripts/docker_archive_reader.py",
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


def _install_archive_writer(path: Path) -> None:
    path.write_text(
        '''from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path


OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def json_bytes(payload: object) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{text}\\n".encode()


def descriptor(
    content: bytes,
    media_type: str,
    *,
    platform: dict[str, str] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "digest": digest(content),
        "mediaType": media_type,
        "size": len(content),
    }
    if platform is not None:
        value["platform"] = platform
    return value


def blob_path(content_digest: str) -> str:
    algorithm, value = content_digest.split(":", maxsplit=1)
    return f"blobs/{algorithm}/{value}"


def add_member(
    archive: tarfile.TarFile,
    name: str,
    content: bytes,
) -> None:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    member.mtime = 0
    member.size = len(content)
    archive.addfile(member, io.BytesIO(content))


def main() -> None:
    output = Path(sys.argv[1])
    tag = sys.argv[2]
    revision = sys.argv[3]
    layer = f"layer:{tag}\\n".encode()
    layer_digest = digest(layer)
    config = json_bytes(
        {
            "architecture": "amd64",
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": revision,
                }
            },
            "os": "linux",
            "rootfs": {
                "diff_ids": [layer_digest],
                "type": "layers",
            },
        }
    )
    config_digest = digest(config)
    manifest = json_bytes(
        {
            "config": descriptor(config, OCI_CONFIG),
            "layers": [descriptor(layer, OCI_LAYER)],
            "mediaType": OCI_MANIFEST,
            "schemaVersion": 2,
        }
    )
    manifest_digest = digest(manifest)
    manifest_descriptor = descriptor(
        manifest,
        OCI_MANIFEST,
        platform={"architecture": "amd64", "os": "linux"},
    )
    manifest_descriptor["annotations"] = {
        "io.containerd.image.name": f"docker.io/library/{tag}",
        "org.opencontainers.image.ref.name": tag.rsplit(":", maxsplit=1)[1],
    }
    index = json_bytes(
        {
            "manifests": [manifest_descriptor],
            "mediaType": OCI_INDEX,
            "schemaVersion": 2,
        }
    )
    legacy = json_bytes(
        [
            {
                "Config": blob_path(config_digest),
                "Layers": [blob_path(layer_digest)],
                "RepoTags": [tag],
            }
        ]
    )
    members = {
        "index.json": index,
        "manifest.json": legacy,
        "oci-layout": json_bytes({"imageLayoutVersion": "1.0.0"}),
        blob_path(config_digest): config,
        blob_path(layer_digest): layer,
        blob_path(manifest_digest): manifest,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, mode="w") as archive:
        for name in sorted(members):
            add_member(archive, name, members[name])


if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )


def _install_fake_commands(binaries: Path) -> None:
    _install_archive_writer(binaries / "write_docker_archive.py")
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
  output=""
  while (($#)); do
    if [[ "$1" == "--output" ]]; then
      output="$2"
      shift 2
      continue
    fi
    shift
  done
  [[ -n "${{output}}" ]]
  "${{FAKE_REAL_PYTHON}}" "${{FAKE_ARCHIVE_WRITER}}" \
    "${{output}}" "${{image}}" "{_REVISION}"
  exit 0
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
            "FAKE_ARCHIVE_WRITER": str(
                sandbox.binaries / "write_docker_archive.py"
            ),
            "FAKE_COMMAND_LOG": str(sandbox.command_log),
            "FAKE_REAL_PYTHON": sys.executable,
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


def _refresh_runtime_manifest(
    runtime: Path,
    relative_paths: tuple[str, ...],
) -> None:
    manifest_path = runtime / "MANIFEST.sha256"
    replacements = {
        relative_path: hashlib.sha256(
            (runtime / relative_path).read_bytes()
        ).hexdigest()
        for relative_path in relative_paths
    }
    output: list[str] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, relative_path = line.split("  ", maxsplit=1)
        normalized_path = relative_path.removeprefix("./")
        output.append(
            f"{replacements.pop(normalized_path, digest)}  {relative_path}"
        )
    assert not replacements
    manifest_path.write_text("\n".join(output) + "\n", encoding="utf-8")


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
    archive_helpers = {
        "runtime/scripts/docker_archive_identity.py",
        "runtime/scripts/docker_archive_reader.py",
    }
    with tarfile.open(runtime_archive, mode="r:gz") as archive:
        runtime_names = set(archive.getnames())
        manifest_file = archive.extractfile("runtime/MANIFEST.sha256")
        assert manifest_file is not None
        runtime_manifest = manifest_file.read().decode("utf-8")
        image_manifest_file = archive.extractfile(
            "runtime/IMAGE_ARCHIVES.tsv"
        )
        assert image_manifest_file is not None
        image_manifest_rows = [
            line.split("\t")
            for line in image_manifest_file.read().decode().splitlines()
        ]
    assert contract_script in runtime_names
    assert archive_helpers <= runtime_names
    assert (
        "evaluation/runtime/scripts/verify_model_contracts.py"
        in runtime_manifest
    )
    for helper in archive_helpers:
        assert helper.removeprefix("runtime/") in runtime_manifest
    expected_images = (
        (
            "images/docx-rag-linux-amd64.tar",
            f"docx-rag:{_RELEASE_ID}",
            _REVISION,
        ),
        (
            "images/docx-rag-ocr-linux-amd64.tar",
            f"docx-rag-ocr:{_RELEASE_ID}",
            _REVISION,
        ),
        (
            "images/qdrant-linux-amd64.tar",
            f"rag-qdrant:{_RELEASE_ID}",
            f"qdrant/qdrant@sha256:{_QDRANT_DIGEST}",
        ),
    )
    assert len(image_manifest_rows) == len(expected_images)
    for row, expected_image in zip(
        image_manifest_rows,
        expected_images,
        strict=True,
    ):
        assert len(row) == 6
        archive_path, tag, manifest_id, provenance, config_id, platform = row
        assert (archive_path, tag, provenance) == expected_image
        assert platform == "linux/amd64"
        for digest in (manifest_id, config_id):
            assert digest.startswith("sha256:")
            assert len(digest) == 71
        assert manifest_id != config_id
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


def test_runtime_verifier_rejects_rehashed_qdrant_provenance_tamper(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    completed = _run_package(sandbox)
    assert completed.returncode == 0, completed.stderr
    runtime = _extract_runtime(sandbox, tmp_path / "extracted-qdrant")
    fake_digest = "f" * 64
    source_path = runtime / "QDRANT_SOURCE_IMAGE"
    source_path.write_text(
        f"qdrant/qdrant:v1.18.3@sha256:{fake_digest}\n",
        encoding="ascii",
    )
    image_manifest_path = runtime / "IMAGE_ARCHIVES.tsv"
    image_manifest_path.write_text(
        image_manifest_path.read_text(encoding="ascii").replace(
            f"qdrant/qdrant@sha256:{_QDRANT_DIGEST}",
            f"qdrant/qdrant@sha256:{fake_digest}",
        ),
        encoding="ascii",
    )
    _refresh_runtime_manifest(
        runtime,
        ("QDRANT_SOURCE_IMAGE", "IMAGE_ARCHIVES.tsv"),
    )

    rejected = _run_runtime_verifier(runtime)

    assert rejected.returncode != 0
    assert "批准白名单" in rejected.stderr
