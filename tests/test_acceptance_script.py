from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_REVISION = "1" * 40
_CALIBRATION_REVISION = "2" * 40
_RELEASE_ID = "release-a"
_REPORT_NAMES = (
    "model-contract-embedding.json",
    "model-contract-reranker.json",
    "model-contract-llm-1.json",
    "model-contract-llm-2.json",
    "model-contract-llm-3.json",
    "model-contract-llm-4.json",
)
_ENDPOINTS = (
    "http://embedding.internal:8000",
    "http://reranker.internal:8000",
    "http://llm-1.internal:8000",
    "http://llm-2.internal:8000",
    "http://llm-3.internal:8000",
    "http://llm-4.internal:8000",
)
_REVISIONS = (
    "embedding-revision",
    "reranker-revision",
    "llm-revision-1",
    "llm-revision-2",
    "llm-revision-3",
    "llm-revision-4",
)


@dataclass(frozen=True)
class _AcceptanceSandbox:
    root: Path
    script: Path
    report_dir: Path
    logs: Path
    command_log: Path
    worker_state: Path
    binaries: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_model_reports(directory: Path) -> None:
    directory.mkdir(parents=True)
    services = ("embedding", "reranker", "llm", "llm", "llm", "llm")
    models = (
        "Qwen3-Embedding-0.6B",
        "Qwen3-Reranker-0.6B",
        "Qwen/Qwen3-8B-AWQ",
        "Qwen/Qwen3-8B-AWQ",
        "Qwen/Qwen3-8B-AWQ",
        "Qwen/Qwen3-8B-AWQ",
    )
    reports: list[dict[str, str]] = []
    for name, service, endpoint, model, revision in zip(
        _REPORT_NAMES,
        services,
        _ENDPOINTS,
        models,
        _REVISIONS,
        strict=True,
    ):
        probe: dict[str, object]
        if service == "embedding":
            probe = {"dimension": 1024}
        elif service == "reranker":
            probe = {"score_range": [0.0, 1.0]}
        else:
            probe = {"temperature": 0, "thinking_enabled": False}
        payload = {
            "schema_version": "1",
            "status": "passed",
            "service": service,
            "endpoint": endpoint,
            "model": model,
            "endpoint_revision": revision,
            "revision_source": "endpoint",
            "health": "passed",
            "model_id": "passed",
            "probe": probe,
        }
        path = directory / name
        path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o400)
        reports.append(
            {
                "name": name,
                "service": service,
                "sha256": "sha256:"
                + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    fleet = {
        "schema_version": "1",
        "attempt_id": "a" * 32,
        "source_revision": _CALIBRATION_REVISION,
        "status": "passed",
        "reports": sorted(reports, key=lambda report: report["name"]),
    }
    fleet_path = directory / "FLEET_REPORT.json"
    fleet_path.write_text(
        json.dumps(fleet, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fleet_path.chmod(0o400)
    directory.chmod(0o500)


def _prepare_sandbox(tmp_path: Path) -> _AcceptanceSandbox:
    root = tmp_path / "RAG"
    release = root / f"releases/{_RELEASE_ID}"
    env_dir = root / "shared/env"
    logs = root / "logs"
    report_dir = root / f"logs/model-contracts/{_RELEASE_ID}"
    for directory in (release, env_dir, logs):
        directory.mkdir(parents=True, exist_ok=True)
    (release / "RELEASE_ID").write_text(
        f"{_RELEASE_ID}\n",
        encoding="ascii",
    )
    (release / "SOURCE_REVISION").write_text(
        f"{_REVISION}\n",
        encoding="ascii",
    )
    (release / "compose.yaml").write_text(
        "services:\n  rag-worker: {image: app}\n",
        encoding="utf-8",
    )
    _write_executable(
        release / "verify-offline.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
    )
    (root / "current").symlink_to(release)
    env_file = env_dir / "rag.env"
    env_file.write_text(
        f"RAG_APP_IMAGE=docx-rag:{_RELEASE_ID}\n"
        f"RAG_OCR_IMAGE=docx-rag-ocr:{_RELEASE_ID}\n"
        f"RAG_QDRANT_IMAGE=rag-qdrant:{_RELEASE_ID}\n"
        "RAG_PORT=8088\n"
        f"RAG_RELEASE_REVISION={_REVISION}\n"
        f'RAG_EMBEDDING_ENDPOINTS=["{_ENDPOINTS[0]}"]\n'
        f'RAG_RERANKER_ENDPOINTS=["{_ENDPOINTS[1]}"]\n'
        "RAG_LLM_ENDPOINTS="
        + json.dumps(list(_ENDPOINTS[2:]), separators=(",", ":"))
        + "\n"
        "RAG_EMBEDDING_MODEL=Qwen3-Embedding-0.6B\n"
        "RAG_RERANKER_MODEL=Qwen3-Reranker-0.6B\n"
        "RAG_LLM_MODEL=Qwen/Qwen3-8B-AWQ\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    _write_model_reports(report_dir)
    source = (
        Path(__file__).parents[1] / "deployment/acceptance.sh"
    ).read_text(encoding="utf-8")
    script = tmp_path / "acceptance.sh"
    script.write_text(
        source.replace(
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
    worker_state = tmp_path / "worker.state"
    worker_state.write_text("true\n", encoding="ascii")
    _install_fake_commands(binaries)
    return _AcceptanceSandbox(
        root=root,
        script=script,
        report_dir=report_dir,
        logs=logs,
        command_log=command_log,
        worker_state=worker_state,
        binaries=binaries,
    )


def _install_fake_commands(binaries: Path) -> None:
    _write_executable(
        binaries / "id",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "-u" ]]; then echo 0; else exit 91; fi
""",
    )
    _write_executable(
        binaries / "chown",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'chown %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
""",
    )
    _write_executable(
        binaries / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
if [[ "$1 $2" == "image inspect" ]]; then
  case "${@: -1}" in
    docx-rag:*) printf 'sha256:%064d\n' 1 ;;
    docx-rag-ocr:*) printf 'sha256:%064d\n' 2 ;;
    rag-qdrant:*) printf 'sha256:%064d\n' 3 ;;
    *) exit 92 ;;
  esac
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  container="${@: -1}"
  if [[ "$*" == *".Image"* ]]; then
    case "${container}" in
      rag-app) printf 'sha256:%064d\n' 1 ;;
      rag-ocr) printf 'sha256:%064d\n' 2 ;;
      rag-qdrant) printf 'sha256:%064d\n' 3 ;;
      rag-worker) printf 'sha256:%064d\n' 1 ;;
      *) exit 93 ;;
    esac
    exit 0
  fi
  if [[ "$*" == *"OOMKilled"* ]]; then
    running=true
    if [[ "${container}" == "rag-worker" ]]; then
      running="$(cat "${FAKE_WORKER_STATE}")"
    fi
    oom=false
    if [[ "${FAKE_OOM_SERVICE:-}" == "${container}" ]]; then oom=true; fi
    printf '%s\t%s\n' "${running}" "${oom}"
    exit 0
  fi
  exit 0
fi
if [[ "$1 $2" == "exec rag-app" ]]; then
  printf '%s\n' "${FAKE_FROZEN_CONTRACT}"
  exit 0
fi
if [[ "$1 $2" == "exec rag-ocr" ]]; then
  if [[ "${FAKE_OCR_MODE:-gpu}" == "cpu" ]]; then
    echo '{"device":"cpu","cuda_count":0}'
  else
    echo '{"device":"gpu:0","cuda_count":1}'
  fi
  exit 0
fi
if [[ "$1" == "compose" && "$*" == *" stop rag-worker"* ]]; then
  printf 'false\n' > "${FAKE_WORKER_STATE}"
  exit 0
fi
if [[ "$1" == "compose" && "$*" == *" run "* ]]; then
  printf '%s\n' "${FAKE_JOB_JSON}"
  exit "${FAKE_JOB_EXIT:-0}"
fi
if [[ "$1" == "compose" && "$*" == *" up -d "* ]]; then
  printf 'true\n' > "${FAKE_WORKER_STATE}"
  exit 0
fi
exit 94
""",
    )
    _write_executable(
        binaries / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "${FAKE_COMMAND_LOG}"
if [[ "${FAKE_READY_MODE:-ready}" == "503" ]]; then
  printf '{"ready":false}\n503'
else
  printf '{"ready":true}\n200'
fi
""",
    )


def _frozen_contract() -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "embedding_model": "Qwen3-Embedding-0.6B",
            "embedding_revision": _REVISIONS[0],
            "embedding_dimension": 1024,
            "reranker_model": "Qwen3-Reranker-0.6B",
            "reranker_revision": _REVISIONS[1],
            "llm_model": "Qwen/Qwen3-8B-AWQ",
            "llm_revisions": [
                [f"llm-{index}", revision]
                for index, revision in enumerate(_REVISIONS[2:], start=1)
            ],
            "calibration_source_revision": _CALIBRATION_REVISION,
            "index_fingerprint": "sha256:" + "2" * 64,
            "serving_fingerprint": "sha256:" + "3" * 64,
            "freeze_decision_sha256": "sha256:" + "4" * 64,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _run_acceptance(
    sandbox: _AcceptanceSandbox,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
            "FAKE_COMMAND_LOG": str(sandbox.command_log),
            "FAKE_WORKER_STATE": str(sandbox.worker_state),
            "FAKE_FROZEN_CONTRACT": _frozen_contract(),
            "FAKE_JOB_JSON": json.dumps(
                {
                    "job_id": "job_" + "b" * 32,
                    "state": "succeeded",
                    "error_code": None,
                },
                separators=(",", ":"),
            ),
        }
    )
    environment.update(overrides)
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(sandbox.script), str(sandbox.report_dir)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_acceptance_success_writes_private_sanitized_report(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_acceptance(sandbox)

    assert completed.returncode == 0, completed.stderr
    report_path = sandbox.logs / f"acceptance-{_RELEASE_ID}.json"
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o400
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["release_id"] == _RELEASE_ID
    assert report["source_revision"] == _REVISION
    assert report["job"] == {
        "error_code": None,
        "job_id": "job_" + "b" * 32,
        "state": "succeeded",
    }
    assert report["ready"] is True
    assert report["ocr_gpu"] == {"cuda_count": 1, "device": "gpu:0"}
    serialized = json.dumps(report, sort_keys=True).casefold()
    assert "endpoint" not in serialized
    assert "token" not in serialized
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert log.index(" stop rag-worker") < log.index(" run --rm")
    assert log.index(" run --rm") < log.index(" up -d")
    assert "chown 0:0" in log


@pytest.mark.parametrize(
    ("job_payload", "job_exit"),
    [
        (
            {
                "job_id": "job_" + "c" * 32,
                "state": "failed",
                "error_code": "INDEX_FAILED",
            },
            "1",
        ),
        (
            {
                "job_id": "job_" + "d" * 32,
                "state": "pending",
                "error_code": None,
            },
            "0",
        ),
    ],
)
def test_non_success_terminal_job_stops_acceptance(
    tmp_path: Path,
    job_payload: dict[str, object],
    job_exit: str,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_acceptance(
        sandbox,
        FAKE_JOB_JSON=json.dumps(job_payload, separators=(",", ":")),
        FAKE_JOB_EXIT=job_exit,
    )

    assert completed.returncode != 0
    assert not (sandbox.logs / f"acceptance-{_RELEASE_ID}.json").exists()
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert " run --rm" in log
    assert " up -d" not in log
    assert "curl " not in log


def test_ready_503_stops_without_acceptance_report(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_acceptance(sandbox, FAKE_READY_MODE="503")

    assert completed.returncode != 0
    assert not (sandbox.logs / f"acceptance-{_RELEASE_ID}.json").exists()
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert " up -d" in log
    assert "curl " in log


def test_ocr_cpu_stops_before_index(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_acceptance(sandbox, FAKE_OCR_MODE="cpu")

    assert completed.returncode != 0
    assert not (sandbox.logs / f"acceptance-{_RELEASE_ID}.json").exists()
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert "exec rag-ocr" in log
    assert " run --rm" not in log


def test_wrong_model_report_count_stops_before_index(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    report_path = sandbox.report_dir / "model-contract-llm-4.json"
    sandbox.report_dir.chmod(0o700)
    report_path.chmod(0o600)
    report_path.unlink()
    sandbox.report_dir.chmod(0o500)

    completed = _run_acceptance(sandbox)

    assert completed.returncode != 0
    assert not (sandbox.logs / f"acceptance-{_RELEASE_ID}.json").exists()
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert " run --rm" not in log


def test_wrong_calibration_revision_stops_before_index(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    fleet_path = sandbox.report_dir / "FLEET_REPORT.json"
    sandbox.report_dir.chmod(0o700)
    fleet_path.chmod(0o600)
    fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
    fleet["source_revision"] = "3" * 40
    fleet_path.write_text(
        json.dumps(fleet, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fleet_path.chmod(0o400)
    sandbox.report_dir.chmod(0o500)

    completed = _run_acceptance(sandbox)

    assert completed.returncode != 0
    assert not (sandbox.logs / f"acceptance-{_RELEASE_ID}.json").exists()
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert " run --rm" not in log
