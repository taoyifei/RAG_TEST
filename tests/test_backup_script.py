from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


def _backup_script() -> str:
    root = Path(__file__).parents[1]
    return (root / "deployment/backup.sh").read_text(encoding="utf-8")


@dataclass(frozen=True)
class _BackupSandbox:
    root: Path
    script: Path
    env_file: Path
    backups: Path
    state_file: Path
    command_log: Path
    binaries: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_sandbox(tmp_path: Path) -> _BackupSandbox:
    root = tmp_path / "RAG"
    env_dir = root / "shared/env"
    data = root / "data"
    state = data / "state"
    qdrant = data / "qdrant"
    backups = root / "backups"
    releases = root / "releases"
    active_release = releases / "release-a"
    for directory in (
        env_dir,
        state,
        qdrant,
        backups,
        active_release,
    ):
        directory.mkdir(parents=True)
    (state / "state.sqlite3").write_bytes(b"sqlite-state")
    (qdrant / "storage.bin").write_bytes(b"qdrant-state")
    env_file = env_dir / "rag.env"
    env_file.write_text("RAG_PORT=8088\nTOKEN=preserve\n", encoding="utf-8")
    env_file.chmod(0o600)
    (active_release / "compose.yaml").write_text(
        "services:\n"
        "  rag-app: {image: app}\n"
        "  rag-worker: {image: app, profiles: [index]}\n"
        "  rag-qdrant: {image: qdrant}\n",
        encoding="utf-8",
    )
    (root / "current").symlink_to(active_release)
    script = tmp_path / "backup.sh"
    script.write_text(
        _backup_script().replace(
            'project_root="/data/tyf/RAG"',
            f'project_root="{root}"',
            1,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    state_file = tmp_path / "containers.env"
    state_file.write_text(
        "APP_RUNNING=true\n"
        "WORKER_RUNNING=false\n"
        "QDRANT_RUNNING=true\n",
        encoding="ascii",
    )
    command_log = tmp_path / "commands.log"
    _write_executable(
        binaries / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
source "${FAKE_CONTAINER_STATE}"
write_state() {
  {
    printf 'APP_RUNNING=%s\n' "${APP_RUNNING}"
    printf 'WORKER_RUNNING=%s\n' "${WORKER_RUNNING}"
    printf 'QDRANT_RUNNING=%s\n' "${QDRANT_RUNNING}"
  } > "${FAKE_CONTAINER_STATE}"
}
if [[ "$1 $2" == "container inspect" ]]; then
  case "${@: -1}" in
    rag-app) echo "${APP_RUNNING}" ;;
    rag-worker) echo "${WORKER_RUNNING}" ;;
    rag-qdrant) echo "${QDRANT_RUNNING}" ;;
    *) exit 41 ;;
  esac
  exit 0
fi
if [[ "$1" == "compose" && "$*" == *" stop "* ]]; then
  if [[ "${FAKE_STOP_FAIL:-0}" == "1" ]]; then
    exit 42
  fi
  APP_RUNNING=false
  WORKER_RUNNING=false
  QDRANT_RUNNING=false
  if [[ "${FAKE_STOP_STUCK:-}" == "rag-app" ]]; then APP_RUNNING=true; fi
  if [[ "${FAKE_STOP_STUCK:-}" == "rag-worker" ]]; then WORKER_RUNNING=true; fi
  if [[ "${FAKE_STOP_STUCK:-}" == "rag-qdrant" ]]; then QDRANT_RUNNING=true; fi
  write_state
  exit 0
fi
if [[ "$1" == "compose" && "$*" == *" up -d "* ]]; then
  service="${@: -1}"
  if [[ "${FAKE_RESTORE_FAIL_SERVICE:-}" == "${service}" ]]; then
    exit 43
  fi
  case "${service}" in
    rag-app) APP_RUNNING=true ;;
    rag-worker) WORKER_RUNNING=true ;;
    rag-qdrant) QDRANT_RUNNING=true ;;
    *) exit 44 ;;
  esac
  write_state
  exit 0
fi
exit 45
""",
    )
    _write_executable(
        binaries / "sudo",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'sudo %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
"$@"
""",
    )
    _write_executable(
        binaries / "tar",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'tar %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
if [[ "${FAKE_TAR_LIST_FAIL:-0}" == "1" && "$1" == "-tzf" ]]; then
  exit 51
fi
exec /usr/bin/tar "$@"
""",
    )
    _write_executable(
        binaries / "gzip",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'gzip %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
if [[ "$#" == "0" && "${FAKE_EMPTY_ARCHIVE:-0}" == "1" ]]; then
  exit 0
fi
if [[ "$#" -gt 0 && "$1" == "-t" \
  && "${FAKE_GZIP_TEST_FAIL:-0}" == "1" ]]; then
  exit 52
fi
exec /usr/bin/gzip "$@"
""",
    )
    _write_executable(
        binaries / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
[[ "${FAKE_CURL_FAIL:-0}" != "1" ]]
""",
    )
    _write_executable(
        binaries / "sha256sum",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'sha256sum %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
if [[ "$1" == "-c" && "${FAKE_SHA_FAIL:-0}" == "1" ]]; then
  exit 53
fi
exec /usr/bin/sha256sum "$@"
""",
    )
    _write_executable(
        binaries / "mv",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'mv %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
source_path="${@: -2:1}"
destination="${@: -1}"
if [[ "${FAKE_FINAL_DIR_RACE:-0}" == "1" \
  && "${source_path}" == *".backup-a.incomplete."* \
  && "${destination}" == */backups/backup-a ]]; then
  mkdir "${destination}"
fi
exec /usr/bin/mv "$@"
""",
    )
    return _BackupSandbox(
        root=root,
        script=script,
        env_file=env_file,
        backups=backups,
        state_file=state_file,
        command_log=command_log,
        binaries=binaries,
    )


def _run_backup(
    sandbox: _BackupSandbox,
    backup_id: str = "backup-a",
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
            "FAKE_COMMAND_LOG": str(sandbox.command_log),
            "FAKE_CONTAINER_STATE": str(sandbox.state_file),
        }
    )
    environment.update(overrides)
    return subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(sandbox.script),
            backup_id,
            str(sandbox.env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _container_states(sandbox: _BackupSandbox) -> str:
    return sandbox.state_file.read_text(encoding="ascii")


def test_backup_uses_fixed_sources_and_private_atomic_output() -> None:
    script = _backup_script()

    assert "set -euo pipefail" in script
    assert "umask 077" in script
    assert 'project_root="/data/tyf/RAG"' in script
    assert 'state_path="${project_root}/data/state"' in script
    assert 'qdrant_path="${project_root}/data/qdrant"' in script
    assert "sudo tar --format=posix" in script
    assert "| gzip >" in script
    assert "chmod 0600" in script
    assert "sha256sum -c MANIFEST.sha256" in script
    assert "mv -T" in script


def test_backup_restores_only_the_original_running_services() -> None:
    script = _backup_script()

    assert ".State.Running" in script
    assert "rag-worker" in script
    assert "--profile index" in script
    assert "restore_services" in script
    assert "trap on_exit EXIT" in script
    assert "/live" in script


def test_success_creates_private_verified_archives_and_restores_services(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    history = sandbox.backups / "history"
    history.mkdir()
    (history / "keep").write_text("keep", encoding="ascii")

    completed = _run_backup(sandbox)

    assert completed.returncode == 0, completed.stderr
    final = sandbox.backups / "backup-a"
    assert final.is_dir()
    assert (history / "keep").read_text(encoding="ascii") == "keep"
    assert stat.S_IMODE(final.stat().st_mode) == 0o700
    for filename in ("state.tar.gz", "qdrant.tar.gz", "MANIFEST.sha256"):
        output = final / filename
        assert output.stat().st_size > 0
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert output.stat().st_uid == os.getuid()
        assert output.stat().st_gid == os.getgid()
    manifest_check = subprocess.run(
        ["/usr/bin/sha256sum", "-c", "MANIFEST.sha256"],
        cwd=final,
        check=False,
        capture_output=True,
        text=True,
    )
    assert manifest_check.returncode == 0
    assert "APP_RUNNING=true" in _container_states(sandbox)
    assert "WORKER_RUNNING=false" in _container_states(sandbox)
    assert "QDRANT_RUNNING=true" in _container_states(sandbox)
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert "sudo tar --format=posix" in log
    assert " up -d --no-deps --no-build --pull never rag-worker" not in log
    assert "curl -fsS --max-time 10 http://127.0.0.1:8088/live" in log
    assert not tuple(sandbox.backups.glob(".backup-a.incomplete.*"))


def test_running_worker_is_restored_through_index_profile(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    sandbox.state_file.write_text(
        "APP_RUNNING=true\n"
        "WORKER_RUNNING=true\n"
        "QDRANT_RUNNING=true\n",
        encoding="ascii",
    )

    completed = _run_backup(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert "WORKER_RUNNING=true" in _container_states(sandbox)
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert "--profile index up -d --no-deps" in log
    assert log.index(" stop rag-worker rag-app rag-qdrant") < log.index(
        " up -d --no-deps"
    )


def test_symlink_backup_root_and_existing_id_are_rejected(
    tmp_path: Path,
) -> None:
    symlink_sandbox = _prepare_sandbox(tmp_path / "symlink")
    alternate = symlink_sandbox.root / "alternate-backups"
    alternate.mkdir()
    symlink_sandbox.backups.rmdir()
    symlink_sandbox.backups.symlink_to(alternate)

    symlink_result = _run_backup(symlink_sandbox)

    assert symlink_result.returncode != 0
    existing_sandbox = _prepare_sandbox(tmp_path / "existing")
    existing = existing_sandbox.backups / "backup-a"
    existing.mkdir()
    marker = existing / "history"
    marker.write_text("do-not-delete", encoding="ascii")

    existing_result = _run_backup(existing_sandbox)

    assert existing_result.returncode != 0
    assert marker.read_text(encoding="ascii") == "do-not-delete"


@pytest.mark.parametrize("missing_directory", ("state", "qdrant"))
def test_missing_fixed_source_directory_fails_without_formal_backup(
    tmp_path: Path,
    missing_directory: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    shutil.rmtree(sandbox.root / "data" / missing_directory)

    completed = _run_backup(sandbox)

    assert completed.returncode != 0
    assert not (sandbox.backups / "backup-a").exists()


@pytest.mark.parametrize(
    "failure",
    (
        "FAKE_EMPTY_ARCHIVE",
        "FAKE_GZIP_TEST_FAIL",
        "FAKE_TAR_LIST_FAIL",
        "FAKE_SHA_FAIL",
    ),
)
def test_archive_validation_failures_restore_services_without_publish(
    tmp_path: Path,
    failure: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    history = sandbox.backups / "history"
    history.mkdir()
    marker = history / "keep"
    marker.write_text("keep", encoding="ascii")

    completed = _run_backup(sandbox, **{failure: "1"})

    assert completed.returncode != 0
    assert not (sandbox.backups / "backup-a").exists()
    assert tuple(sandbox.backups.glob(".backup-a.incomplete.*"))
    assert marker.read_text(encoding="ascii") == "keep"
    assert "APP_RUNNING=true" in _container_states(sandbox)
    assert "WORKER_RUNNING=false" in _container_states(sandbox)
    assert "QDRANT_RUNNING=true" in _container_states(sandbox)


def test_restore_failure_keeps_verified_backup_and_returns_stable_code(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_backup(
        sandbox,
        FAKE_RESTORE_FAIL_SERVICE="rag-qdrant",
    )

    assert completed.returncode == 70
    final = sandbox.backups / "backup-a"
    assert final.is_dir()
    assert (final / "MANIFEST.sha256").is_file()
    assert "已验证的正式备份不会删除" in completed.stderr


def test_archive_with_symlink_is_rejected_and_services_restore(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="ascii")
    (sandbox.root / "data/state/escape").symlink_to(outside)

    completed = _run_backup(sandbox)

    assert completed.returncode != 0
    assert "链接、设备或 FIFO" in completed.stderr
    assert not (sandbox.backups / "backup-a").exists()
    assert "APP_RUNNING=true" in _container_states(sandbox)
    assert "QDRANT_RUNNING=true" in _container_states(sandbox)


def test_bad_backup_id_cannot_escape_fixed_backup_root(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_backup(sandbox, backup_id="../escaped")

    assert completed.returncode != 0
    assert not (sandbox.root / "escaped").exists()


def test_final_publish_does_not_replace_racing_backup_target(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_backup(sandbox, FAKE_FINAL_DIR_RACE="1")

    assert completed.returncode != 0
    assert (sandbox.backups / "backup-a").is_dir()
    assert not (sandbox.backups / "backup-a/MANIFEST.sha256").exists()
    assert tuple(sandbox.backups.glob(".backup-a.incomplete.*"))
