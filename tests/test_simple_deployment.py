"""简单 Compose 与服务器脚本的专项契约。"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
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
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG}"
if [[ "$1" == "load" ]]; then cat >/dev/null; exit 0; fi
if [[ "$1" == "inspect" ]]; then echo healthy; exit 0; fi
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
        extra_env={"FAKE_DOCKER_LOG": str(log)},
    )

    assert completed.returncode == 0, completed.stderr
    assert (docs / "sample.docx").read_bytes() == b"docx"
    calls = log.read_text(encoding="utf-8").splitlines()
    up_call = next(line for line in calls if " up -d " in f" {line} ")
    assert "rag-qdrant rag-ocr rag-app" in up_call
    assert "rag-worker" not in up_call
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


def _write_update_fakes(fake_bin: Path, *, fail_first_compose: bool) -> None:
    compose_behavior = """
if [[ "$1" == "compose" ]]; then
  count=0
  [[ -f "${FAKE_COMPOSE_COUNT}" ]] && count="$(cat "${FAKE_COMPOSE_COUNT}")"
  count=$((count + 1))
  printf '%s' "${count}" >"${FAKE_COMPOSE_COUNT}"
  if [[ "${FAIL_FIRST_COMPOSE}" == "true" && "${count}" -eq 1 ]]; then
    exit 1
  fi
  exit 0
fi
"""
    _write_executable(
        fake_bin / "docker",
        f"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"${{FAKE_DOCKER_LOG}}"
if [[ "$1" == "load" ]]; then
  cat >/dev/null
  echo 'Loaded image: docx-rag:new'
  exit 0
fi
if [[ "$1" == "run" ]]; then
  if [[ "$*" == *'docx-rag:old'* ]]; then
    echo '{{"pipeline_fingerprint":"sha256:old"}}'
  else
    echo '{{"pipeline_fingerprint":"sha256:new"}}'
  fi
  exit 0
fi
if [[ "$1" == "image" && "$2" == "inspect" && "$3" == "--format" ]]; then
  printf '%040d\n' 2
  exit 0
fi
{compose_behavior}
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\necho '{\"status\":\"live\"}'\n",
    )
    os.environ["FAIL_FIRST_COMPOSE"] = str(fail_first_compose).lower()


def test_update_app_changes_only_app_env_and_service(tmp_path: Path) -> None:
    root = _root()
    _, env_file, archive, sidecar = _prepare_update_case(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_update_fakes(fake_bin, fail_first_compose=False)
    log = tmp_path / "docker.log"
    count = tmp_path / "compose.count"

    completed = _run_script(
        root / "deployment/simple/update-app.sh",
        [str(archive), str(sidecar), str(env_file)],
        fake_bin=fake_bin,
        extra_env={
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_COMPOSE_COUNT": str(count),
            "FAIL_FIRST_COMPOSE": "false",
        },
    )

    assert completed.returncode == 0, completed.stderr
    updated = env_file.read_text(encoding="utf-8")
    assert "RAG_APP_IMAGE=docx-rag:new\n" in updated
    assert f"RAG_RELEASE_REVISION={'2'.zfill(40)}\n" in updated
    calls = log.read_text(encoding="utf-8")
    assert "rag-ocr" not in calls
    assert "rag-qdrant" not in calls
    assert "rag-app" in calls
    assert "REINDEX_REQUIRED" in completed.stdout


def test_update_app_restores_old_env_when_compose_fails(tmp_path: Path) -> None:
    root = _root()
    _, env_file, archive, sidecar = _prepare_update_case(tmp_path)
    original = env_file.read_text(encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_update_fakes(fake_bin, fail_first_compose=True)

    completed = _run_script(
        root / "deployment/simple/update-app.sh",
        [str(archive), str(sidecar), str(env_file)],
        fake_bin=fake_bin,
        extra_env={
            "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
            "FAKE_COMPOSE_COUNT": str(tmp_path / "compose.count"),
            "FAIL_FIRST_COMPOSE": "true",
        },
    )

    assert completed.returncode != 0
    assert env_file.read_text(encoding="utf-8") == original
    assert "已恢复旧 image 和旧 env" in completed.stderr


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
