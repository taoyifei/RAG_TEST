"""验证服务器 preflight 只读、脱敏且可机器解析。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class _PreflightSandbox:
    """保存 server-preflight 行为测试路径。"""

    source: Path
    runtime: Path
    project_root: Path
    docker_root: Path
    binaries: Path
    environment: dict[str, str]


def _write_executable(path: Path, content: str) -> None:
    """写入测试命令。

    Args:
        path: 命令路径。
        content: Shell 内容。

    Returns:
        无。

    """
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    """获取目录内路径、大小和 mode 快照。

    Args:
        root: 快照根目录。

    Returns:
        稳定排序的相对路径、大小与 mode。

    """
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.stat().st_size if path.is_file() else -1,
                path.stat().st_mode,
            )
            for path in root.rglob("*")
        )
    )


def _prepare_sandbox(tmp_path: Path) -> _PreflightSandbox:
    """创建不调用真实 Docker/GPU 的只读预检沙箱。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        完整沙箱路径和环境。

    """
    root = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    images = runtime / "images"
    images.mkdir(parents=True)
    for name in (
        "docx-rag-linux-amd64.tar",
        "docx-rag-ocr-linux-amd64.tar",
        "qdrant-linux-amd64.tar",
    ):
        (images / name).write_bytes(b"image")
    (runtime / "RELEASE_ID").write_text("release\n", encoding="ascii")
    project_root = tmp_path / "project"
    docker_root = tmp_path / "docker-root"
    project_root.mkdir()
    docker_root.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_executable(
        binaries / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "version --format {{.Server.Version}}") echo 27.5.1 ;;
  "compose version --short") echo 2.33.1 ;;
  "info --format {{json .DriverStatus}}")
    echo '[["Backing Filesystem","extfs"]]' ;;
  "info --format {{json .Runtimes}}")
    echo '{"io.containerd.runc.v2":{},"nvidia":{}}' ;;
  "info --format {{json .DockerRootDir}}")
    printf '"%s"\n' "${FAKE_DOCKER_ROOT}" ;;
  "ps -a --format {{.Names}}") exit 0 ;;
  "network ls --format {{.Name}}") exit 0 ;;
  *) exit 91 ;;
esac
""",
    )
    _write_executable(
        binaries / "nvidia-smi",
        """#!/usr/bin/env bash
set -euo pipefail
echo '0, 24000'
echo '1, 12000'
""",
    )
    _write_executable(
        binaries / "df",
        """#!/usr/bin/env bash
set -euo pipefail
path="${!#}"
echo 'Filesystem Avail'
if [[ "${path}" == "${FAKE_PROJECT_ROOT}" ]]; then
  echo "${FAKE_PROJECT_SOURCE} ${FAKE_PROJECT_FREE}"
elif [[ "${path}" == "${FAKE_DOCKER_ROOT}" ]]; then
  echo "${FAKE_DOCKER_SOURCE} ${FAKE_DOCKER_FREE}"
else
  exit 92
fi
""",
    )
    _write_executable(
        binaries / "ss",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s' "${FAKE_SS_OUTPUT:-}"
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_DOCKER_FREE": "1000000",
            "FAKE_DOCKER_ROOT": str(docker_root),
            "FAKE_DOCKER_SOURCE": "/dev/docker",
            "FAKE_PROJECT_FREE": "1000000",
            "FAKE_PROJECT_ROOT": str(project_root),
            "FAKE_PROJECT_SOURCE": "/dev/project",
            "PATH": f"{binaries}:/usr/bin:/bin",
            "RAG_PREFLIGHT_PROJECT_ROOT": str(project_root),
        }
    )
    return _PreflightSandbox(
        source=root / "deployment/server-preflight.sh",
        runtime=runtime,
        project_root=project_root,
        docker_root=docker_root,
        binaries=binaries,
        environment=environment,
    )


def _write_candidate(
    path: Path,
    endpoint: str,
    *,
    gpu_device_id: str | None = "0",
) -> None:
    """写入含秘密哨兵的最小 candidate env。

    Args:
        path: candidate 文件路径。
        endpoint: 三类模型共用的测试 origin。
        gpu_device_id: OCR GPU 索引；None 表示故意缺失。

    Returns:
        无。

    """
    lines = [
        f"RAG_EMBEDDING_ENDPOINTS={json.dumps([endpoint])}",
        f"RAG_RERANKER_ENDPOINTS={json.dumps([endpoint])}",
        f"RAG_LLM_ENDPOINTS={json.dumps([endpoint])}",
        "RAG_QUERY_TOKEN=never-print-this-token",
    ]
    if gpu_device_id is not None:
        lines.append(f"RAG_OCR_GPU_DEVICE_ID={gpu_device_id}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_preflight(
    sandbox: _PreflightSandbox,
    candidate: Path | None,
    mode: str,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """运行真实 server-preflight shell 入口。

    Args:
        sandbox: 预检沙箱。
        candidate: candidate env；None 表示 basic preflight。
        mode: fresh 或 upgrade。
        environment: 可选覆盖后的进程环境。

    Returns:
        完整子进程结果。

    """
    return subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(sandbox.source),
            str(sandbox.runtime),
            str(candidate) if candidate is not None else "-",
            mode,
        ],
        env=environment or sandbox.environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _listening_socket() -> socket.socket:
    """创建足以接收多次 TCP connect 的本地监听 socket。

    Args:
        无。

    Returns:
        已绑定随机 loopback 端口的监听 socket。

    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    return listener


def _checks(report: dict[str, object]) -> dict[str, dict[str, object]]:
    """按名称索引预检检查项。

    Args:
        report: 已解析 JSON 报告。

    Returns:
        检查项名称到原始对象的映射。

    """
    values = report["checks"]
    assert isinstance(values, list)
    return {
        str(item["name"]): item
        for item in values
        if isinstance(item, dict)
    }


def test_server_preflight_full_candidate_is_redacted_and_read_only(
    tmp_path: Path,
) -> None:
    """证明 full preflight 使用 candidate、选中 GPU 且零副作用。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        无。

    """
    sandbox = _prepare_sandbox(tmp_path)
    candidate = tmp_path / "candidate.env"
    with _listening_socket() as listener:
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
        _write_candidate(candidate, endpoint)
        before = _snapshot(tmp_path)
        completed = _run_preflight(sandbox, candidate, "fresh")
        after = _snapshot(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert before == after
    assert completed.stderr == ""
    assert endpoint not in completed.stdout
    assert "never-print-this-token" not in completed.stdout
    assert str(candidate) not in completed.stdout
    report = json.loads(completed.stdout)
    assert report["overall"] == "PASS"
    checks = _checks(report)
    assert set(checks) == {
        "docker",
        "fixed_directory",
        "gpu",
        "model_endpoints",
        "ports",
        "runtime",
        "runtime_state",
        "storage",
    }
    assert checks["gpu"]["details"] == {
        "configured": True,
        "nvidia_runtime": True,
        "selected_device_id": 0,
        "selected_free_mib": 24000,
    }
    assert checks["model_endpoints"]["details"] == {
        "configured": True,
        "count": 3,
        "reachable": 3,
        "self_hosted_selected": False,
    }
    assert checks["ports"]["details"] == {
        "checked": [8088],
        "occupied": [],
        "self_hosted_selected": False,
    }


def test_server_preflight_basic_mode_defers_candidate_checks(
    tmp_path: Path,
) -> None:
    """证明解包后的 basic preflight 不猜测 candidate 值。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        无。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_preflight(sandbox, None, "fresh")

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["overall"] == "WARN"
    checks = _checks(report)
    assert checks["gpu"]["status"] == "WARN"
    assert checks["model_endpoints"]["status"] == "WARN"


def test_server_preflight_fresh_rejects_occupied_8088(
    tmp_path: Path,
) -> None:
    """证明 fresh 的 8088 占用是硬失败而 upgrade 仅告警。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        无。

    """
    sandbox = _prepare_sandbox(tmp_path)
    environment = sandbox.environment | {
        "FAKE_SS_OUTPUT": "LISTEN 0 128 0.0.0.0:8088 0.0.0.0:*\n"
    }
    candidate = tmp_path / "candidate.env"
    with _listening_socket() as listener:
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
        _write_candidate(candidate, endpoint)
        fresh = _run_preflight(
            sandbox,
            candidate,
            "fresh",
            environment=environment,
        )
        upgrade = _run_preflight(
            sandbox,
            candidate,
            "upgrade",
            environment=environment,
        )

    assert fresh.returncode != 0
    assert _checks(json.loads(fresh.stdout))["ports"]["status"] == "FAIL"
    assert upgrade.returncode == 0, upgrade.stderr
    assert _checks(json.loads(upgrade.stdout))["ports"]["status"] == "WARN"


def test_server_preflight_checks_model_ports_only_for_self_hosted(
    tmp_path: Path,
) -> None:
    """证明 8091/8092 只在 candidate 选择它们时进入端口检查。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        无。

    """
    sandbox = _prepare_sandbox(tmp_path)
    candidate = tmp_path / "candidate.env"
    _write_candidate(candidate, "http://127.0.0.1:8091")

    completed = _run_preflight(sandbox, candidate, "upgrade")

    report = json.loads(completed.stdout)
    ports = _checks(report)["ports"]["details"]
    assert ports["self_hosted_selected"] is True
    assert ports["checked"] == [8088, 8091, 8092]


@pytest.mark.parametrize(
    ("project_free", "docker_free", "expected_project", "expected_docker"),
    ((1, 1_000_000, False, True), (1_000_000, 1, True, False)),
)
def test_server_preflight_rejects_each_insufficient_filesystem(
    tmp_path: Path,
    project_free: int,
    docker_free: int,
    expected_project: bool,
    expected_docker: bool,
) -> None:
    """证明 project 与 DockerRootDir 任一空间不足均失败。

    Args:
        tmp_path: pytest 临时目录。
        project_free: fake project 可用字节。
        docker_free: fake DockerRootDir 可用字节。
        expected_project: 预期 project 容量结论。
        expected_docker: 预期 DockerRootDir 容量结论。

    Returns:
        无。

    """
    sandbox = _prepare_sandbox(tmp_path)
    environment = sandbox.environment | {
        "FAKE_DOCKER_FREE": str(docker_free),
        "FAKE_PROJECT_FREE": str(project_free),
    }

    completed = _run_preflight(
        sandbox,
        None,
        "fresh",
        environment=environment,
    )

    assert completed.returncode != 0
    storage = _checks(json.loads(completed.stdout))["storage"]
    assert storage["status"] == "FAIL"
    assert storage["details"]["project_sufficient"] is expected_project
    assert storage["details"]["docker_sufficient"] is expected_docker


def test_server_preflight_merges_requirements_on_same_filesystem(
    tmp_path: Path,
) -> None:
    """证明同一文件系统按 runtime 与三张镜像需求之和判断。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        无。

    """
    sandbox = _prepare_sandbox(tmp_path)
    runtime_bytes = sum(
        path.stat().st_size
        for path in sandbox.runtime.rglob("*")
        if path.is_file()
    )
    image_bytes = sum(
        path.stat().st_size
        for path in (sandbox.runtime / "images").iterdir()
    )
    environment = sandbox.environment | {
        "FAKE_DOCKER_FREE": str(runtime_bytes + image_bytes - 1),
        "FAKE_DOCKER_SOURCE": "/dev/shared",
        "FAKE_PROJECT_FREE": str(runtime_bytes + image_bytes - 1),
        "FAKE_PROJECT_SOURCE": "/dev/shared",
    }

    completed = _run_preflight(
        sandbox,
        None,
        "fresh",
        environment=environment,
    )

    assert completed.returncode != 0
    details = _checks(json.loads(completed.stdout))["storage"]["details"]
    assert details["same_filesystem"] is True
    assert details["combined_required_bytes"] == runtime_bytes + image_bytes


def test_server_preflight_requires_candidate_ocr_gpu(
    tmp_path: Path,
) -> None:
    """证明 full preflight 拒绝缺失 OCR GPU 索引的 candidate。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        无。

    """
    sandbox = _prepare_sandbox(tmp_path)
    candidate = tmp_path / "candidate.env"
    _write_candidate(candidate, "http://127.0.0.1:1", gpu_device_id=None)

    completed = _run_preflight(sandbox, candidate, "fresh")

    assert completed.returncode != 0
    assert _checks(json.loads(completed.stdout))["gpu"]["status"] == "FAIL"
    assert "RAG_OCR_GPU_DEVICE_ID" not in completed.stdout


def test_server_preflight_source_has_no_mutating_commands() -> None:
    """锁定 preflight 不执行文件、镜像或容器写操作。"""
    source = (
        Path(__file__).parents[1] / "deployment/server-preflight.sh"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "docker load",
        "docker pull",
        "docker compose up",
        "docker run",
        "mkdir ",
        "install ",
        "rm ",
        "touch ",
    ):
        assert forbidden not in source
