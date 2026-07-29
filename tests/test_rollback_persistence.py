from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_APP_IMAGE = "sha256:" + "a" * 64
_OCR_IMAGE = "sha256:" + "b" * 64
_QDRANT_IMAGE = "sha256:" + "c" * 64
_SOURCE_REVISION = "1" * 40


def _rollback_script() -> str:
    root = Path(__file__).parents[1]
    return (root / "deployment/rollback.sh").read_text(encoding="utf-8")


@dataclass(frozen=True)
class _Sandbox:
    root: Path
    script: Path
    env_file: Path
    rollback_file: Path
    old_release: Path
    new_release: Path
    current_link: Path
    docker_log: Path
    state_file: Path
    original_env: str
    original_rollback: str


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_sandbox(tmp_path: Path) -> _Sandbox:
    root = tmp_path / "RAG"
    shared_env = root / "shared/env"
    old_release = root / "releases/old-release"
    new_release = root / "releases/new-release"
    for directory in (shared_env, old_release, new_release):
        directory.mkdir(parents=True)
    current_link = root / "current"
    current_link.symlink_to(new_release)
    env_file = shared_env / "rag.env"
    original_env = (
        f"RAG_APP_IMAGE={'sha256:' + 'd' * 64}\n"
        f"RAG_OCR_IMAGE={'sha256:' + 'e' * 64}\n"
        f"RAG_QDRANT_IMAGE={'sha256:' + 'f' * 64}\n"
        f"RAG_RELEASE_REVISION={'2' * 40}\n"
        "RAG_PORT=8088\n"
        "RAG_QUERY_TOKEN=keep-secret\n"
        "CUSTOM_SETTING=keep-value\n"
    )
    env_file.write_text(original_env, encoding="utf-8")
    env_file.chmod(0o600)
    rollback_file = shared_env / "rollback-images.env"
    original_rollback = (
        f"ROLLBACK_RELEASE_DIR={old_release}\n"
        f"ROLLBACK_APP_IMAGE={_APP_IMAGE}\n"
        f"ROLLBACK_OCR_IMAGE={_OCR_IMAGE}\n"
        f"ROLLBACK_QDRANT_IMAGE={_QDRANT_IMAGE}\n"
    )
    rollback_file.write_text(original_rollback, encoding="utf-8")
    (old_release / "verify-offline.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    (old_release / "compose.yaml").write_text(
        "services:\n"
        "  rag-app:\n"
        "    image: ${RAG_APP_IMAGE:?required}\n"
        "  rag-ocr:\n"
        "    image: ${RAG_OCR_IMAGE:?required}\n"
        "  rag-qdrant:\n"
        "    image: ${RAG_QDRANT_IMAGE:?required}\n"
        "  rag-worker:\n"
        "    profiles: [index]\n"
        "    image: ${RAG_APP_IMAGE:?required}\n",
        encoding="utf-8",
    )
    (old_release / "SOURCE_REVISION").write_text(
        f"{_SOURCE_REVISION}\n",
        encoding="ascii",
    )
    (old_release / "QDRANT_SOURCE_IMAGE").write_text(
        f"qdrant/qdrant:v1.18.3@{_QDRANT_IMAGE}\n",
        encoding="ascii",
    )
    (new_release / "compose.yaml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )

    script = tmp_path / "rollback.sh"
    script.write_text(
        _rollback_script().replace(
            'project_root="/data/tyf/RAG"',
            f'project_root="{root}"',
            1,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker_log = tmp_path / "docker.log"
    state_file = tmp_path / "container-state.env"
    _write_executable(
        binaries / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG}"
if [[ "$1 $2" == "image inspect" ]]; then
  image="${@: -1}"
  if [[ "${FAKE_MISSING_IMAGE:-}" == "${image}" ]]; then
    exit 41
  fi
  if [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
    if [[ "${FAKE_BAD_REVISION:-0}" == "1" \
      && "${image}" == "${FAKE_APP_IMAGE}" ]]; then
      printf '%040d\n' 9
    else
      printf '%s\n' "${FAKE_SOURCE_REVISION}"
    fi
  else
    printf '%s\n' "${image}"
  fi
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  container="${@: -1}"
  if [[ "$*" != *"--format"* ]]; then
    [[ "${container}" != "rag-worker" || "${FAKE_WORKER_EXISTS:-1}" == "1" ]]
    exit
  fi
  if [[ "$*" == *".State.Running"* ]]; then
    [[ "${FAKE_WORKER_RUNNING:-0}" == "1" ]] && echo true || echo false
    exit 0
  fi
  source "${FAKE_STATE_FILE}"
  case "${container}" in
    rag-app) actual="${APP_IMAGE}" ;;
    rag-ocr) actual="${OCR_IMAGE}" ;;
    rag-qdrant) actual="${QDRANT_IMAGE}" ;;
    rag-worker) actual="${WORKER_IMAGE}" ;;
    *) exit 42 ;;
  esac
  if [[ "${FAKE_BAD_CONTAINER:-}" == "${container}" ]]; then
    actual="sha256:$(printf '%064d' 0)"
  fi
  printf '%s\n' "${actual}"
  exit 0
fi
if [[ "$1" == "compose" ]]; then
  env_file=""
  previous=""
  for argument in "$@"; do
    if [[ "${previous}" == "--env-file" ]]; then
      env_file="${argument}"
    fi
    previous="${argument}"
  done
  if [[ "$*" == *" config -q"* || "$*" == *" ps"* ]]; then
    exit 0
  fi
  if [[ "$*" == *" up -d "* ]]; then
    if [[ "${FAKE_COMPOSE_UP_FAIL:-0}" == "1" ]]; then
      exit 43
    fi
    app="$(awk -F= '$1 == "RAG_APP_IMAGE" {print $2}' "${env_file}")"
    ocr="$(awk -F= '$1 == "RAG_OCR_IMAGE" {print $2}' "${env_file}")"
    qdrant="$(awk -F= '$1 == "RAG_QDRANT_IMAGE" {print $2}' "${env_file}")"
    {
      printf 'APP_IMAGE=%q\n' "${app}"
      printf 'OCR_IMAGE=%q\n' "${ocr}"
      printf 'QDRANT_IMAGE=%q\n' "${qdrant}"
      printf 'WORKER_IMAGE=%q\n' "${app}"
    } > "${FAKE_STATE_FILE}"
    exit 0
  fi
fi
exit 44
""",
    )
    _write_executable(
        binaries / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "${FAKE_DOCKER_LOG}"
[[ "${FAKE_CURL_FAIL:-0}" != "1" ]]
""",
    )
    _write_executable(
        binaries / "mv",
        """#!/usr/bin/env bash
set -euo pipefail
arguments=("$@")
count="${#arguments[@]}"
source_path="${arguments[count - 2]}"
destination="${arguments[count - 1]}"
if [[ "${FAKE_FAIL_ENV_REPLACE:-0}" == "1" \
  && "${source_path}" == *"rag.env.rollback-new."* \
  && "${destination}" == "${FAKE_ENV_FILE}" ]]; then
  exit 51
fi
if [[ "${FAKE_FAIL_CURRENT_RENAME:-0}" == "1" \
  && "${source_path}" == *"current.rollback-new" \
  && "${destination}" == "${FAKE_CURRENT_LINK}" ]]; then
  exit 52
fi
/usr/bin/mv "$@"
if [[ "${FAKE_CORRUPT_ENV_AFTER_CURRENT:-0}" == "1" \
  && "${source_path}" == *"current.rollback-new" \
  && "${destination}" == "${FAKE_CURRENT_LINK}" ]]; then
  /usr/bin/awk -F= '
    $1 == "RAG_APP_IMAGE" {$0 = "RAG_APP_IMAGE=sha256:bad"}
    {print}
  ' "${FAKE_ENV_FILE}" > "${FAKE_ENV_FILE}.corrupt"
  /usr/bin/mv "${FAKE_ENV_FILE}.corrupt" "${FAKE_ENV_FILE}"
fi
""",
    )
    return _Sandbox(
        root=root,
        script=script,
        env_file=env_file,
        rollback_file=rollback_file,
        old_release=old_release,
        new_release=new_release,
        current_link=current_link,
        docker_log=docker_log,
        state_file=state_file,
        original_env=original_env,
        original_rollback=original_rollback,
    )


def _run_rollback(
    sandbox: _Sandbox,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{sandbox.script.parent / 'bin'}:/usr/bin:/bin",
            "FAKE_DOCKER_LOG": str(sandbox.docker_log),
            "FAKE_STATE_FILE": str(sandbox.state_file),
            "FAKE_ENV_FILE": str(sandbox.env_file),
            "FAKE_CURRENT_LINK": str(sandbox.current_link),
            "FAKE_APP_IMAGE": _APP_IMAGE,
            "FAKE_SOURCE_REVISION": _SOURCE_REVISION,
        }
    )
    environment.update(overrides)
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(sandbox.script), str(sandbox.env_file)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _assert_original_metadata(sandbox: _Sandbox) -> None:
    assert sandbox.env_file.read_text(encoding="utf-8") == sandbox.original_env
    assert sandbox.current_link.resolve() == sandbox.new_release
    assert (
        sandbox.rollback_file.read_text(encoding="utf-8")
        == sandbox.original_rollback
    )


def test_rollback_revalidates_old_release_and_image_identity() -> None:
    script = _rollback_script()

    for required in (
        'bash "${rollback_release_dir}/verify-offline.sh"',
        "SOURCE_REVISION",
        "QDRANT_SOURCE_IMAGE",
        "docker compose",
        "config -q",
        "org.opencontainers.image.revision",
    ):
        assert required in script


def test_rollback_atomically_persists_only_release_image_keys() -> None:
    script = _rollback_script()

    assert "rag.env.rollback-new" in script
    assert "rag.env.rollback-old" in script
    assert "chmod 0600" in script
    assert "RAG_RELEASE_REVISION" in script
    assert 'mv "${new_env}" "${env_file}"' in script
    assert "sed -i" not in script


def test_rollback_preserves_worker_state_and_compensates_metadata() -> None:
    script = _rollback_script()

    assert ".State.Running" in script
    assert "--profile index" in script
    assert "restore_metadata" in script
    assert "verify_persisted_state" in script
    assert 'env \\\n  RAG_APP_IMAGE=' not in script


def test_success_persists_images_and_normal_restart_uses_them(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(sandbox)

    assert completed.returncode == 0, completed.stderr
    persisted = sandbox.env_file.read_text(encoding="utf-8")
    assert f"RAG_APP_IMAGE={_APP_IMAGE}\n" in persisted
    assert f"RAG_OCR_IMAGE={_OCR_IMAGE}\n" in persisted
    assert f"RAG_QDRANT_IMAGE={_QDRANT_IMAGE}\n" in persisted
    assert f"RAG_RELEASE_REVISION={_SOURCE_REVISION}\n" in persisted
    assert "RAG_QUERY_TOKEN=keep-secret\n" in persisted
    assert "CUSTOM_SETTING=keep-value\n" in persisted
    assert stat.S_IMODE(sandbox.env_file.stat().st_mode) == 0o600
    assert sandbox.current_link.resolve() == sandbox.old_release
    log = sandbox.docker_log.read_text(encoding="utf-8")
    assert log.count(" up -d --no-build --pull never") == 2
    assert "--profile index" not in log
    up_calls = [line for line in log.splitlines() if " up -d " in line]
    assert all("rag-worker" not in call for call in up_calls)
    assert (
        sandbox.rollback_file.read_text(encoding="utf-8")
        == sandbox.original_rollback
    )


def test_running_worker_is_restored_with_old_app_image(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(sandbox, FAKE_WORKER_RUNNING="1")

    assert completed.returncode == 0, completed.stderr
    log = sandbox.docker_log.read_text(encoding="utf-8")
    assert "--profile index up -d --no-build --pull never" in log
    state = sandbox.state_file.read_text(encoding="utf-8")
    assert f"WORKER_IMAGE={_APP_IMAGE}" in state


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    (
        ("verify", ""),
        ("missing_image", ""),
        ("bad_revision", "revision"),
        ("compose_up", "Compose"),
        ("container_image", "容器镜像"),
    ),
)
def test_precommit_failures_leave_metadata_unchanged(
    tmp_path: Path,
    failure_mode: str,
    expected_error: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    overrides: dict[str, str] = {}
    if failure_mode == "verify":
        (sandbox.old_release / "verify-offline.sh").write_text(
            "#!/usr/bin/env bash\nexit 9\n",
            encoding="utf-8",
        )
    elif failure_mode == "missing_image":
        overrides["FAKE_MISSING_IMAGE"] = _OCR_IMAGE
    elif failure_mode == "bad_revision":
        overrides["FAKE_BAD_REVISION"] = "1"
    elif failure_mode == "compose_up":
        overrides["FAKE_COMPOSE_UP_FAIL"] = "1"
    elif failure_mode == "container_image":
        overrides["FAKE_BAD_CONTAINER"] = "rag-app"

    completed = _run_rollback(sandbox, **overrides)

    assert completed.returncode != 0
    assert expected_error in completed.stderr
    _assert_original_metadata(sandbox)


@pytest.mark.parametrize("key_state", ("missing", "duplicate"))
def test_invalid_shared_env_image_keys_fail_before_compose_up(
    tmp_path: Path,
    key_state: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    lines = sandbox.original_env.splitlines()
    if key_state == "missing":
        lines = [
            line
            for line in lines
            if not line.startswith("RAG_APP_IMAGE=")
        ]
    else:
        lines.append(f"RAG_APP_IMAGE={'sha256:' + '9' * 64}")
    sandbox.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    expected_env = sandbox.env_file.read_text(encoding="utf-8")

    completed = _run_rollback(sandbox)

    assert completed.returncode != 0
    assert "恰好出现一次" in completed.stderr
    assert sandbox.env_file.read_text(encoding="utf-8") == expected_env
    assert sandbox.current_link.resolve() == sandbox.new_release
    log = (
        sandbox.docker_log.read_text(encoding="utf-8")
        if sandbox.docker_log.exists()
        else ""
    )
    assert " up -d " not in log


@pytest.mark.parametrize(
    "failure_flag",
    ("FAKE_FAIL_ENV_REPLACE", "FAKE_FAIL_CURRENT_RENAME"),
)
def test_atomic_commit_failures_restore_original_metadata(
    tmp_path: Path,
    failure_flag: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(sandbox, **{failure_flag: "1"})

    assert completed.returncode != 0
    _assert_original_metadata(sandbox)
    assert not tuple((sandbox.root / "shared/env").glob("*.rollback-*"))


def test_post_switch_env_validation_failure_restores_both_paths(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_CORRUPT_ENV_AFTER_CURRENT="1",
    )

    assert completed.returncode != 0
    assert "已恢复" in completed.stderr
    _assert_original_metadata(sandbox)
