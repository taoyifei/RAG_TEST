"""简单 Compose 与服务器脚本的专项契约。"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _write_gzip(path: Path, payload: bytes) -> None:
    with gzip.open(path, "wb") as stream:
        stream.write(payload)


def _write_sidecar(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.name}.sha256").write_text(
        f"{digest}  {path.name}\n",
        encoding="ascii",
    )


def _run_script(
    script: Path,
    arguments: list[str],
    *,
    fake_bin: Path,
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(extra_env)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    return subprocess.run(  # noqa: S603
        ["/usr/bin/bash", str(script), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_simple_compose_has_exact_four_service_contract() -> None:
    root = _root()
    docker = shutil.which("docker")
    assert docker is not None
    completed = subprocess.run(  # noqa: S603
        [
            docker,
            "compose",
            "--env-file",
            str(root / "deployment/simple/.env.example"),
            "-f",
            str(root / "deployment/simple/compose.yaml"),
            "--profile",
            "index",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    services = payload["services"]

    assert set(services) == {
        "rag-app",
        "rag-worker",
        "rag-ocr",
        "rag-qdrant",
    }
    assert services["rag-app"]["image"] == services["rag-worker"]["image"]
    assert services["rag-worker"]["profiles"] == ["index"]
    assert services["rag-app"]["environment"]["RAG_RUN_MODE"] == "demo"
    assert services["rag-worker"]["environment"]["RAG_RUN_MODE"] == "demo"
    assert "ports" in services["rag-app"]
    assert all(
        "ports" not in services[name]
        for name in ("rag-worker", "rag-ocr", "rag-qdrant")
    )
    app_mounts = {
        mount["target"]: mount
        for mount in services["rag-app"]["volumes"]
    }
    worker_mounts = {
        mount["target"]: mount for mount in services["rag-worker"]["volumes"]
    }
    assert app_mounts["/data/docs"]["read_only"] is True
    assert worker_mounts["/data/docs"]["read_only"] is True
    assert "/state" in app_mounts and "/state" in worker_mounts
    assert services["rag-qdrant"]["volumes"][0]["target"] == "/qdrant/storage"


def test_deploy_script_loads_modules_and_runs_one_off_full_index(
    tmp_path: Path,
) -> None:
    root = _root()
    package = tmp_path / "package"
    package.mkdir()
    shutil.copyfile(
        root / "deployment/simple/compose.yaml",
        package / "compose.yaml",
    )
    for name in (
        "app-image.tar.gz",
        "ocr-image.tar.gz",
        "qdrant-image.tar.gz",
    ):
        _write_gzip(package / name, name.encode("ascii"))
        _write_sidecar(package / name)
    source_doc = tmp_path / "sample.docx"
    source_doc.write_bytes(b"docx")
    with tarfile.open(package / "corpus.tar.gz", "w:gz") as archive:
        archive.add(source_doc, arcname="sample.docx")
    _write_sidecar(package / "corpus.tar.gz")

    docs = tmp_path / "docs"
    env_file = tmp_path / "rag.env"
    env_file.write_text(
        "\n".join(
            (
                f"RAG_SIMPLE_COMPOSE_FILE={package / 'compose.yaml'}",
                "RAG_APP_IMAGE=docx-rag:test",
                "RAG_OCR_IMAGE=docx-rag-ocr:fixed",
                "RAG_QDRANT_IMAGE=qdrant/qdrant:v1.18.3",
                f"RAG_STATE_PATH={tmp_path / 'state'}",
                f"RAG_QDRANT_PATH={tmp_path / 'qdrant'}",
                f"RAG_DOCS_PATH={docs}",
                f"RAG_LOGS_PATH={tmp_path / 'logs'}",
                "RAG_PORT=8088",
                f"RAG_RELEASE_REVISION={'a' * 40}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    app_image_id = f"sha256:{'a' * 64}"
    compose_payload = json.dumps(
        {
            "name": "rag-simple",
            "services": {
                "rag-app": {
                    "image": "docx-rag:test",
                    "ports": [{"target": 8088, "published": "8088"}],
                },
                "rag-worker": {"image": "docx-rag:test"},
                "rag-ocr": {"image": "docx-rag-ocr:fixed"},
                "rag-qdrant": {"image": "qdrant/qdrant:v1.18.3"},
            },
        },
        separators=(",", ":"),
    )
    _write_executable(
        fake_bin / "docker",
        f"""#!/usr/bin/env bash
log={shlex.quote(str(log))}
printf '%s\n' "$*" >>"${{log}}"
if [[ "$1" == "load" ]]; then cat >/dev/null; exit 0; fi
if [[ "$1" == "run" ]]; then exit 0; fi
if [[ "$1" == "inspect" ]]; then echo healthy; exit 0; fi
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$3" == "--format" && "$4" == *'.Id'* ]]; then
    echo {shlex.quote(app_image_id)}
  elif [[ "$3" == "--format" ]]; then
    echo {'a' * 40}
  fi
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  case "$4" in
    *'.Config.Image'*) echo 'docx-rag:test' ;;
    *'.Image'*) echo {shlex.quote(app_image_id)} ;;
    *'compose.project'*) echo 'rag-simple' ;;
    *'compose.service'*) echo 'rag-app' ;;
    *'.Config.Env'*) echo 'RAG_RELEASE_REVISION={'a' * 40}' ;;
    *'NetworkSettings.Ports'*)
      echo '{{"8088/tcp":[{{"HostIp":"","HostPort":"8088"}}]}}'
      ;;
  esac
  exit 0
fi
if [[ "$1" == "exec" ]]; then exit 0; fi
if [[ "$1" == "compose" && "$*" == *'config --format json'* ]]; then
  [[ -z "${{COMPOSE_PROJECT_NAME+x}}" && -z "${{RAG_PORT+x}}" ]] \
    || exit 93
  printf '%s\n' {shlex.quote(compose_payload)}
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
if [[ "$*" == *'/ready'* ]]; then
  echo '{"ready":true,"run_mode":"demo","production_ready":false}'
else
  echo '{"status":"live"}'
fi
""",
    )

    completed = _run_script(
        root / "deployment/simple/deploy.sh",
        [str(env_file), str(package)],
        fake_bin=fake_bin,
        extra_env={"COMPOSE_PROJECT_NAME": "polluted", "RAG_PORT": "8188"},
    )

    assert completed.returncode == 0, completed.stderr
    assert (docs / "sample.docx").read_bytes() == b"docx"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert any("-p rag-simple" in line for line in calls)
    assert any(
        "up -d --no-build --pull never rag-qdrant rag-ocr" in line
        for line in calls
    )
    assert any(
        "--force-recreate rag-app" in line
        for line in calls
    )
    assert any(
        "--profile index run --rm --no-deps rag-worker index full" in line
        for line in calls
    )


def _prepare_update_case(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    env_file = tmp_path / "rag.env"
    env_file.write_text(
        "\n".join(
            (
                "RAG_APP_IMAGE=docx-rag:old",
                f"RAG_RELEASE_REVISION={'1' * 40}",
                f"RAG_SIMPLE_COMPOSE_FILE={compose}",
                "RAG_PORT=8088",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    archive = tmp_path / "app-image.tar.gz"
    _write_gzip(archive, b"new app")
    _write_sidecar(archive)
    return compose, env_file, archive, archive.with_name(
        "app-image.tar.gz.sha256"
    )


def _write_update_fakes(
    fake_bin: Path,
    *,
    log: Path,
    count_file: Path,
    state_file: Path,
    fail_first_app_up: bool,
) -> None:
    state_file.write_text(
        f"docx-rag:old\n{'1' * 40}\n",
        encoding="ascii",
    )
    old_fingerprint = f"sha256:{'a' * 64}"
    new_fingerprint = f"sha256:{'b' * 64}"
    _write_executable(
        fake_bin / "docker",
        f"""#!/usr/bin/env bash
log={shlex.quote(str(log))}
count_file={shlex.quote(str(count_file))}
state_file={shlex.quote(str(state_file))}
fail_first_app_up={str(fail_first_app_up).lower()}
printf '%s\n' "$*" >>"${{log}}"
if [[ "$1" == "load" ]]; then
  cat >/dev/null
  echo 'Loaded image: docx-rag:new'
  exit 0
fi
if [[ "$1" == "run" ]]; then
  if [[ "$*" == *'docx-rag:old'* ]]; then
    echo '{{"pipeline_fingerprint":"{old_fingerprint}"}}'
  else
    echo '{{"pipeline_fingerprint":"{new_fingerprint}"}}'
  fi
  exit 0
fi
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$3" != "--format" ]]; then exit 0; fi
  image="$5"
  if [[ "$4" == *'.Id'* ]]; then
    if [[ "${{image}}" == 'docx-rag:old' ]]; then
      echo 'sha256:{'1' * 64}'
    else
      echo 'sha256:{'2' * 64}'
    fi
  elif [[ "${{image}}" == 'docx-rag:old' ]]; then
    echo '{'1' * 40}'
  else
    echo '{'2' * 40}'
  fi
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  current_image="$(sed -n '1p' "${{state_file}}")"
  current_revision="$(sed -n '2p' "${{state_file}}")"
  case "$4" in
    *'.Config.Image'*) echo "${{current_image}}" ;;
    *'.Image'*)
      if [[ "${{current_image}}" == 'docx-rag:old' ]]; then
        echo 'sha256:{'1' * 64}'
      else
        echo 'sha256:{'2' * 64}'
      fi ;;
    *'compose.project'*) echo 'rag-simple' ;;
    *'compose.service'*) echo 'rag-app' ;;
    *'.Config.Env'*) echo "RAG_RELEASE_REVISION=${{current_revision}}" ;;
    *'NetworkSettings.Ports'*)
      echo '{{"8088/tcp":[{{"HostIp":"","HostPort":"8088"}}]}}'
      ;;
  esac
  exit 0
fi
if [[ "$1" == "exec" ]]; then exit 0; fi
if [[ "$1" == "compose" ]]; then
  [[ -z "${{COMPOSE_PROJECT_NAME+x}}" && -z "${{RAG_PORT+x}}" ]] \
    || exit 93
  env_file=''
  previous=''
  for argument in "$@"; do
    if [[ "${{previous}}" == '--env-file' ]]; then env_file="${{argument}}"; fi
    previous="${{argument}}"
  done
  image="$(
    awk -F= \
      '$1 == "RAG_APP_IMAGE" {{print substr($0, index($0, "=") + 1)}}' \
      "${{env_file}}"
  )"
  revision="$(
    awk -F= \
      '$1 == "RAG_RELEASE_REVISION" {{print substr($0, index($0, "=") + 1)}}' \
      "${{env_file}}"
  )"
  if [[ "$*" == *'config --format json'* ]]; then
    printf '%s' '{{"name":"rag-simple","services":{{"rag-app":'
    printf '{{"image":"%s",' "${{image}}"
    printf '%s\n' '"ports":[{{"target":8088,"published":"8088"}}]}}}}}}'
    exit 0
  fi
  if [[ "$*" == *' up '* && "$*" == *'rag-app'* ]]; then
    count=0
    [[ -f "${{count_file}}" ]] && count="$(cat "${{count_file}}")"
    count=$((count + 1))
    printf '%s' "${{count}}" >"${{count_file}}"
    if [[ "${{fail_first_app_up}}" == 'true' && "${{count}}" -eq 1 ]]; then
      exit 1
    fi
    printf '%s\n%s\n' "${{image}}" "${{revision}}" >"${{state_file}}"
  fi
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
if [[ "$*" == *'/ready'* ]]; then
  echo '{"ready":true}'
else
  echo '{"status":"live"}'
fi
""",
    )


def test_update_app_changes_only_app_env_and_service(tmp_path: Path) -> None:
    root = _root()
    _, env_file, archive, sidecar = _prepare_update_case(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    count = tmp_path / "compose.count"
    _write_update_fakes(
        fake_bin,
        log=log,
        count_file=count,
        state_file=tmp_path / "container.state",
        fail_first_app_up=False,
    )

    completed = _run_script(
        root / "deployment/simple/update-app.sh",
        [str(archive), str(sidecar), str(env_file)],
        fake_bin=fake_bin,
        extra_env={"COMPOSE_PROJECT_NAME": "polluted", "RAG_PORT": "8188"},
    )

    assert completed.returncode == 0, completed.stderr
    updated = env_file.read_text(encoding="utf-8")
    assert "RAG_APP_IMAGE=docx-rag:new\n" in updated
    assert f"RAG_RELEASE_REVISION={'2' * 40}\n" in updated
    calls = log.read_text(encoding="utf-8")
    assert "rag-ocr" not in calls
    assert "rag-qdrant" not in calls
    assert "rag-app" in calls
    assert "-p rag-simple" in calls
    assert "--force-recreate rag-app" in calls
    assert "REINDEX_REQUIRED" in completed.stdout


def test_update_app_restores_old_env_when_compose_fails(tmp_path: Path) -> None:
    root = _root()
    _, env_file, archive, sidecar = _prepare_update_case(tmp_path)
    original = env_file.read_text(encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    _write_update_fakes(
        fake_bin,
        log=log,
        count_file=tmp_path / "compose.count",
        state_file=tmp_path / "container.state",
        fail_first_app_up=True,
    )

    completed = _run_script(
        root / "deployment/simple/update-app.sh",
        [str(archive), str(sidecar), str(env_file)],
        fake_bin=fake_bin,
        extra_env={},
    )

    assert completed.returncode != 0
    assert env_file.read_text(encoding="utf-8") == original
    assert "已恢复并验证旧 image 和旧 env" in completed.stderr
    recreate_count = log.read_text(encoding="utf-8").count(
        "--force-recreate rag-app"
    )
    assert recreate_count == 2


def test_update_app_rejects_malformed_asset_fingerprint(tmp_path: Path) -> None:
    root = _root()
    _, env_file, archive, sidecar = _prepare_update_case(tmp_path)
    original = env_file.read_text(encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    _write_update_fakes(
        fake_bin,
        log=tmp_path / "docker.log",
        count_file=tmp_path / "compose.count",
        state_file=tmp_path / "container.state",
        fail_first_app_up=False,
    )
    source = docker.read_text(encoding="utf-8").replace(
        f"sha256:{'a' * 64}",
        "not-a-sha256",
        1,
    )
    _write_executable(docker, source)

    completed = _run_script(
        root / "deployment/simple/update-app.sh",
        [str(archive), str(sidecar), str(env_file)],
        fake_bin=fake_bin,
        extra_env={},
    )

    assert completed.returncode != 0
    assert "PIPELINE_FINGERPRINT_INVALID" in completed.stderr
    assert env_file.read_text(encoding="utf-8") == original


def test_deployment_guide_is_the_single_copyable_demo_path() -> None:
    guide_path = _root() / "deployment/simple/DEPLOYMENT_GUIDE.md"
    guide = guide_path.read_text(encoding="utf-8")

    assert len(guide.splitlines()) <= 300
    for required in (
        "scripts/build_simple_bundle.py",
        "scripts/build_app_update.py",
        "rsync -av",
        "scp -r",
        "bash deploy.sh",
        "index full",
        "--restart-worker",
        '"run_mode":"demo"',
        '"production_ready":false',
        "demo 不是 production",
        "user4a@10.242.180.60",
        "RAG_EMBEDDING_GPU_DEVICE_ID=1",
        "RAG_RERANKER_GPU_DEVICE_ID=2",
        "http://10.242.180.57:8000",
        "http://10.242.180.57:8001",
        "http://10.242.180.58:8000",
        "http://10.242.180.58:8001",
        "GPU_COUNT_OK=",
        "DISK_FREE_GIB_OK=",
        "LLM_MODELS_OK=",
        "LLM_CHAT_OK=",
        "RAG_SERVER_PREFLIGHT_OK",
        "RAG_ROOT_PERMISSION_OK",
        "UPLOAD_DIRS_OK",
        "MODEL_IMAGES_LOAD_OK",
    ):
        assert required in guide
    for forbidden in (
        "covlink-llm-main_server.tar",
        "TENSOR_PARALLEL_SIZE=1",
        "docker logs --tail 300 rag-llm",
    ):
        assert forbidden not in guide
    server_steps = guide.split(
        "## 4. 服务器校验、解包并加载模型镜像",
        maxsplit=1,
    )[1]
    assert 'ssh "${SERVER}"' not in server_steps
