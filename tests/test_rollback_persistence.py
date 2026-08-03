from __future__ import annotations

import base64
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_APP_IMAGE = "sha256:" + "a" * 64
_OCR_IMAGE = "sha256:" + "b" * 64
_QDRANT_IMAGE = "sha256:" + "c" * 64
_APP_CONFIG = "sha256:" + "3" * 64
_OCR_CONFIG = "sha256:" + "4" * 64
_QDRANT_CONFIG = "sha256:" + "5" * 64
_QDRANT_REGISTRY_DIGEST = (
    "sha256:0bd98fa7977f1e75694779359ca4e212"
    "822e5a71334e28421182f72f209d5286"
)
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
    clock_file: Path
    original_env: str
    original_rollback: str
    rollback_images: tuple[str, str, str]
    store_mode: str


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _copy_identity_helper(target: Path) -> None:
    source = (
        Path(__file__).parents[1]
        / "scripts/docker_archive_loaded_identity.py"
    )
    target.parent.mkdir()
    target.write_text(
        source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _prepare_sandbox(
    tmp_path: Path,
    *,
    store_mode: str = "containerd",
) -> _Sandbox:
    """创建 rollback 集成沙箱。

    Args:
        tmp_path: pytest 临时目录。
        store_mode: 模拟 `containerd` 或 `classic` image store。

    Returns:
        完整 rollback 测试沙箱。

    """
    if store_mode not in {"containerd", "classic"}:
        raise ValueError("store_mode 无效。")
    rollback_images = (
        (_APP_IMAGE, _OCR_IMAGE, _QDRANT_IMAGE)
        if store_mode == "containerd"
        else (_APP_CONFIG, _OCR_CONFIG, _QDRANT_CONFIG)
    )
    rollback_app, rollback_ocr, rollback_qdrant = rollback_images
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
        "RAG_QDRANT_API_KEY=rollback-qdrant-secret\n"
        "CUSTOM_SETTING=keep-value\n"
    )
    env_file.write_text(original_env, encoding="utf-8")
    env_file.chmod(0o600)
    rollback_file = shared_env / "rollback-images.env"
    rollback_env = (
        original_env.replace("sha256:" + "d" * 64, rollback_app)
        .replace("sha256:" + "e" * 64, rollback_ocr)
        .replace("sha256:" + "f" * 64, rollback_qdrant)
        .replace("2" * 40, _SOURCE_REVISION)
    )
    rollback_env_bytes = rollback_env.encode()
    original_rollback = (
        "ROLLBACK_SCHEMA_VERSION=2\n"
        f"ROLLBACK_RELEASE_DIR={old_release}\n"
        f"ROLLBACK_APP_IMAGE={rollback_app}\n"
        f"ROLLBACK_OCR_IMAGE={rollback_ocr}\n"
        f"ROLLBACK_QDRANT_IMAGE={rollback_qdrant}\n"
        "ROLLBACK_WORKER_EXISTS=true\n"
        "ROLLBACK_WORKER_WAS_RUNNING=false\n"
        f"ROLLBACK_WORKER_IMAGE={rollback_app}\n"
        f"ROLLBACK_SOURCE_REVISION={_SOURCE_REVISION}\n"
        "ROLLBACK_ENV_SHA256="
        f"{hashlib.sha256(rollback_env_bytes).hexdigest()}\n"
        "ROLLBACK_ENV_BASE64="
        f"{base64.b64encode(rollback_env_bytes).decode()}\n"
    )
    rollback_file.write_text(original_rollback, encoding="utf-8")
    rollback_file.chmod(0o600)
    (old_release / "verify-offline.sh").write_text(
        "#!/usr/bin/env bash\n"
        "printf 'verify-rollback-target\\n' >> \"${FAKE_DOCKER_LOG}\"\n"
        "exit 0\n",
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
        f"qdrant/qdrant:v1.18.3@{_QDRANT_REGISTRY_DIGEST}\n",
        encoding="ascii",
    )
    (old_release / "IMAGE_ARCHIVES.tsv").write_text(
        "images/docx-rag-linux-amd64.tar\tapp\t"
        f"{_APP_IMAGE}\t{_SOURCE_REVISION}\t"
        f"{_APP_CONFIG}\tlinux/amd64\n"
        "images/docx-rag-ocr-linux-amd64.tar\tocr\t"
        f"{_OCR_IMAGE}\t{_SOURCE_REVISION}\t"
        f"{_OCR_CONFIG}\tlinux/amd64\n"
        "images/qdrant-linux-amd64.tar\tqdrant\t"
        f"{_QDRANT_IMAGE}\tqdrant/qdrant@{_QDRANT_REGISTRY_DIGEST}\t"
        f"{_QDRANT_CONFIG}\tlinux/amd64\n",
        encoding="ascii",
    )
    (new_release / "compose.yaml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (new_release / "SOURCE_REVISION").write_text(
        f"{'2' * 40}\n",
        encoding="ascii",
    )
    (new_release / "verify-offline.sh").write_text(
        "#!/usr/bin/env bash\n"
        "printf 'verify-original-release\\n' >> \"${FAKE_DOCKER_LOG}\"\n"
        "[[ \"${FAKE_ORIGINAL_VERIFY_FAIL:-0}\" != \"1\" ]]\n",
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
    policy_source = (
        Path(__file__).parents[1] / "deployment/qdrant-policy.sh"
    )
    (tmp_path / "qdrant-policy.sh").write_text(
        policy_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    helper_target = tmp_path / "scripts/docker_archive_loaded_identity.py"
    _copy_identity_helper(helper_target)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker_log = tmp_path / "docker.log"
    state_file = tmp_path / "container-state.env"
    clock_file = tmp_path / "clock"
    clock_file.write_text("0\n", encoding="ascii")
    state_file.write_text(
        "APP_EXISTS=true\n"
        f"APP_IMAGE={'sha256:' + 'd' * 64}\n"
        "OCR_EXISTS=true\n"
        f"OCR_IMAGE={'sha256:' + 'e' * 64}\n"
        "QDRANT_EXISTS=true\n"
        f"QDRANT_IMAGE={'sha256:' + 'f' * 64}\n"
        f"WORKER_IMAGE={'sha256:' + 'd' * 64}\n"
        "APP_RUNNING=true\n"
        "OCR_RUNNING=true\n"
        "QDRANT_RUNNING=true\n"
        "WORKER_EXISTS=true\n"
        "WORKER_RUNNING=false\n",
        encoding="ascii",
    )
    _write_executable(
        binaries / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG}"
source "${FAKE_STATE_FILE}"
classic_inspect_format='[{"Architecture":"amd64",'
classic_inspect_format+='"Config":{"Labels":{'
classic_inspect_format+='"org.opencontainers.image.revision":"%s"}},'
classic_inspect_format+='"Id":"%s","Os":"linux"}]'
containerd_inspect_format='[{"Architecture":"amd64",'
containerd_inspect_format+='"Config":{"Labels":{'
containerd_inspect_format+='"org.opencontainers.image.revision":"%s"}},'
containerd_inspect_format+='"Descriptor":{"digest":"%s"},'
containerd_inspect_format+='"Id":"%s","Os":"linux"}]'
write_state() {
  {
    printf 'APP_EXISTS=%q\n' "${APP_EXISTS}"
    printf 'APP_IMAGE=%q\n' "${APP_IMAGE}"
    printf 'OCR_EXISTS=%q\n' "${OCR_EXISTS}"
    printf 'OCR_IMAGE=%q\n' "${OCR_IMAGE}"
    printf 'QDRANT_EXISTS=%q\n' "${QDRANT_EXISTS}"
    printf 'QDRANT_IMAGE=%q\n' "${QDRANT_IMAGE}"
    printf 'WORKER_IMAGE=%q\n' "${WORKER_IMAGE}"
    printf 'APP_RUNNING=%q\n' "${APP_RUNNING}"
    printf 'OCR_RUNNING=%q\n' "${OCR_RUNNING}"
    printf 'QDRANT_RUNNING=%q\n' "${QDRANT_RUNNING}"
    printf 'WORKER_EXISTS=%q\n' "${WORKER_EXISTS}"
    printf 'WORKER_RUNNING=%q\n' "${WORKER_RUNNING}"
  } > "${FAKE_STATE_FILE}"
}
if [[ "$1 $2" == "image inspect" ]]; then
  image="${@: -1}"
  if [[ "${FAKE_MISSING_IMAGE:-}" == "${image}" ]]; then
    exit 41
  fi
  if [[ "$#" == "3" ]]; then
    case "${image}" in
      "${FAKE_APP_IMAGE}") config="${FAKE_APP_CONFIG}" ;;
      "${FAKE_OCR_IMAGE}") config="${FAKE_OCR_CONFIG}" ;;
      "${FAKE_QDRANT_IMAGE}") config="${FAKE_QDRANT_CONFIG}" ;;
      *) exit 41 ;;
    esac
    revision="${FAKE_SOURCE_REVISION}"
    if [[ "${FAKE_BAD_REVISION:-0}" == "1" \
      && "${image}" == "${FAKE_APP_IMAGE}" ]]; then
      revision="$(printf '%040d' 9)"
    fi
    descriptor_id="${image}"
    if [[ "${FAKE_BAD_DESCRIPTOR_IMAGE:-}" == "${image}" ]]; then
      descriptor_id="sha256:$(printf '%064d' 8)"
    fi
    if [[ "${FAKE_STORE_MODE}" == "classic" ]]; then
      printf "${classic_inspect_format}\n" \
        "${revision}" "${image}"
    else
      printf "${containerd_inspect_format}\n" \
        "${revision}" "${descriptor_id}" "${image}"
    fi
  elif [[ "$*" == *"json .Descriptor"* ]]; then
    descriptor_id="${image}"
    if [[ "${FAKE_BAD_DESCRIPTOR_IMAGE:-}" == "${image}" ]]; then
      descriptor_id="sha256:$(printf '%064d' 8)"
    fi
    printf '{"digest":"%s"}\n' "${descriptor_id}"
  elif [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
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
    case "${container}" in
      rag-app) [[ "${APP_EXISTS}" == "true" ]] ;;
      rag-ocr) [[ "${OCR_EXISTS}" == "true" ]] ;;
      rag-qdrant) [[ "${QDRANT_EXISTS}" == "true" ]] ;;
      rag-worker) [[ "${WORKER_EXISTS}" == "true" ]] ;;
      *) exit 42 ;;
    esac
    exit
  fi
  if [[ "$*" == *".State.Running"* ]]; then
    case "${container}" in
      rag-app)
        [[ "${APP_EXISTS}" == "true" ]] || exit 42
        echo "${APP_RUNNING}"
        ;;
      rag-ocr)
        [[ "${OCR_EXISTS}" == "true" ]] || exit 42
        echo "${OCR_RUNNING}"
        ;;
      rag-qdrant)
        [[ "${QDRANT_EXISTS}" == "true" ]] || exit 42
        echo "${QDRANT_RUNNING}"
        ;;
      rag-worker)
        [[ "${WORKER_EXISTS}" == "true" ]] || exit 42
        echo "${WORKER_RUNNING}"
        ;;
      *) exit 42 ;;
    esac
    exit 0
  fi
  if [[ "$*" == *".State.Health.Status"* ]]; then
    case "${container}" in
      rag-app)
        [[ "${APP_EXISTS}" == "true" ]] || exit 42
        if [[ "${APP_IMAGE}" == "${FAKE_APP_IMAGE}" ]]; then
          echo "${FAKE_TARGET_APP_HEALTH:-healthy}"
        else
          echo healthy
        fi
        ;;
      rag-ocr)
        [[ "${OCR_EXISTS}" == "true" ]] || exit 42
        if [[ "${OCR_IMAGE}" == "${FAKE_OCR_IMAGE}" ]]; then
          if [[ -n "${FAKE_TARGET_OCR_HEALTHY_AT_SECONDS:-}" ]]; then
            elapsed="$(cat "${FAKE_CLOCK_FILE}")"
            if ((elapsed >= FAKE_TARGET_OCR_HEALTHY_AT_SECONDS)); then
              echo healthy
            else
              echo starting
            fi
          else
            echo "${FAKE_TARGET_OCR_HEALTH:-healthy}"
          fi
        else
          echo healthy
        fi
        ;;
      rag-qdrant)
        [[ "${QDRANT_EXISTS}" == "true" ]] || exit 42
        if [[ "${QDRANT_IMAGE}" == "${FAKE_QDRANT_IMAGE}" ]]; then
          echo "${FAKE_TARGET_QDRANT_HEALTH:-healthy}"
        else
          echo healthy
        fi
        ;;
      *) exit 42 ;;
    esac
    exit 0
  fi
  case "${container}" in
    rag-app)
      [[ "${APP_EXISTS}" == "true" ]] || exit 42
      actual="${APP_IMAGE}"
      ;;
    rag-ocr)
      [[ "${OCR_EXISTS}" == "true" ]] || exit 42
      actual="${OCR_IMAGE}"
      ;;
    rag-qdrant)
      [[ "${QDRANT_EXISTS}" == "true" ]] || exit 42
      actual="${QDRANT_IMAGE}"
      ;;
    rag-worker)
      [[ "${WORKER_EXISTS}" == "true" ]] || exit 42
      actual="${WORKER_IMAGE}"
      ;;
    *) exit 42 ;;
  esac
  if [[ "${FAKE_BAD_CONTAINER:-}" == "${container}" \
    && "${APP_IMAGE}" == "${FAKE_APP_IMAGE}" ]]; then
    actual="sha256:$(printf '%064d' 0)"
  fi
  printf '%s\n' "${actual}"
  exit 0
fi
if [[ "$1 $2" == "exec rag-app" ]]; then
  [[ "${APP_EXISTS}" == "true" \
    && "${APP_RUNNING}" == "true" \
    && "${QDRANT_EXISTS}" == "true" \
    && "${QDRANT_RUNNING}" == "true" ]] || exit 45
  if [[ "${QDRANT_IMAGE}" == "${FAKE_QDRANT_IMAGE}" ]]; then
    if [[ -n "${FAKE_TARGET_QDRANT_READY_AT_SECONDS:-}" ]]; then
      elapsed="$(cat "${FAKE_CLOCK_FILE}")"
      if ((elapsed >= FAKE_TARGET_QDRANT_READY_AT_SECONDS)); then
        exit 0
      fi
      exit 1
    fi
    mode="${FAKE_TARGET_QDRANT_READYZ:-ready}"
  else
    mode="${FAKE_ORIGINAL_QDRANT_READYZ:-ready}"
  fi
  case "${mode}" in
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
    *) exit 46 ;;
  esac
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
  if [[ "$*" == *" stop "* ]]; then
    for service in rag-app rag-ocr rag-qdrant rag-worker; do
      if [[ "$*" == *" ${service}"* ]]; then
        case "${service}" in
          rag-app) APP_RUNNING=false ;;
          rag-ocr) OCR_RUNNING=false ;;
          rag-qdrant) QDRANT_RUNNING=false ;;
          rag-worker) WORKER_RUNNING=false ;;
        esac
      fi
    done
    write_state
    exit 0
  fi
  if [[ "$*" == *" up -d "* ]]; then
    app="$(awk -F= '$1 == "RAG_APP_IMAGE" {print $2}' "${env_file}")"
    ocr="$(awk -F= '$1 == "RAG_OCR_IMAGE" {print $2}' "${env_file}")"
    qdrant="$(awk -F= '$1 == "RAG_QDRANT_IMAGE" {print $2}' "${env_file}")"
    if [[ "$*" == *" rag-app"* ]]; then
      APP_EXISTS=true
      APP_IMAGE="${app}"
      APP_RUNNING=true
    fi
    if [[ "$*" == *" rag-ocr"* ]]; then
      OCR_EXISTS=true
      OCR_IMAGE="${ocr}"
      OCR_RUNNING=true
    fi
    if [[ "$*" == *" rag-qdrant"* ]]; then
      QDRANT_EXISTS=true
      QDRANT_IMAGE="${qdrant}"
      QDRANT_RUNNING=true
    fi
    if [[ "$*" == *"rag-worker"* ]]; then
      WORKER_EXISTS=true
      WORKER_IMAGE="${app}"
      WORKER_RUNNING=true
    fi
    write_state
    if [[ "${FAKE_COMPOSE_UP_FAIL:-0}" == "1" \
      && "${app}" == "${FAKE_APP_IMAGE}" ]]; then
      exit 43
    fi
    exit 0
  fi
fi
if [[ "$1 $2 $3" == "container rm -f" ]]; then
  case "${@: -1}" in
    rag-app) APP_EXISTS=false; APP_RUNNING=false ;;
    rag-ocr) OCR_EXISTS=false; OCR_RUNNING=false ;;
    rag-qdrant) QDRANT_EXISTS=false; QDRANT_RUNNING=false ;;
    rag-worker)
      WORKER_EXISTS=false
      WORKER_RUNNING=false
      WORKER_IMAGE=
      ;;
    *) exit 44 ;;
  esac
  write_state
  exit 0
fi
exit 44
""",
    )
    _write_executable(
        binaries / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "${FAKE_DOCKER_LOG}"
source "${FAKE_STATE_FILE}"
if [[ "${FAKE_TARGET_CURL_FAIL:-0}" == "1" \
  && "${APP_IMAGE}" == "${FAKE_APP_IMAGE}" ]]; then
  exit 1
fi
if [[ "${FAKE_CURL_FAIL_ONCE:-0}" == "1" \
  && ! -e "${FAKE_CURL_COUNT}" ]]; then
  : > "${FAKE_CURL_COUNT}"
  exit 1
fi
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
        clock_file=clock_file,
        original_env=original_env,
        original_rollback=original_rollback,
        rollback_images=rollback_images,
        store_mode=store_mode,
    )


def _run_rollback(
    sandbox: _Sandbox,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    state = sandbox.state_file.read_text(encoding="ascii")
    if overrides.get("FAKE_WORKER_EXISTS") == "0":
        state = state.replace("WORKER_EXISTS=true", "WORKER_EXISTS=false")
    if overrides.get("FAKE_WORKER_RUNNING") == "1":
        state = state.replace("WORKER_RUNNING=false", "WORKER_RUNNING=true")
    sandbox.state_file.write_text(state, encoding="ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{sandbox.script.parent / 'bin'}:/usr/bin:/bin",
            "FAKE_DOCKER_LOG": str(sandbox.docker_log),
            "FAKE_STATE_FILE": str(sandbox.state_file),
            "FAKE_CLOCK_FILE": str(sandbox.clock_file),
            "FAKE_CURL_COUNT": str(
                sandbox.script.parent / "curl-failure-used",
            ),
            "FAKE_ENV_FILE": str(sandbox.env_file),
            "FAKE_CURRENT_LINK": str(sandbox.current_link),
            "FAKE_APP_IMAGE": sandbox.rollback_images[0],
            "FAKE_APP_CONFIG": _APP_CONFIG,
            "FAKE_OCR_IMAGE": sandbox.rollback_images[1],
            "FAKE_OCR_CONFIG": _OCR_CONFIG,
            "FAKE_QDRANT_IMAGE": sandbox.rollback_images[2],
            "FAKE_QDRANT_CONFIG": _QDRANT_CONFIG,
            "FAKE_SOURCE_REVISION": _SOURCE_REVISION,
            "FAKE_STORE_MODE": sandbox.store_mode,
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
        'bash "${rollback_release}/verify-offline.sh"',
        'bash "${original_release}/verify-offline.sh"',
        "SOURCE_REVISION",
        "ROLLBACK_ENV_SHA256",
        "ROLLBACK_ENV_BASE64",
        "docker compose",
        "config -q",
        "docker_archive_loaded_identity.py",
    ):
        assert required in script


def test_rollback_atomically_persists_only_release_image_keys() -> None:
    script = _rollback_script()

    assert "rag.env.rollback-new" in script
    assert "rag.env.rollback-original" in script
    assert "chmod 0600" in script
    assert "RAG_RELEASE_REVISION" in script
    assert 'mv -T "${active_new}" "${active_env}"' in script
    assert "sed -i" not in script


def test_classic_store_rolls_back_with_config_digest_identity(
    tmp_path: Path,
) -> None:
    """证明 classic store 使用 config digest 完成回滚。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path, store_mode="classic")

    completed = _run_rollback(sandbox)

    assert completed.returncode == 0, completed.stderr
    state = sandbox.state_file.read_text(encoding="ascii")
    assert f"APP_IMAGE={_APP_CONFIG}\n" in state
    assert f"OCR_IMAGE={_OCR_CONFIG}\n" in state
    assert f"QDRANT_IMAGE={_QDRANT_CONFIG}\n" in state


def test_original_release_verify_failure_prevents_runtime_mutation(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_ORIGINAL_VERIFY_FAIL="1",
    )

    assert completed.returncode != 0
    _assert_original_metadata(sandbox)
    log = sandbox.docker_log.read_text(encoding="utf-8")
    assert "verify-rollback-target" in log
    assert "verify-original-release" in log
    assert " up -d " not in log


def test_rollback_worker_image_must_equal_old_app_before_up(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    mismatched_image = "sha256:" + "8" * 64
    sandbox.rollback_file.write_text(
        sandbox.original_rollback.replace(
            f"ROLLBACK_WORKER_IMAGE={_APP_IMAGE}",
            f"ROLLBACK_WORKER_IMAGE={mismatched_image}",
        ),
        encoding="utf-8",
    )
    mismatched_rollback = sandbox.rollback_file.read_bytes()

    completed = _run_rollback(sandbox)

    assert completed.returncode != 0
    assert sandbox.env_file.read_text(
        encoding="utf-8",
    ) == sandbox.original_env
    assert sandbox.current_link.resolve() == sandbox.new_release
    assert sandbox.rollback_file.read_bytes() == mismatched_rollback
    log = sandbox.docker_log.read_text(encoding="utf-8")
    assert " up -d " not in log


def test_rollback_preserves_worker_state_and_compensates_metadata() -> None:
    script = _rollback_script()

    assert ".State.Running" in script
    assert "--profile index" in script
    assert "restore_original_runtime" in script
    assert "restore_original_metadata" in script
    assert "verify_rollback_target" in script
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
    assert log.count(" up -d --no-build --pull never") == 1
    up_calls = [line for line in log.splitlines() if " up -d " in line]
    assert len(up_calls) == 1
    assert "--profile index" in up_calls[0]
    assert "rag-worker" in up_calls[0]
    assert " stop rag-worker" in log
    assert (
        sandbox.rollback_file.read_text(encoding="utf-8")
        == sandbox.original_rollback
    )


def test_running_worker_is_restored_with_old_app_image(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    sandbox.rollback_file.write_text(
        sandbox.original_rollback.replace(
            "ROLLBACK_WORKER_WAS_RUNNING=false",
            "ROLLBACK_WORKER_WAS_RUNNING=true",
        ),
        encoding="utf-8",
    )

    completed = _run_rollback(sandbox, FAKE_WORKER_RUNNING="1")

    assert completed.returncode == 0, completed.stderr
    log = sandbox.docker_log.read_text(encoding="utf-8")
    assert "--profile index up -d --no-build --pull never" in log
    state = sandbox.state_file.read_text(encoding="utf-8")
    assert f"WORKER_IMAGE={_APP_IMAGE}" in state


def test_rollback_waits_for_ocr_beyond_30_seconds(tmp_path: Path) -> None:
    """证明 rollback 与 deploy 共用足够长的 OCR 等待窗口。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_TARGET_OCR_HEALTHY_AT_SECONDS="210",
    )

    assert completed.returncode == 0, completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) >= 210
    assert sandbox.current_link.resolve() == sandbox.old_release


def test_rollback_waits_for_delayed_qdrant_readyz(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_TARGET_QDRANT_READY_AT_SECONDS="3",
    )

    assert completed.returncode == 0, completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 3
    assert sandbox.current_link.resolve() == sandbox.old_release
    assert (
        sandbox.docker_log.read_text(encoding="utf-8").count(
            "exec rag-app",
        )
        == 4
    )


@pytest.mark.parametrize(
    "ready_mode",
    ("connection_error", "non_200", "timeout"),
)
def test_rollback_readyz_failure_times_out_and_checks_compensation(
    tmp_path: Path,
    ready_mode: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_TARGET_QDRANT_READYZ=ready_mode,
    )

    assert completed.returncode == 1
    assert "ROLLBACK_FAILED_RECOVERED" in completed.stderr
    assert "sensitive-qdrant-response-body" not in completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 60
    assert (
        sandbox.docker_log.read_text(encoding="utf-8").count(
            "exec rag-app",
        )
        >= 61
    )
    assert (
        sandbox.docker_log.read_text(encoding="utf-8").count(
            "verify-original-release",
        )
        == 2
    )
    _assert_original_metadata(sandbox)


def test_rollback_qdrant_disappearance_fails_without_metadata_commit(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_TARGET_QDRANT_READYZ="disappear",
    )

    assert completed.returncode == 1
    assert "ROLLBACK_FAILED_RECOVERED" in completed.stderr
    assert int(sandbox.clock_file.read_text(encoding="ascii")) == 0
    _assert_original_metadata(sandbox)


def test_rollback_readyz_does_not_expose_secret_or_body(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_TARGET_QDRANT_READYZ="non_200",
    )

    combined_output = completed.stdout + completed.stderr
    assert "rollback-qdrant-secret" not in combined_output
    assert "sensitive-qdrant-response-body" not in combined_output
    assert "rollback-qdrant-secret" not in sandbox.docker_log.read_text(
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    (
        ("verify", ""),
        ("missing_image", ""),
        ("descriptor_mismatch", ""),
        ("qdrant_policy", "批准白名单"),
        ("bad_revision", "revision"),
        ("compose_up", "ROLLBACK_FAILED_RECOVERED"),
        ("container_image", "ROLLBACK_FAILED_RECOVERED"),
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
    elif failure_mode == "descriptor_mismatch":
        overrides["FAKE_BAD_DESCRIPTOR_IMAGE"] = _APP_IMAGE
    elif failure_mode == "qdrant_policy":
        fake_digest = "sha256:" + "f" * 64
        (sandbox.old_release / "QDRANT_SOURCE_IMAGE").write_text(
            f"qdrant/qdrant:v1.18.3@{fake_digest}\n",
            encoding="ascii",
        )
        image_manifest_path = sandbox.old_release / "IMAGE_ARCHIVES.tsv"
        image_manifest_path.write_text(
            image_manifest_path.read_text(encoding="ascii").replace(
                _QDRANT_REGISTRY_DIGEST,
                fake_digest,
            ),
            encoding="ascii",
        )
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
    assert "ROLLBACK_FAILED_RECOVERED" in completed.stderr
    _assert_original_metadata(sandbox)
