from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_OLD_APP_IMAGE = "sha256:" + "a" * 64
_OLD_OCR_IMAGE = "sha256:" + "b" * 64
_OLD_QDRANT_IMAGE = "sha256:" + "c" * 64
_NEW_APP_IMAGE = "sha256:" + "d" * 64
_NEW_OCR_IMAGE = "sha256:" + "e" * 64
_NEW_QDRANT_IMAGE = "sha256:" + "f" * 64
_OLD_REVISION = "1" * 40
_NEW_REVISION = "2" * 40
_MODEL_NETWORK_PREFIX = ".".join(("10", "242", "180"))
_VALID_EMBEDDING_ENDPOINTS = (
    f'["http://{_MODEL_NETWORK_PREFIX}.57:8000/v1"]'
)
_VALID_RERANKER_ENDPOINTS = (
    f'["http://{_MODEL_NETWORK_PREFIX}.58:8000"]'
)
_VALID_LLM_ENDPOINTS = (
    f'["http://{_MODEL_NETWORK_PREFIX}.57:8000/v1",'
    f'"http://{_MODEL_NETWORK_PREFIX}.57:8001/v1",'
    f'"http://{_MODEL_NETWORK_PREFIX}.58:8000/v1",'
    f'"https://{_MODEL_NETWORK_PREFIX}.60:8001/v1"]'
)


@dataclass(frozen=True)
class _DeploySandbox:
    root: Path
    script: Path
    env_file: Path
    active_env: Path
    current_link: Path
    old_release: Path
    new_release: Path
    state_file: Path
    clock_file: Path
    command_log: Path
    binaries: Path
    original_active: str
    original_rollback: str


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_sandbox(
    tmp_path: Path,
    *,
    worker_exists: bool = True,
    worker_running: bool = True,
) -> _DeploySandbox:
    root = tmp_path / "RAG"
    env_dir = root / "shared/env"
    candidate_dir = env_dir / "candidates"
    data = root / "data"
    state = data / "state"
    qdrant = data / "qdrant"
    docs = root / "shared/corpora/corpus/docs"
    old_release = root / "releases/old"
    new_release = root / "releases/new"
    for directory in (
        env_dir,
        candidate_dir,
        state,
        qdrant,
        docs,
        old_release,
        new_release / "images",
    ):
        directory.mkdir(parents=True)
    state.chmod(0o700)
    current_link = root / "current"
    current_link.symlink_to(old_release)
    env_file = candidate_dir / "new.env"
    candidate_content = (
        "RAG_APP_IMAGE=new-app:new\n"
        "RAG_OCR_IMAGE=new-ocr:new\n"
        "RAG_QDRANT_IMAGE=new-qdrant:new\n"
        f"RAG_RELEASE_REVISION={_NEW_REVISION}\n"
        f"RAG_STATE_PATH={state}\n"
        f"RAG_QDRANT_PATH={qdrant}\n"
        f"RAG_DOCS_PATH={docs}\n"
        "RAG_PORT=8088\n"
        f"RAG_EMBEDDING_ENDPOINTS={_VALID_EMBEDDING_ENDPOINTS}\n"
        f"RAG_RERANKER_ENDPOINTS={_VALID_RERANKER_ENDPOINTS}\n"
        f"RAG_LLM_ENDPOINTS={_VALID_LLM_ENDPOINTS}\n"
        "RAG_QDRANT_API_KEY=deploy-qdrant-secret\n"
        "CUSTOM_SETTING=candidate-value\n"
    )
    env_file.write_text(candidate_content, encoding="utf-8")
    env_file.chmod(0o600)
    active_env = env_dir / "rag.env"
    original_active = (
        f"RAG_APP_IMAGE={_OLD_APP_IMAGE}\n"
        f"RAG_OCR_IMAGE={_OLD_OCR_IMAGE}\n"
        f"RAG_QDRANT_IMAGE={_OLD_QDRANT_IMAGE}\n"
        f"RAG_RELEASE_REVISION={_OLD_REVISION}\n"
        f"RAG_STATE_PATH={state}\n"
        f"RAG_QDRANT_PATH={qdrant}\n"
        f"RAG_DOCS_PATH={docs}\n"
        "RAG_PORT=8088\n"
        "RAG_QDRANT_API_KEY=deploy-old-qdrant-secret\n"
        "CUSTOM_SETTING=active-value\n"
    )
    active_env.write_text(original_active, encoding="utf-8")
    active_env.chmod(0o600)
    rollback_file = env_dir / "rollback-images.env"
    original_rollback = "OLD_ROLLBACK_STATE=preserve\n"
    rollback_file.write_text(original_rollback, encoding="utf-8")
    rollback_file.chmod(0o600)
    (new_release / "RELEASE_ID").write_text("new\n", encoding="ascii")
    (new_release / "SOURCE_REVISION").write_text(
        f"{_NEW_REVISION}\n",
        encoding="ascii",
    )
    (new_release / "IMAGE_ARCHIVES.tsv").write_text(
        "images/docx-rag-linux-amd64.tar\tnew-app:new\t"
        f"{_NEW_APP_IMAGE}\t{_NEW_REVISION}\n"
        "images/docx-rag-ocr-linux-amd64.tar\tnew-ocr:new\t"
        f"{_NEW_OCR_IMAGE}\t{_NEW_REVISION}\n"
        "images/qdrant-linux-amd64.tar\tnew-qdrant:new\t"
        f"{_NEW_QDRANT_IMAGE}\tqdrant/qdrant@sha256:{'9' * 64}\n",
        encoding="ascii",
    )
    (new_release / "verify-offline.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    (new_release / "compose.yaml").write_text(
        "services:\n"
        "  rag-app: {image: '${RAG_APP_IMAGE:?required}'}\n"
        "  rag-ocr: {image: '${RAG_OCR_IMAGE:?required}'}\n"
        "  rag-qdrant: {image: '${RAG_QDRANT_IMAGE:?required}'}\n"
        "  rag-worker:\n"
        "    image: '${RAG_APP_IMAGE:?required}'\n"
        "    profiles: [index]\n",
        encoding="utf-8",
    )
    (old_release / "SOURCE_REVISION").write_text(
        f"{_OLD_REVISION}\n",
        encoding="ascii",
    )
    shutil.copyfile(
        new_release / "compose.yaml",
        old_release / "compose.yaml",
    )
    (old_release / "verify-offline.sh").write_text(
        "#!/usr/bin/env bash\n"
        "printf 'verify-old-release\\n' >> \"${FAKE_COMMAND_LOG}\"\n"
        "[[ \"${FAKE_OLD_VERIFY_FAIL:-0}\" != \"1\" ]]\n",
        encoding="utf-8",
    )
    source = Path(__file__).parents[1] / "deployment/deploy.sh"
    script = new_release / "deploy.sh"
    script.write_text(
        source.read_text(encoding="utf-8").replace(
            'project_root="/data/tyf/RAG"',
            f'project_root="{root}"',
            1,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    command_log = tmp_path / "commands.log"
    state_file = tmp_path / "containers.env"
    clock_file = tmp_path / "clock"
    clock_file.write_text("0\n", encoding="ascii")
    state_file.write_text(
        "APP_EXISTS=true\n"
        "APP_RUNNING=true\n"
        f"APP_IMAGE={_OLD_APP_IMAGE}\n"
        "OCR_EXISTS=true\n"
        "OCR_RUNNING=true\n"
        f"OCR_IMAGE={_OLD_OCR_IMAGE}\n"
        "QDRANT_EXISTS=true\n"
        "QDRANT_RUNNING=true\n"
        f"QDRANT_IMAGE={_OLD_QDRANT_IMAGE}\n"
        f"WORKER_EXISTS={'true' if worker_exists else 'false'}\n"
        f"WORKER_RUNNING={'true' if worker_running else 'false'}\n"
        f"WORKER_IMAGE={_OLD_APP_IMAGE}\n",
        encoding="ascii",
    )
    _install_fake_commands(binaries)
    return _DeploySandbox(
        root=root,
        script=script,
        env_file=env_file,
        active_env=active_env,
        current_link=current_link,
        old_release=old_release,
        new_release=new_release,
        state_file=state_file,
        clock_file=clock_file,
        command_log=command_log,
        binaries=binaries,
        original_active=original_active,
        original_rollback=original_rollback,
    )


def _install_fake_commands(binaries: Path) -> None:
    _write_executable(
        binaries / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >> "${{FAKE_COMMAND_LOG}}"
source "${{FAKE_CONTAINER_STATE}}"
write_state() {{
  {{
    printf 'APP_EXISTS=%s\n' "${{APP_EXISTS}}"
    printf 'APP_RUNNING=%s\n' "${{APP_RUNNING}}"
    printf 'APP_IMAGE=%s\n' "${{APP_IMAGE}}"
    printf 'OCR_EXISTS=%s\n' "${{OCR_EXISTS}}"
    printf 'OCR_RUNNING=%s\n' "${{OCR_RUNNING}}"
    printf 'OCR_IMAGE=%s\n' "${{OCR_IMAGE}}"
    printf 'QDRANT_EXISTS=%s\n' "${{QDRANT_EXISTS}}"
    printf 'QDRANT_RUNNING=%s\n' "${{QDRANT_RUNNING}}"
    printf 'QDRANT_IMAGE=%s\n' "${{QDRANT_IMAGE}}"
    printf 'WORKER_EXISTS=%s\n' "${{WORKER_EXISTS}}"
    printf 'WORKER_RUNNING=%s\n' "${{WORKER_RUNNING}}"
    printf 'WORKER_IMAGE=%s\n' "${{WORKER_IMAGE}}"
  }} > "${{FAKE_CONTAINER_STATE}}"
}}
container_field() {{
  local container="$1"
  local field="$2"
  case "${{container}}:${{field}}" in
    rag-app:exists) echo "${{APP_EXISTS}}" ;;
    rag-app:running) echo "${{APP_RUNNING}}" ;;
    rag-app:image) echo "${{APP_IMAGE}}" ;;
    rag-ocr:exists) echo "${{OCR_EXISTS}}" ;;
    rag-ocr:running) echo "${{OCR_RUNNING}}" ;;
    rag-ocr:image) echo "${{OCR_IMAGE}}" ;;
    rag-qdrant:exists) echo "${{QDRANT_EXISTS}}" ;;
    rag-qdrant:running) echo "${{QDRANT_RUNNING}}" ;;
    rag-qdrant:image) echo "${{QDRANT_IMAGE}}" ;;
    rag-worker:exists) echo "${{WORKER_EXISTS}}" ;;
    rag-worker:running) echo "${{WORKER_RUNNING}}" ;;
    rag-worker:image) echo "${{WORKER_IMAGE}}" ;;
    *) exit 81 ;;
  esac
}}
resolve_image() {{
  case "$1" in
    new-app:new)
      if [[ "${{FAKE_BAD_LOADED_APP_ID:-0}}" == "1" ]]; then
        echo "sha256:$(printf '%064d' 6)"
      else
        echo "{_NEW_APP_IMAGE}"
      fi
      ;;
    new-ocr:new)
      if [[ "${{FAKE_BAD_LOADED_OCR_ID:-0}}" == "1" ]]; then
        echo "sha256:$(printf '%064d' 7)"
      else
        echo "{_NEW_OCR_IMAGE}"
      fi
      ;;
    new-qdrant:new)
      if [[ "${{FAKE_BAD_LOADED_QDRANT_ID:-0}}" == "1" ]]; then
        echo "sha256:$(printf '%064d' 8)"
      else
        echo "{_NEW_QDRANT_IMAGE}"
      fi
      ;;
    sha256:*) echo "$1" ;;
    *) exit 82 ;;
  esac
}}
if [[ "$1" == "ps" ]]; then
  for item in \
    "rag-app:${{APP_EXISTS}}" \
    "rag-ocr:${{OCR_EXISTS}}" \
    "rag-qdrant:${{QDRANT_EXISTS}}" \
    "rag-worker:${{WORKER_EXISTS}}"; do
    [[ "${{item##*:}}" == "true" ]] && echo "${{item%%:*}}"
  done
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  container="${{@: -1}}"
  exists="$(container_field "${{container}}" exists)"
  [[ "${{exists}}" == "true" ]] || exit 1
  if [[ "$*" == *".State.Running"* ]]; then
    container_field "${{container}}" running
  elif [[ "$*" == *".State.Health.Status"* ]]; then
    case "${{container}}" in
      rag-qdrant)
        if [[ "${{QDRANT_IMAGE}}" == "{_NEW_QDRANT_IMAGE}" ]]; then
          status="${{FAKE_QDRANT_HEALTH:-healthy}}"
          marker="${{FAKE_COMMAND_LOG}}.qdrant-health"
          if [[ "${{status}}" == "starting_then_healthy" \
            && ! -e "${{marker}}" ]]; then
            : > "${{marker}}"
            echo starting
          elif [[ "${{status}}" == "starting_then_healthy" ]]; then
            echo healthy
          else
            echo "${{status}}"
          fi
        else
          echo healthy
        fi
        ;;
      rag-ocr)
        if [[ "${{OCR_IMAGE}}" == "{_NEW_OCR_IMAGE}" ]]; then
          if [[ -n "${{FAKE_OCR_HEALTHY_AT_SECONDS:-}}" ]]; then
            elapsed="$(cat "${{FAKE_CLOCK_FILE}}")"
            if ((elapsed >= FAKE_OCR_HEALTHY_AT_SECONDS)); then
              echo healthy
            else
              echo starting
            fi
          elif [[ "${{FAKE_OCR_HEALTH:-healthy}}" == "disappear" ]]; then
            OCR_EXISTS=false
            OCR_RUNNING=false
            write_state
            exit 1
          elif [[ "${{FAKE_OCR_HEALTH:-healthy}}" == "no_health" ]]; then
            echo ""
          else
            echo "${{FAKE_OCR_HEALTH:-healthy}}"
          fi
        else
          echo healthy
        fi
        ;;
      rag-app)
        if [[ "${{APP_IMAGE}}" == "{_NEW_APP_IMAGE}" ]]; then
          echo "${{FAKE_APP_HEALTH:-healthy}}"
        else
          echo healthy
        fi
        ;;
      *) exit 81 ;;
    esac
  elif [[ "$*" == *".Image"* ]]; then
    container_field "${{container}}" image
  fi
  exit 0
fi
if [[ "$1 $2" == "exec rag-app" ]]; then
  [[ "${{APP_EXISTS}}" == "true" \
    && "${{APP_RUNNING}}" == "true" \
    && "${{QDRANT_EXISTS}}" == "true" \
    && "${{QDRANT_RUNNING}}" == "true" ]] || exit 86
  if [[ "${{QDRANT_IMAGE}}" == "{_NEW_QDRANT_IMAGE}" ]]; then
    if [[ -n "${{FAKE_NEW_QDRANT_READY_AT_SECONDS:-}}" ]]; then
      elapsed="$(cat "${{FAKE_CLOCK_FILE}}")"
      if ((elapsed >= FAKE_NEW_QDRANT_READY_AT_SECONDS)); then
        exit 0
      fi
      exit 1
    fi
    mode="${{FAKE_NEW_QDRANT_READYZ:-ready}}"
  else
    mode="${{FAKE_OLD_QDRANT_READYZ:-ready}}"
  fi
  case "${{mode}}" in
    ready) exit 0 ;;
    disappear)
      QDRANT_EXISTS=false
      QDRANT_RUNNING=false
      write_state
      exit 1
      ;;
    non_200)
      echo "sensitive-qdrant-response-body"
      exit 1
      ;;
    connection_error|timeout) exit 1 ;;
    *) exit 87 ;;
  esac
fi
if [[ "$1 $2 $3" == "container rm -f" ]]; then
  case "${{@: -1}}" in
    rag-app) APP_EXISTS=false; APP_RUNNING=false ;;
    rag-ocr) OCR_EXISTS=false; OCR_RUNNING=false ;;
    rag-qdrant) QDRANT_EXISTS=false; QDRANT_RUNNING=false ;;
    rag-worker) WORKER_EXISTS=false; WORKER_RUNNING=false ;;
    *) exit 85 ;;
  esac
  write_state
  exit 0
fi
if [[ "$1 $2" == "image inspect" ]]; then
  image="${{@: -1}}"
  if [[ "$*" == *".Architecture"* ]]; then
    echo amd64
  elif [[ "$*" == *".Os"* ]]; then
    echo linux
  elif [[ "$*" == *".Id"* ]]; then
    resolve_image "${{image}}"
  elif [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
    echo "{_NEW_REVISION}"
  fi
  exit 0
fi
if [[ "$1" == "load" ]]; then
  [[ "${{FAKE_LOAD_FAIL:-0}}" != "1" ]]
  exit
fi
if [[ "$1" == "compose" ]]; then
  env_file=""
  previous=""
  for argument in "$@"; do
    if [[ "${{previous}}" == "--env-file" ]]; then
      env_file="${{argument}}"
    fi
    previous="${{argument}}"
  done
  if [[ "$*" == *" config -q"* ]]; then
    exit 0
  fi
  if [[ "$*" == *" stop rag-worker"* ]]; then
    WORKER_RUNNING=false
    write_state
    exit 0
  fi
  if [[ "$*" == *" up -d "* ]]; then
    app_ref="$(awk -F= '$1 == "RAG_APP_IMAGE" {{print $2}}' "${{env_file}}")"
    ocr_ref="$(awk -F= '$1 == "RAG_OCR_IMAGE" {{print $2}}' "${{env_file}}")"
    qdrant_ref="$(awk -F= '$1 == "RAG_QDRANT_IMAGE" {{print $2}}' \
      "${{env_file}}")"
    if [[ "$*" == \
      *" up -d --no-deps --no-build --pull never rag-worker"* ]]; then
      WORKER_EXISTS=true
      WORKER_RUNNING=true
      WORKER_IMAGE="$(resolve_image "${{app_ref}}")"
      write_state
      exit 0
    fi
    APP_EXISTS=true
    APP_RUNNING=true
    APP_IMAGE="$(resolve_image "${{app_ref}}")"
    OCR_EXISTS=true
    OCR_RUNNING=true
    OCR_IMAGE="$(resolve_image "${{ocr_ref}}")"
    QDRANT_EXISTS=true
    QDRANT_RUNNING=true
    QDRANT_IMAGE="$(resolve_image "${{qdrant_ref}}")"
    if [[ "$*" == *"rag-worker"* ]]; then
      WORKER_EXISTS=true
      WORKER_RUNNING=true
      WORKER_IMAGE="${{APP_IMAGE}}"
    fi
    write_state
    if [[ "${{FAKE_CORE_UP_PARTIAL_FAIL:-0}}" == "1" \
      && "${{app_ref}}" == "new-app:new" ]]; then
      exit 83
    fi
    exit 0
  fi
  if [[ "$*" == *" ps"* ]]; then
    [[ "${{FAKE_PS_FAIL:-0}}" != "1" ]]
    exit
  fi
fi
exit 84
""",
    )
    _write_executable(
        binaries / "stat",
        """#!/usr/bin/env bash
set -euo pipefail
path="${@: -1}"
if [[ "${path}" == *.env || "${path}" == */rollback-images.env ]]; then
  exec /usr/bin/stat "$@"
elif [[ "$*" == *"%u"* ]]; then
  echo 10001
elif [[ "$*" == *"%a"* ]]; then
  echo 700
else
  exec /usr/bin/stat "$@"
fi
""",
    )
    _write_executable(
        binaries / "find",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"! -uid 10001"* ]]; then
  exit 0
fi
exec /usr/bin/find "$@"
""",
    )
    _write_executable(
        binaries / "ss",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        binaries / "sleep",
        """#!/usr/bin/env bash
set -euo pipefail
elapsed="$(cat "${FAKE_CLOCK_FILE}")"
printf '%s\n' "$((elapsed + $1))" > "${FAKE_CLOCK_FILE}"
""",
    )
    _write_executable(
        binaries / "date",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == "+%s" ]]; then
  cat "${FAKE_CLOCK_FILE}"
else
  exec /usr/bin/date "$@"
fi
""",
    )
    _write_executable(
        binaries / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
source "${FAKE_CONTAINER_STATE}"
expected_new_app="sha256:$(printf 'd%.0s' {1..64})"
if [[ "${FAKE_NEW_CURL_FAIL:-0}" == "1" \
  && "${APP_IMAGE}" == "${expected_new_app}" ]]; then
  exit 1
fi
[[ "${FAKE_CURL_FAIL:-0}" != "1" ]]
""",
    )
    _write_executable(
        binaries / "mv",
        """#!/usr/bin/env bash
set -euo pipefail
source_path="${@: -2:1}"
destination="${@: -1}"
if [[ "${FAKE_CURRENT_RENAME_FAIL:-0}" == "1" \
  && "${source_path}" == *"current.new" \
  && "${destination}" == */current ]]; then
  exit 91
fi
if [[ "${FAKE_ACTIVE_ENV_REPLACE_FAIL:-0}" == "1" \
  && "${source_path}" == *".rag.env.active-new."* \
  && "${destination}" == */shared/env/rag.env ]]; then
  exit 92
fi
if [[ "${FAKE_ROLLBACK_PUBLISH_FAIL:-0}" == "1" \
  && "${source_path}" == *".rollback-images.env.new."* \
  && "${destination}" == */rollback-images.env ]]; then
  exit 93
fi
exec /usr/bin/mv "$@"
""",
    )


def _run_deploy(
    sandbox: _DeploySandbox,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
            "FAKE_COMMAND_LOG": str(sandbox.command_log),
            "FAKE_CONTAINER_STATE": str(sandbox.state_file),
            "FAKE_CLOCK_FILE": str(sandbox.clock_file),
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


def _state(sandbox: _DeploySandbox) -> dict[str, str]:
    return dict(
        line.split("=", maxsplit=1)
        for line in sandbox.state_file.read_text(
            encoding="ascii"
        ).splitlines()
    )


def _env_values(sandbox: _DeploySandbox) -> dict[str, str]:
    return dict(
        line.split("=", maxsplit=1)
        for line in sandbox.active_env.read_text(
            encoding="utf-8"
        ).splitlines()
    )


def _replace_env_value(
    sandbox: _DeploySandbox,
    key: str,
    value: str,
) -> None:
    lines = sandbox.env_file.read_text(encoding="utf-8").splitlines()
    matches = sum(line.startswith(f"{key}=") for line in lines)
    assert matches == 1
    sandbox.env_file.write_text(
        "\n".join(
            f"{key}={value}" if line.startswith(f"{key}=") else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )


def _command_log(sandbox: _DeploySandbox) -> str:
    if not sandbox.command_log.exists():
        return ""
    return sandbox.command_log.read_text(encoding="utf-8")


def _assert_old_runtime_restored(sandbox: _DeploySandbox) -> None:
    state = _state(sandbox)
    assert state["APP_IMAGE"] == _OLD_APP_IMAGE
    assert state["OCR_IMAGE"] == _OLD_OCR_IMAGE
    assert state["QDRANT_IMAGE"] == _OLD_QDRANT_IMAGE
    assert state["APP_RUNNING"] == "true"
    assert state["OCR_RUNNING"] == "true"
    assert state["QDRANT_RUNNING"] == "true"
    assert state["WORKER_RUNNING"] == "true"
    assert state["WORKER_IMAGE"] == _OLD_APP_IMAGE
    assert sandbox.current_link.resolve() == sandbox.old_release
    env = _env_values(sandbox)
    assert env["RAG_APP_IMAGE"] == _OLD_APP_IMAGE
    assert env["RAG_OCR_IMAGE"] == _OLD_OCR_IMAGE
    assert env["RAG_QDRANT_IMAGE"] == _OLD_QDRANT_IMAGE
    assert env["RAG_RELEASE_REVISION"] == _OLD_REVISION
    assert (
        sandbox.active_env.read_text(encoding="utf-8")
        == sandbox.original_active
    )
    assert (
        sandbox.root.joinpath(
            "shared/env/rollback-images.env"
        ).read_text(encoding="utf-8")
        == sandbox.original_rollback
    )


def _remove_core_runtime(sandbox: _DeploySandbox) -> None:
    """把 fake 状态调整为没有三个核心容器。

    Args:
        sandbox: 部署测试沙箱。

    """
    state = _state(sandbox)
    for prefix in ("APP", "OCR", "QDRANT"):
        state[f"{prefix}_EXISTS"] = "false"
        state[f"{prefix}_RUNNING"] = "false"
    sandbox.state_file.write_text(
        "".join(f"{key}={value}\n" for key, value in state.items()),
        encoding="ascii",
    )


def _assert_no_runtime_mutation(sandbox: _DeploySandbox) -> None:
    log = _command_log(sandbox)
    for command in ("docker load ", " up -d ", " stop "):
        assert command not in log


def test_running_worker_is_stopped_before_load_and_stays_stopped(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    state = _state(sandbox)
    assert state["WORKER_RUNNING"] == "false"
    assert sandbox.current_link.resolve() == sandbox.new_release
    log = _command_log(sandbox)
    assert log.index(" stop rag-worker") < log.index("docker load ")
    rollback = (
        sandbox.root / "shared/env/rollback-images.env"
    ).read_text(encoding="utf-8")
    assert "ROLLBACK_WORKER_WAS_RUNNING=true\n" in rollback


def test_valid_internal_model_endpoint_arrays_are_accepted(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert _env_values(sandbox)[
        "RAG_EMBEDDING_ENDPOINTS"
    ] == _VALID_EMBEDDING_ENDPOINTS
    assert _env_values(sandbox)[
        "RAG_RERANKER_ENDPOINTS"
    ] == _VALID_RERANKER_ENDPOINTS
    assert _env_values(sandbox)["RAG_LLM_ENDPOINTS"] == _VALID_LLM_ENDPOINTS


@pytest.mark.parametrize(
    ("endpoint_key", "endpoint_value", "expected_category"),
    (
        (
            "RAG_EMBEDDING_ENDPOINTS",
            '["http://example.invalid:8000"]',
            "MODEL_ENDPOINT_HOST_FORBIDDEN",
        ),
        (
            "RAG_RERANKER_ENDPOINTS",
            '["https://model.zone.invalid/v1"]',
            "MODEL_ENDPOINT_HOST_FORBIDDEN",
        ),
        (
            "RAG_LLM_ENDPOINTS",
            "not-json",
            "MODEL_ENDPOINTS_INVALID_JSON",
        ),
        (
            "RAG_EMBEDDING_ENDPOINTS",
            "[]",
            "MODEL_ENDPOINTS_EMPTY",
        ),
        (
            "RAG_RERANKER_ENDPOINTS",
            (
                f'["http://{_MODEL_NETWORK_PREFIX}.58:8000",'
                f'"http://{_MODEL_NETWORK_PREFIX}.58:8000"]'
            ),
            "MODEL_ENDPOINTS_DUPLICATE",
        ),
        (
            "RAG_LLM_ENDPOINTS",
            f'["http://user:password@{_MODEL_NETWORK_PREFIX}.57:8000"]',
            "MODEL_ENDPOINT_CREDENTIALS_FORBIDDEN",
        ),
        (
            "RAG_EMBEDDING_ENDPOINTS",
            f'["http://{_MODEL_NETWORK_PREFIX}.57:8000/v1?mode=probe"]',
            "MODEL_ENDPOINT_QUERY_FORBIDDEN",
        ),
        (
            "RAG_RERANKER_ENDPOINTS",
            f'["http://{_MODEL_NETWORK_PREFIX}.58:8000/v1#probe"]',
            "MODEL_ENDPOINT_FRAGMENT_FORBIDDEN",
        ),
        (
            "RAG_LLM_ENDPOINTS",
            '{"endpoint":"http://service"}',
            "MODEL_ENDPOINTS_NOT_ARRAY",
        ),
        (
            "RAG_EMBEDDING_ENDPOINTS",
            "[123]",
            "MODEL_ENDPOINT_ITEM_INVALID",
        ),
        (
            "RAG_RERANKER_ENDPOINTS",
            '["http://REPLACE_MODEL_ENDPOINT"]',
            "CANDIDATE_PLACEHOLDER_FORBIDDEN",
        ),
        (
            "RAG_LLM_ENDPOINTS",
            '["ftp://model.internal/v1"]',
            "MODEL_ENDPOINT_URL_INVALID",
        ),
    ),
)
def test_invalid_model_endpoint_array_fails_before_any_docker_call(
    tmp_path: Path,
    endpoint_key: str,
    endpoint_value: str,
    expected_category: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    _replace_env_value(sandbox, endpoint_key, endpoint_value)

    completed = _run_deploy(sandbox)

    combined_output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert expected_category in combined_output
    assert endpoint_value not in combined_output
    assert _command_log(sandbox) == ""


def test_success_commits_candidate_then_complete_rollback_state(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    candidate = sandbox.env_file.read_bytes()

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert sandbox.active_env.read_bytes() == candidate
    rollback_file = sandbox.root / "shared/env/rollback-images.env"
    rollback = rollback_file.read_text(encoding="utf-8")
    assert "ROLLBACK_SCHEMA_VERSION=2\n" in rollback
    assert f"ROLLBACK_SOURCE_REVISION={_OLD_REVISION}\n" in rollback
    assert f"ROLLBACK_APP_IMAGE={_OLD_APP_IMAGE}\n" in rollback
    assert "ROLLBACK_WORKER_EXISTS=true\n" in rollback
    assert "ROLLBACK_ENV_SHA256=" in rollback
    assert "ROLLBACK_ENV_BASE64=" in rollback
    assert rollback_file.stat().st_mode & 0o777 == 0o600


def test_first_deploy_allows_missing_active_env(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(
        tmp_path,
        worker_exists=False,
        worker_running=False,
    )
    _remove_core_runtime(sandbox)
    sandbox.active_env.unlink()
    sandbox.current_link.unlink()
    rollback_file = sandbox.root / "shared/env/rollback-images.env"
    rollback_file.unlink()

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert sandbox.active_env.read_bytes() == sandbox.env_file.read_bytes()
    assert sandbox.current_link.resolve() == sandbox.new_release
    assert not rollback_file.exists()


def test_fresh_deploy_rejects_stale_rollback_before_load(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(
        tmp_path,
        worker_exists=False,
        worker_running=False,
    )
    _remove_core_runtime(sandbox)
    sandbox.active_env.unlink()
    sandbox.current_link.unlink()
    rollback_file = sandbox.root / "shared/env/rollback-images.env"
    stale_rollback = rollback_file.read_bytes()

    completed = _run_deploy(sandbox)

    assert completed.returncode != 0
    assert not sandbox.active_env.exists()
    assert not sandbox.current_link.exists()
    assert rollback_file.read_bytes() == stale_rollback
    _assert_no_runtime_mutation(sandbox)


def test_degraded_without_core_or_worker_publishes_complete_rollback(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(
        tmp_path,
        worker_exists=False,
        worker_running=False,
    )
    _remove_core_runtime(sandbox)

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    rollback = sandbox.root.joinpath(
        "shared/env/rollback-images.env",
    ).read_text(encoding="utf-8")
    assert "ROLLBACK_SCHEMA_VERSION=2\n" in rollback
    assert f"ROLLBACK_APP_IMAGE={_OLD_APP_IMAGE}\n" in rollback
    assert f"ROLLBACK_OCR_IMAGE={_OLD_OCR_IMAGE}\n" in rollback
    assert f"ROLLBACK_QDRANT_IMAGE={_OLD_QDRANT_IMAGE}\n" in rollback
    assert "ROLLBACK_WORKER_EXISTS=false\n" in rollback


@pytest.mark.parametrize("missing_path", ("active", "current"))
def test_degraded_requires_both_active_env_and_current(
    tmp_path: Path,
    missing_path: str,
) -> None:
    sandbox = _prepare_sandbox(
        tmp_path,
        worker_exists=False,
        worker_running=False,
    )
    _remove_core_runtime(sandbox)
    if missing_path == "active":
        sandbox.active_env.unlink()
    else:
        sandbox.current_link.unlink()
    original_active = (
        sandbox.active_env.read_bytes()
        if sandbox.active_env.exists()
        else None
    )

    completed = _run_deploy(sandbox)

    assert completed.returncode != 0
    if original_active is None:
        assert not sandbox.active_env.exists()
    else:
        assert sandbox.active_env.read_bytes() == original_active
    _assert_no_runtime_mutation(sandbox)


def test_degraded_worker_image_must_equal_old_app_image(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    _remove_core_runtime(sandbox)
    state = _state(sandbox)
    state["WORKER_IMAGE"] = "sha256:" + "9" * 64
    sandbox.state_file.write_text(
        "".join(f"{key}={value}\n" for key, value in state.items()),
        encoding="ascii",
    )

    completed = _run_deploy(sandbox)

    assert completed.returncode != 0
    assert sandbox.current_link.resolve() == sandbox.old_release
    assert sandbox.active_env.read_text(
        encoding="utf-8",
    ) == sandbox.original_active
    _assert_no_runtime_mutation(sandbox)


def test_old_release_is_reverified_before_load(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(sandbox, FAKE_OLD_VERIFY_FAIL="1")

    assert completed.returncode != 0
    log = _command_log(sandbox)
    assert "verify-old-release" in log
    _assert_no_runtime_mutation(sandbox)


def test_failure_reverifies_old_release_before_compensation(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(sandbox, FAKE_LOAD_FAIL="1")

    assert completed.returncode == 1
    log = _command_log(sandbox)
    assert log.count("verify-old-release") == 2
    assert log.rindex("verify-old-release") > log.index("docker load ")
    _assert_old_runtime_restored(sandbox)


def test_starting_health_reaches_healthy_before_commit(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(
        sandbox,
        FAKE_QDRANT_HEALTH="starting_then_healthy",
    )

    assert completed.returncode == 0, completed.stderr
    assert sandbox.current_link.resolve() == sandbox.new_release


def test_delayed_qdrant_readyz_succeeds_before_metadata_commit(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(
        sandbox,
        FAKE_NEW_QDRANT_READY_AT_SECONDS="3",
    )

    assert completed.returncode == 0, completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 3
    assert sandbox.current_link.resolve() == sandbox.new_release
    assert _command_log(sandbox).count("docker exec rag-app") == 4


@pytest.mark.parametrize(
    "ready_mode",
    ("connection_error", "non_200", "timeout"),
)
def test_qdrant_readyz_failure_times_out_and_compensates(
    tmp_path: Path,
    ready_mode: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(
        sandbox,
        FAKE_NEW_QDRANT_READYZ=ready_mode,
    )

    assert completed.returncode == 1
    assert "DEPLOY_FAILED_RECOVERED" in completed.stderr
    assert "sensitive-qdrant-response-body" not in completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 60
    assert _command_log(sandbox).count("docker exec rag-app") >= 61
    _assert_old_runtime_restored(sandbox)


def test_qdrant_disappearance_during_readyz_fails_immediately(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(
        sandbox,
        FAKE_NEW_QDRANT_READYZ="disappear",
    )

    assert completed.returncode == 1
    assert "DEPLOY_FAILED_RECOVERED" in completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 0
    _assert_old_runtime_restored(sandbox)


def test_qdrant_readyz_command_does_not_expose_secret_or_body(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(
        sandbox,
        FAKE_NEW_QDRANT_READYZ="non_200",
    )

    combined_output = completed.stdout + completed.stderr
    assert "deploy-qdrant-secret" not in combined_output
    assert "sensitive-qdrant-response-body" not in combined_output
    assert "deploy-qdrant-secret" not in _command_log(sandbox)


@pytest.mark.parametrize("healthy_at_seconds", (31, 90, 210))
def test_delayed_ocr_health_succeeds_within_240_second_deadline(
    tmp_path: Path,
    healthy_at_seconds: int,
) -> None:
    """证明 OCR 在 Compose 启动窗口内延迟健康仍可部署。

    Args:
        tmp_path: pytest 临时目录。
        healthy_at_seconds: OCR 首次返回 healthy 的伪时钟秒数。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(
        sandbox,
        FAKE_OCR_HEALTHY_AT_SECONDS=str(healthy_at_seconds),
    )

    assert completed.returncode == 0, completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) >= (
        healthy_at_seconds
    )
    assert sandbox.current_link.resolve() == sandbox.new_release


def test_ocr_starting_times_out_at_240_seconds_and_compensates(
    tmp_path: Path,
) -> None:
    """证明 OCR 超时使用 240 秒 deadline 且恢复旧运行态。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(sandbox, FAKE_OCR_HEALTH="starting")

    assert completed.returncode == 1
    assert "DEPLOY_FAILED_RECOVERED" in completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 240
    _assert_old_runtime_restored(sandbox)


@pytest.mark.parametrize("health", ("unhealthy", "no_health", "disappear"))
def test_invalid_ocr_health_fails_immediately_and_compensates(
    tmp_path: Path,
    health: str,
) -> None:
    """证明终止状态、缺失 health 和容器消失都立即失败。

    Args:
        tmp_path: pytest 临时目录。
        health: fake OCR health 故障。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(sandbox, FAKE_OCR_HEALTH=health)

    assert completed.returncode == 1
    assert "DEPLOY_FAILED_RECOVERED" in completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 0
    _assert_old_runtime_restored(sandbox)


@pytest.mark.parametrize(
    "failure",
    (
        "FAKE_LOAD_FAIL",
        "FAKE_BAD_LOADED_APP_ID",
        "FAKE_BAD_LOADED_OCR_ID",
        "FAKE_BAD_LOADED_QDRANT_ID",
        "FAKE_CORE_UP_PARTIAL_FAIL",
        "FAKE_PS_FAIL",
        "FAKE_QDRANT_HEALTH",
        "FAKE_OCR_HEALTH",
        "FAKE_APP_HEALTH",
        "FAKE_NEW_CURL_FAIL",
        "FAKE_ACTIVE_ENV_REPLACE_FAIL",
        "FAKE_CURRENT_RENAME_FAIL",
        "FAKE_ROLLBACK_PUBLISH_FAIL",
    ),
)
def test_failure_restores_old_core_worker_env_and_current(
    tmp_path: Path,
    failure: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    value = "unhealthy" if failure.endswith("_HEALTH") else "1"
    completed = _run_deploy(sandbox, **{failure: value})

    assert completed.returncode != 0
    assert "DEPLOY_FAILED_RECOVERED" in completed.stderr
    _assert_old_runtime_restored(sandbox)


def test_absent_worker_is_not_created(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(
        tmp_path,
        worker_exists=False,
        worker_running=False,
    )

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    state = _state(sandbox)
    assert state["WORKER_EXISTS"] == "false"
    assert state["WORKER_RUNNING"] == "false"


def test_stopped_worker_remains_stopped_and_is_recorded(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path, worker_running=False)

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert _state(sandbox)["WORKER_RUNNING"] == "false"
    rollback = (
        sandbox.root / "shared/env/rollback-images.env"
    ).read_text(encoding="utf-8")
    assert "ROLLBACK_WORKER_WAS_RUNNING=false\n" in rollback


def test_running_worker_without_core_is_stopped_on_success(
    tmp_path: Path,
) -> None:
    """证明孤立运行 worker 不会与新核心同时运行。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)
    _remove_core_runtime(sandbox)

    completed = _run_deploy(sandbox)

    assert completed.returncode == 0, completed.stderr
    state = _state(sandbox)
    assert state["APP_IMAGE"] == _NEW_APP_IMAGE
    assert state["OCR_IMAGE"] == _NEW_OCR_IMAGE
    assert state["QDRANT_IMAGE"] == _NEW_QDRANT_IMAGE
    assert state["WORKER_RUNNING"] == "false"
    assert sandbox.current_link.resolve() == sandbox.new_release
    rollback = sandbox.root.joinpath(
        "shared/env/rollback-images.env",
    ).read_text(encoding="utf-8")
    assert f"ROLLBACK_APP_IMAGE={_OLD_APP_IMAGE}\n" in rollback
    assert "ROLLBACK_WORKER_EXISTS=true\n" in rollback
    assert f"ROLLBACK_WORKER_IMAGE={_OLD_APP_IMAGE}\n" in rollback


def test_running_worker_without_core_is_restored_after_failure(
    tmp_path: Path,
) -> None:
    """证明孤立运行 worker 在部署失败后恢复且核心仍不存在。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)
    _remove_core_runtime(sandbox)

    completed = _run_deploy(sandbox, FAKE_LOAD_FAIL="1")

    assert completed.returncode == 1
    assert "DEPLOY_FAILED_RECOVERED" in completed.stderr
    state = _state(sandbox)
    assert state["APP_EXISTS"] == "false"
    assert state["OCR_EXISTS"] == "false"
    assert state["QDRANT_EXISTS"] == "false"
    assert state["WORKER_EXISTS"] == "true"
    assert state["WORKER_RUNNING"] == "true"
    assert state["WORKER_IMAGE"] == _OLD_APP_IMAGE
    assert sandbox.current_link.resolve() == sandbox.old_release


def test_recovery_failure_uses_stable_exit_and_category(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_deploy(
        sandbox,
        FAKE_LOAD_FAIL="1",
        FAKE_CURL_FAIL="1",
    )

    assert completed.returncode == 70
    assert "DEPLOY_FAILED_RECOVERY_FAILED" in completed.stderr
    assert "DEPLOY_FAILED_RECOVERED\n" not in completed.stderr


def test_incomplete_core_set_is_rejected_before_load(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    state = _state(sandbox)
    state["OCR_EXISTS"] = "false"
    sandbox.state_file.write_text(
        "".join(f"{key}={value}\n" for key, value in state.items()),
        encoding="ascii",
    )

    completed = _run_deploy(sandbox)

    assert completed.returncode != 0
    log = _command_log(sandbox)
    assert "docker load " not in log


@pytest.mark.parametrize(
    "revision",
    ("", "2" * 12, "A" * 40, "0.1.0", "3" * 40),
)
def test_revision_mismatch_fails_before_load_or_compose_up(
    tmp_path: Path,
    revision: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    lines = [
        line
        for line in sandbox.env_file.read_text(
            encoding="utf-8"
        ).splitlines()
        if not line.startswith("RAG_RELEASE_REVISION=")
    ]
    if revision:
        lines.append(f"RAG_RELEASE_REVISION={revision}")
    sandbox.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    completed = _run_deploy(sandbox)

    assert completed.returncode != 0
    log = _command_log(sandbox)
    assert "docker load " not in log
    assert " up -d " not in log
