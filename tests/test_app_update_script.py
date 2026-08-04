from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_BASE_REVISION = "a" * 40
_TARGET_REVISION = "b" * 40
_BASE_IMAGE_ID = "sha256:" + "a" * 64
_TARGET_IMAGE_ID = "sha256:" + "b" * 64
_OCR_IMAGE_ID = "sha256:" + "c" * 64
_QDRANT_IMAGE_ID = "sha256:" + "d" * 64
_MANIFEST_DIGEST = "sha256:" + "1" * 64
_CONFIG_DIGEST = "sha256:" + "2" * 64
_FINGERPRINT = "sha256:" + "3" * 64


@dataclass(frozen=True)
class _Sandbox:
    root: Path
    script: Path
    update_dir: Path
    active_env: Path
    current_link: Path
    container_state: Path
    command_log: Path
    binaries: Path
    protected_paths: tuple[Path, ...]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_update_manifest(update_dir: Path) -> None:
    names = sorted(
        path.name
        for path in update_dir.iterdir()
        if path.name != "APP_UPDATE_MANIFEST.sha256"
    )
    lines = [f"{_sha256(update_dir / name)}  {name}" for name in names]
    (update_dir / "APP_UPDATE_MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n",
        encoding="ascii",
    )


def _rewrite_metadata(
    sandbox: _Sandbox,
    **changes: object,
) -> None:
    path = sandbox.update_dir / "APP_UPDATE.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_update_manifest(sandbox.update_dir)


def _prepare_sandbox(tmp_path: Path) -> _Sandbox:
    root = tmp_path / "RAG"
    release = root / "releases/base"
    shared_env = root / "shared/env"
    shared_state = root / "shared/app-update"
    state_dir = root / "data/state"
    qdrant_dir = root / "data/qdrant"
    corpus_dir = root / "shared/corpora/corpus/docs"
    for directory in (
        release / "scripts",
        shared_env,
        shared_state,
        state_dir,
        qdrant_dir,
        corpus_dir,
    ):
        directory.mkdir(parents=True)
    current_link = root / "current"
    current_link.symlink_to(release)
    (release / "SOURCE_REVISION").write_text(
        f"{_BASE_REVISION}\n",
        encoding="ascii",
    )
    (release / "RELEASE_METADATA.json").write_text(
        json.dumps(
            {
                "configuration_status": "provisional",
                "release_tier": "smoke",
                "schema_version": "1",
                "source_revision": _BASE_REVISION,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (release / "compose.yaml").write_text(
        "services:\n"
        "  rag-app:\n"
        "    image: '${RAG_APP_IMAGE:?required}'\n",
        encoding="utf-8",
    )
    _write_executable(
        release / "verify-offline.sh",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        release / "scripts/docker_archive_identity.py",
        f"""#!/usr/bin/env python3
import os
import sys

expected = sys.argv[sys.argv.index("--expected-revision") + 1]
actual = os.environ.get("FAKE_ARCHIVE_REVISION", expected)
if actual != expected:
    raise SystemExit(1)
print("{_MANIFEST_DIGEST}\\t{_CONFIG_DIGEST}\\tlinux/amd64")
""",
    )
    _write_executable(
        release / "scripts/docker_archive_loaded_identity.py",
        "#!/usr/bin/env python3\n"
        "import os\n"
        "raise SystemExit(1 if "
        "os.environ.get('FAKE_LOADED_IDENTITY_FAIL') == '1' else 0)\n",
    )
    source = _ROOT / "deployment/app-update.sh"
    script = release / "app-update.sh"
    script.write_text(
        source.read_text(encoding="utf-8").replace(
            'project_root="/data/tyf/RAG"',
            f'project_root="{root}"',
            1,
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)
    active_env = shared_env / "rag.env"
    active_env.write_text(
        f"RAG_APP_IMAGE=docx-rag:base\n"
        f"RAG_OCR_IMAGE=docx-rag-ocr:base\n"
        f"RAG_QDRANT_IMAGE=rag-qdrant:base\n"
        f"RAG_RELEASE_REVISION={_BASE_REVISION}\n"
        "RAG_PORT=8088\n"
        "RAG_QUERY_TOKEN=protected-value\n",
        encoding="utf-8",
    )
    active_env.chmod(0o600)
    rollback_state = shared_env / "rollback-images.env"
    rollback_state.write_text("protected rollback state\n", encoding="utf-8")
    rollback_state.chmod(0o600)
    protected_paths = (
        active_env,
        rollback_state,
        state_dir / "state.sqlite3",
        qdrant_dir / "storage.bin",
        corpus_dir / "source.docx",
    )
    for path in protected_paths[1:]:
        path.write_bytes(f"protected:{path.name}".encode())

    update_dir = tmp_path / "update"
    update_dir.mkdir()
    archive_name = f"docx-rag-app-{_TARGET_REVISION[:12]}.tar.gz"
    archive = update_dir / archive_name
    with gzip.open(archive, mode="wb") as output:
        output.write(b"fake-docker-archive")
    (update_dir / f"{archive_name}.sha256").write_text(
        f"{_sha256(archive)}  {archive_name}\n",
        encoding="ascii",
    )
    (update_dir / "APP_UPDATE.json").write_text(
        json.dumps(
            {
                "archive": archive_name,
                "base_revision": _BASE_REVISION,
                "change_categories": ["app_python"],
                "changed_path_count": 1,
                "config_digest": _CONFIG_DIGEST,
                "image_tag": f"docx-rag:{_TARGET_REVISION[:12]}",
                "index_fingerprint": {
                    "base": _FINGERPRINT,
                    "target": _FINGERPRINT,
                },
                "manifest_digest": _MANIFEST_DIGEST,
                "platform": "linux/amd64",
                "reindex_required": False,
                "schema_version": "1",
                "serving_fingerprint": {
                    "base": _FINGERPRINT,
                    "target": "sha256:" + "4" * 64,
                },
                "target_revision": _TARGET_REVISION,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_update_manifest(update_dir)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    command_log = tmp_path / "commands.log"
    container_state = tmp_path / "containers.env"
    container_state.write_text(
        f"APP_IMAGE={_BASE_IMAGE_ID}\n"
        "APP_RUNNING=true\n"
        f"OCR_IMAGE={_OCR_IMAGE_ID}\n"
        "OCR_RUNNING=true\n"
        f"QDRANT_IMAGE={_QDRANT_IMAGE_ID}\n"
        "QDRANT_RUNNING=true\n"
        "WORKER_EXISTS=false\n"
        "WORKER_RUNNING=false\n",
        encoding="ascii",
    )
    _install_fake_docker(binaries / "docker")
    _write_executable(
        binaries / "curl",
        "#!/usr/bin/env bash\n"
        "printf 'curl %s\\n' \"$*\" >> \"${FAKE_COMMAND_LOG}\"\n"
        "exit 0\n",
    )
    return _Sandbox(
        root=root,
        script=script,
        update_dir=update_dir,
        active_env=active_env,
        current_link=current_link,
        container_state=container_state,
        command_log=command_log,
        binaries=binaries,
        protected_paths=protected_paths,
    )


def _install_fake_docker(path: Path) -> None:
    _write_executable(
        path,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >> "${{FAKE_COMMAND_LOG}}"
source "${{FAKE_CONTAINER_STATE}}"
write_state() {{
  {{
    printf 'APP_IMAGE=%s\n' "${{APP_IMAGE}}"
    printf 'APP_RUNNING=%s\n' "${{APP_RUNNING}}"
    printf 'OCR_IMAGE=%s\n' "${{OCR_IMAGE}}"
    printf 'OCR_RUNNING=%s\n' "${{OCR_RUNNING}}"
    printf 'QDRANT_IMAGE=%s\n' "${{QDRANT_IMAGE}}"
    printf 'QDRANT_RUNNING=%s\n' "${{QDRANT_RUNNING}}"
    printf 'WORKER_EXISTS=%s\n' "${{WORKER_EXISTS}}"
    printf 'WORKER_RUNNING=%s\n' "${{WORKER_RUNNING}}"
  }} > "${{FAKE_CONTAINER_STATE}}"
}}
if [[ "$1 $2" == "container inspect" ]]; then
  container="${{@: -1}}"
  if [[ "${{container}}" == "rag-worker" ]]; then
    [[ "${{WORKER_EXISTS}}" == "true" ]] || exit 1
    if [[ "$*" == *".State.Running"* ]]; then
      echo "${{WORKER_RUNNING}}"
    fi
    exit 0
  fi
  if [[ "${{container}}" == "rag-ocr" ]]; then
    if [[ "$*" == *".State.Running"* ]]; then
      echo "${{OCR_RUNNING}}"
    elif [[ "$*" == *".Image"* ]]; then
      echo "${{OCR_IMAGE}}"
    fi
    exit 0
  fi
  if [[ "${{container}}" == "rag-qdrant" ]]; then
    if [[ "$*" == *".State.Running"* ]]; then
      echo "${{QDRANT_RUNNING}}"
    elif [[ "$*" == *".Image"* ]]; then
      echo "${{QDRANT_IMAGE}}"
    fi
    exit 0
  fi
  [[ "${{container}}" == "rag-app" ]] || exit 91
  if [[ "$*" == *".State.Running"* ]]; then
    echo "${{APP_RUNNING}}"
  elif [[ "$*" == *".State.Health.Status"* ]]; then
    if [[ "${{APP_IMAGE}}" == "{_TARGET_IMAGE_ID}" \
      && "${{FAKE_TARGET_UNHEALTHY:-0}}" == "1" ]]; then
      echo unhealthy
    else
      echo healthy
    fi
  elif [[ "$*" == *".Image"* ]]; then
    echo "${{APP_IMAGE}}"
  fi
  exit 0
fi
if [[ "$1 $2" == "image inspect" ]]; then
  image="${{@: -1}}"
  if [[ "$*" == *"--format"* ]]; then
    case "${{image}}" in
      docx-rag:base|{_BASE_IMAGE_ID}) echo "{_BASE_IMAGE_ID}" ;;
      docx-rag:{_TARGET_REVISION[:12]}|{_TARGET_IMAGE_ID})
        echo "{_TARGET_IMAGE_ID}" ;;
      *) exit 92 ;;
    esac
  else
    echo '[]'
  fi
  exit 0
fi
if [[ "$1" == "load" ]]; then
  if [[ "${{FAKE_LOAD_FAIL:-0}}" == "1" ]]; then
    exit 94
  fi
  exit 0
fi
if [[ "$1" == "compose" ]]; then
  if [[ "$*" == *" config"* || "$*" == *" ps"* ]]; then
    exit 0
  fi
  if [[ "$*" == *" up "* && "${{@: -1}}" == "rag-app" ]]; then
    if [[ "$*" == *"app-update.override.yaml"* ]]; then
      APP_IMAGE="{_TARGET_IMAGE_ID}"
    else
      APP_IMAGE="{_BASE_IMAGE_ID}"
    fi
    APP_RUNNING=true
    write_state
    exit 0
  fi
fi
exit 93
""",
    )


def _run(
    sandbox: _Sandbox,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_COMMAND_LOG": str(sandbox.command_log),
            "FAKE_CONTAINER_STATE": str(sandbox.container_state),
            "PATH": f"{sandbox.binaries}:{environment['PATH']}",
        }
    )
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(  # noqa: S603
        ["/usr/bin/bash", str(sandbox.script), *arguments],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=20,
    )


def _state_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", maxsplit=1)[1]
    raise AssertionError(f"missing state key: {key}")


def test_full_runtime_carries_executable_app_update_tool() -> None:
    package = (_ROOT / "deployment/package.sh").read_text(encoding="utf-8")
    verifier = (_ROOT / "deployment/verify-offline.sh").read_text(
        encoding="utf-8"
    )

    assert 'deployment/app-update.sh' in package
    assert '"${runtime_root}/app-update.sh"' in package
    assert 'chmod 0700 "${runtime_root}/app-update.sh"' in package
    assert '"app-update.sh"' in verifier
    assert '[[ ! -x app-update.sh ]]' in verifier


def test_apply_updates_only_app_and_preserves_protected_state(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    before = {path: _sha256(path) for path in sandbox.protected_paths}
    current_before = sandbox.current_link.resolve()

    completed = _run(sandbox, "apply", str(sandbox.update_dir))

    assert completed.returncode == 0, completed.stderr
    assert _state_value(sandbox.container_state, "APP_IMAGE") == (
        _TARGET_IMAGE_ID
    )
    assert _state_value(sandbox.container_state, "OCR_IMAGE") == (
        _OCR_IMAGE_ID
    )
    assert _state_value(sandbox.container_state, "OCR_RUNNING") == "true"
    assert _state_value(sandbox.container_state, "QDRANT_IMAGE") == (
        _QDRANT_IMAGE_ID
    )
    assert _state_value(sandbox.container_state, "QDRANT_RUNNING") == "true"
    assert {path: _sha256(path) for path in sandbox.protected_paths} == before
    assert sandbox.current_link.resolve() == current_before
    command_log = sandbox.command_log.read_text(encoding="utf-8")
    compose_up = [
        line for line in command_log.splitlines() if " compose " in line
        and " up " in line
    ]
    assert len(compose_up) == 1
    assert compose_up[0].endswith("rag-app")
    assert "--no-deps --no-build --pull never rag-app" in compose_up[0]
    assert "rag-ocr" not in compose_up[0]
    assert "rag-qdrant" not in compose_up[0]


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("base", "base revision"),
        ("sha", "SHA256"),
        ("platform", "platform"),
        ("revision", "OCI"),
    ),
)
def test_apply_rejects_invalid_update_identity(
    tmp_path: Path,
    kind: str,
    expected: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    environment: dict[str, str] = {}
    if kind == "base":
        _rewrite_metadata(sandbox, base_revision="e" * 40)
    elif kind == "sha":
        archive = next(sandbox.update_dir.glob("*.tar.gz"))
        archive.write_bytes(archive.read_bytes() + b"tampered")
    elif kind == "platform":
        _rewrite_metadata(sandbox, platform="linux/arm64")
    else:
        environment["FAKE_ARCHIVE_REVISION"] = "e" * 40

    completed = _run(
        sandbox,
        "apply",
        str(sandbox.update_dir),
        extra_env=environment,
    )

    assert completed.returncode != 0
    assert expected.casefold() in completed.stderr.casefold()
    assert not (sandbox.root / "shared/app-update/state.json").exists()


def test_running_worker_blocks_apply_before_image_load(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    text = sandbox.container_state.read_text(encoding="ascii")
    sandbox.container_state.write_text(
        text.replace("WORKER_EXISTS=false", "WORKER_EXISTS=true").replace(
            "WORKER_RUNNING=false",
            "WORKER_RUNNING=true",
        ),
        encoding="ascii",
    )

    completed = _run(sandbox, "apply", str(sandbox.update_dir))

    assert completed.returncode != 0
    assert "worker" in completed.stderr.casefold()
    command_log = sandbox.command_log.read_text(encoding="utf-8")
    assert "docker load" not in command_log


def test_failed_app_health_restores_base_image(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run(
        sandbox,
        "apply",
        str(sandbox.update_dir),
        extra_env={"FAKE_TARGET_UNHEALTHY": "1"},
    )

    assert completed.returncode != 0
    assert "APP_UPDATE_FAILED_RECOVERED" in completed.stderr
    assert _state_value(sandbox.container_state, "APP_IMAGE") == (
        _BASE_IMAGE_ID
    )
    assert not (sandbox.root / "shared/app-update/state.json").exists()
    assert not (
        sandbox.root / "shared/app-update/app-update.override.yaml"
    ).exists()


@pytest.mark.parametrize(
    "failure_environment",
    (
        {"FAKE_LOAD_FAIL": "1"},
        {"FAKE_LOADED_IDENTITY_FAIL": "1"},
    ),
)
def test_image_activation_failure_reverifies_base_app(
    tmp_path: Path,
    failure_environment: dict[str, str],
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run(
        sandbox,
        "apply",
        str(sandbox.update_dir),
        extra_env=failure_environment,
    )

    assert completed.returncode != 0
    assert "APP_UPDATE_FAILED_RECOVERED" in completed.stderr
    assert _state_value(sandbox.container_state, "APP_IMAGE") == (
        _BASE_IMAGE_ID
    )
    command_log = sandbox.command_log.read_text(encoding="utf-8")
    base_up = [
        line
        for line in command_log.splitlines()
        if " compose " in line
        and " up " in line
        and "app-update.override.yaml" not in line
    ]
    assert len(base_up) == 1
    assert "curl -fsS" in command_log
    assert not (sandbox.root / "shared/app-update/state.json").exists()
    assert not (
        sandbox.root / "shared/app-update/app-update.override.yaml"
    ).exists()


def test_status_and_rollback_are_idempotent(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    inactive = _run(sandbox, "status")
    assert inactive.returncode == 0
    assert "status=inactive" in inactive.stdout
    assert _run(sandbox, "apply", str(sandbox.update_dir)).returncode == 0

    active = _run(sandbox, "status")
    first = _run(sandbox, "rollback")
    second = _run(sandbox, "rollback")

    assert active.returncode == 0
    assert "status=active" in active.stdout
    assert first.returncode == 0
    assert second.returncode == 0
    assert "status=inactive" in second.stdout
    assert _state_value(sandbox.container_state, "APP_IMAGE") == (
        _BASE_IMAGE_ID
    )


def test_production_rejects_reindex_hot_switch(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    metadata_path = sandbox.root / "releases/base/RELEASE_METADATA.json"
    release_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    release_metadata["release_tier"] = "production"
    metadata_path.write_text(json.dumps(release_metadata) + "\n")
    _rewrite_metadata(
        sandbox,
        index_fingerprint={
            "base": _FINGERPRINT,
            "target": "sha256:" + "5" * 64,
        },
        reindex_required=True,
    )

    completed = _run(sandbox, "apply", str(sandbox.update_dir))

    assert completed.returncode != 0
    assert "reindex_required" in completed.stderr
    assert "docker load" not in sandbox.command_log.read_text(
        encoding="utf-8"
    )


def test_smoke_allows_reindex_update_with_worker_stopped(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    _rewrite_metadata(
        sandbox,
        index_fingerprint={
            "base": _FINGERPRINT,
            "target": "sha256:" + "5" * 64,
        },
        reindex_required=True,
    )

    completed = _run(sandbox, "apply", str(sandbox.update_dir))

    assert completed.returncode == 0, completed.stderr
    assert "reindex_required=true" in completed.stdout
    assert _state_value(sandbox.container_state, "WORKER_RUNNING") == "false"


def test_full_deploy_and_rollback_refuse_active_app_update(
    tmp_path: Path,
) -> None:
    root = tmp_path / "RAG"
    state = root / "shared/app-update/state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}\n", encoding="utf-8")
    shutil_source = _ROOT / "deployment/qdrant-policy.sh"
    (tmp_path / "qdrant-policy.sh").write_bytes(shutil_source.read_bytes())
    for name in ("deploy.sh", "rollback.sh"):
        source = _ROOT / "deployment" / name
        script = tmp_path / name
        script.write_text(
            source.read_text(encoding="utf-8").replace(
                'project_root="/data/tyf/RAG"',
                f'project_root="{root}"',
                1,
            ),
            encoding="utf-8",
        )

        completed = subprocess.run(  # noqa: S603
            ["/usr/bin/bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode != 0
        assert "app-update.sh rollback" in completed.stderr
