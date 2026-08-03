#!/usr/bin/env bash
set -euo pipefail

exec python3 - "$@" <<'PY'
"""执行服务器部署前的零副作用只读检查。"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
ENDPOINT_KEYS = (
    "RAG_EMBEDDING_ENDPOINTS",
    "RAG_RERANKER_ENDPOINTS",
    "RAG_LLM_ENDPOINTS",
)
GPU_KEY = "RAG_OCR_GPU_DEVICE_ID"
IMAGE_ARCHIVES = (
    "docx-rag-linux-amd64.tar",
    "docx-rag-ocr-linux-amd64.tar",
    "qdrant-linux-amd64.tar",
)


def command(arguments: list[str]) -> tuple[bool, str]:
    """运行只读命令并收敛错误输出。"""
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    if result.returncode != 0:
        return False, ""
    return True, result.stdout.strip()


def check(name: str, status: str, details: dict[str, Any]) -> dict[str, Any]:
    """构造稳定的单项检查结果。"""
    return {"details": details, "name": name, "status": status}


def parse_inputs() -> tuple[Path, Path | None, str]:
    """解析 runtime、candidate 与部署模式。"""
    if len(sys.argv) != 4:
        raise ValueError("invalid argument count")
    runtime_value, candidate_value, mode = sys.argv[1:]
    if mode not in {"fresh", "upgrade"}:
        raise ValueError("invalid deployment mode")
    runtime = Path(runtime_value)
    if not runtime.is_absolute() or runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("invalid runtime directory")
    if candidate_value == "-":
        candidate = None
    else:
        candidate = Path(candidate_value)
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            raise ValueError("invalid candidate file")
    return runtime, candidate, mode


def docker_check() -> tuple[dict[str, Any], Path | None]:
    """检查 Docker、Compose、store 模式和 DockerRootDir。"""
    version_ok, version = command(
        ["docker", "version", "--format", "{{.Server.Version}}"]
    )
    compose_ok, compose = command(["docker", "compose", "version", "--short"])
    driver_ok, driver_text = command(
        ["docker", "info", "--format", "{{json .DriverStatus}}"]
    )
    root_ok, root_text = command(
        ["docker", "info", "--format", "{{json .DockerRootDir}}"]
    )
    store_mode = "unknown"
    if driver_ok:
        try:
            driver = json.loads(driver_text)
        except json.JSONDecodeError:
            driver_ok = False
        else:
            marker = ["driver-type", "io.containerd.snapshotter.v1"]
            store_mode = "containerd" if marker in driver else "classic"
    docker_root: Path | None = None
    if root_ok:
        try:
            root_value = json.loads(root_text)
        except json.JSONDecodeError:
            root_ok = False
        else:
            if not isinstance(root_value, str) or not Path(root_value).is_absolute():
                root_ok = False
            else:
                docker_root = Path(root_value)
                if docker_root.is_symlink():
                    root_ok = False
                    docker_root = None
    status = PASS if all((version_ok, compose_ok, driver_ok, root_ok)) else FAIL
    return (
        check(
            "docker",
            status,
            {
                "compose_version": compose if compose_ok else None,
                "root_configured": root_ok,
                "store_mode": store_mode,
                "version": version if version_ok else None,
            },
        ),
        docker_root,
    )


def runtime_check(runtime: Path) -> tuple[dict[str, Any], int, int]:
    """核对 runtime 和三张镜像归档并计算字节需求。"""
    runtime_bytes = 0
    try:
        for path in runtime.rglob("*"):
            if path.is_symlink():
                raise OSError("runtime symbolic link")
            if path.is_file():
                runtime_bytes += path.stat().st_size
            elif not path.is_dir():
                raise OSError("runtime special file")
        archives = tuple(runtime / "images" / name for name in IMAGE_ARCHIVES)
        if any(
            path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
            for path in archives
        ):
            raise OSError("runtime image archive")
        image_bytes = sum(path.stat().st_size for path in archives)
    except OSError:
        return (
            check(
                "runtime",
                FAIL,
                {
                    "image_archive_bytes": None,
                    "image_archive_count": 0,
                    "runtime_bytes": None,
                },
            ),
            0,
            0,
        )
    return (
        check(
            "runtime",
            PASS,
            {
                "image_archive_bytes": image_bytes,
                "image_archive_count": len(archives),
                "runtime_bytes": runtime_bytes,
            },
        ),
        runtime_bytes,
        image_bytes,
    )


def fixed_directory_check(project_root: Path) -> dict[str, Any]:
    """检查固定项目目录，不创建或修复它。"""
    if not project_root.exists():
        return check("fixed_directory", WARN, {"state": "absent"})
    if project_root.is_symlink() or not project_root.is_dir():
        return check("fixed_directory", FAIL, {"state": "unsafe"})
    return check("fixed_directory", PASS, {"state": "directory"})


def candidate_values(candidate: Path | None) -> tuple[dict[str, str], bool]:
    """只提取端点数组与 OCR GPU 索引，不保留密钥。"""
    if candidate is None:
        return {}, True
    required = set(ENDPOINT_KEYS) | {GPU_KEY}
    values: dict[str, str] = {}
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}, False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in required:
            if key in values:
                return {}, False
            values[key] = value
    return values, True


def nvidia_runtime() -> tuple[bool, bool]:
    """检查 Docker NVIDIA runtime。"""
    runtime_ok, runtimes_text = command(
        ["docker", "info", "--format", "{{json .Runtimes}}"]
    )
    if not runtime_ok:
        return False, False
    try:
        runtimes = json.loads(runtimes_text)
    except json.JSONDecodeError:
        return False, False
    return True, isinstance(runtimes, dict) and "nvidia" in runtimes


def gpu_check(
    values: dict[str, str],
    *,
    candidate_present: bool,
    candidate_valid: bool,
) -> dict[str, Any]:
    """校验 candidate 选择的 OCR GPU 并报告其空闲显存。"""
    runtime_ok, has_runtime = nvidia_runtime()
    smi_ok, smi_text = command(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.free",
            "--format=csv,noheader,nounits",
        ]
    )
    devices: dict[int, int] = {}
    if smi_ok:
        try:
            for row in smi_text.splitlines():
                index, free = (int(value.strip()) for value in row.split(","))
                devices[index] = free
        except (TypeError, ValueError):
            smi_ok = False
            devices = {}
    configured = candidate_present and candidate_valid and GPU_KEY in values
    selected_device: int | None = None
    if configured and re.fullmatch(r"0|[1-9][0-9]*", values[GPU_KEY]):
        selected_device = int(values[GPU_KEY])
    selected_free = devices.get(selected_device) if selected_device is not None else None
    infrastructure_ok = runtime_ok and has_runtime and smi_ok and bool(devices)
    if not candidate_present:
        status = WARN if infrastructure_ok else FAIL
    else:
        status = PASS if infrastructure_ok and selected_free is not None else FAIL
    return check(
        "gpu",
        status,
        {
            "configured": configured,
            "nvidia_runtime": has_runtime,
            "selected_device_id": selected_device,
            "selected_free_mib": selected_free,
        },
    )


def parsed_endpoints(values: dict[str, str]) -> tuple[list[tuple[str, int]], bool]:
    """解析三个端点数组并判断是否选择 8091/8092。"""
    endpoints: list[tuple[str, int]] = []
    self_hosted = False
    for key in ENDPOINT_KEYS:
        raw = values.get(key)
        if raw is None:
            raise ValueError("missing endpoint array")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("invalid endpoint JSON") from None
        if not isinstance(items, list) or not items:
            raise ValueError("invalid endpoint array")
        for item in items:
            if not isinstance(item, str):
                raise ValueError("invalid endpoint item")
            parsed = urlsplit(item)
            try:
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                raise ValueError("invalid endpoint port") from None
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("invalid endpoint origin")
            endpoints.append((parsed.hostname, port))
            self_hosted = self_hosted or port in {8091, 8092}
    return endpoints, self_hosted


def model_endpoints_check(
    values: dict[str, str],
    *,
    candidate_present: bool,
    candidate_valid: bool,
) -> tuple[dict[str, Any], bool]:
    """从 candidate 对模型端点执行无 token TCP 检查。"""
    if not candidate_present:
        return (
            check(
                "model_endpoints",
                WARN,
                {
                    "configured": False,
                    "count": 0,
                    "reachable": 0,
                    "self_hosted_selected": False,
                },
            ),
            False,
        )
    if not candidate_valid:
        return (
            check(
                "model_endpoints",
                FAIL,
                {
                    "configured": False,
                    "count": 0,
                    "reachable": 0,
                    "self_hosted_selected": False,
                },
            ),
            False,
        )
    try:
        endpoints, self_hosted = parsed_endpoints(values)
    except ValueError:
        return (
            check(
                "model_endpoints",
                FAIL,
                {
                    "configured": True,
                    "count": 0,
                    "reachable": 0,
                    "self_hosted_selected": False,
                },
            ),
            False,
        )
    reachable = 0
    for hostname, port in endpoints:
        try:
            with socket.create_connection((hostname, port), timeout=2):
                reachable += 1
        except OSError:
            continue
    status = PASS if reachable == len(endpoints) else FAIL
    return (
        check(
            "model_endpoints",
            status,
            {
                "configured": True,
                "count": len(endpoints),
                "reachable": reachable,
                "self_hosted_selected": self_hosted,
            },
        ),
        self_hosted,
    )


def ports_check(mode: str, *, self_hosted: bool) -> dict[str, Any]:
    """按部署模式和模型拓扑检查相关监听端口。"""
    success, output = command(["ss", "-H", "-lnt"])
    checked = [8088]
    if self_hosted:
        checked.extend((8091, 8092))
    if not success:
        return check(
            "ports",
            FAIL,
            {
                "checked": checked,
                "occupied": [],
                "self_hosted_selected": self_hosted,
            },
        )
    occupied = [
        port
        for port in checked
        if re.search(rf":{port}(?:\s|$)", output) is not None
    ]
    if mode == "fresh" and 8088 in occupied:
        status = FAIL
    elif occupied:
        status = WARN
    else:
        status = PASS
    return check(
        "ports",
        status,
        {
            "checked": checked,
            "occupied": occupied,
            "self_hosted_selected": self_hosted,
        },
    )


def runtime_state_check() -> dict[str, Any]:
    """列出既有 rag 容器与网络的数量。"""
    containers_ok, containers_text = command(
        ["docker", "ps", "-a", "--format", "{{.Names}}"]
    )
    networks_ok, networks_text = command(
        ["docker", "network", "ls", "--format", "{{.Name}}"]
    )
    if not containers_ok or not networks_ok:
        return check(
            "runtime_state",
            FAIL,
            {"container_count": 0, "network_count": 0},
        )
    containers = [
        name for name in containers_text.splitlines() if name.startswith("rag-")
    ]
    networks = [
        name for name in networks_text.splitlines() if name.startswith("rag-")
    ]
    status = WARN if containers or networks else PASS
    return check(
        "runtime_state",
        status,
        {"container_count": len(containers), "network_count": len(networks)},
    )


def existing_ancestor(path: Path) -> Path:
    """查找用于 df 的最近既有祖先。"""
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def filesystem_capacity(path: Path) -> tuple[str, int] | None:
    """读取路径所在文件系统标识与可用字节。"""
    success, output = command(
        ["df", "-B1", "--output=source,avail", "--", str(existing_ancestor(path))]
    )
    if not success:
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    if len(fields) < 2:
        return None
    try:
        free_bytes = int(fields[-1])
    except ValueError:
        return None
    return " ".join(fields[:-1]), free_bytes


def storage_check(
    project_root: Path,
    docker_root: Path | None,
    runtime_bytes: int,
    image_bytes: int,
) -> dict[str, Any]:
    """按分盘或同盘拓扑核对 runtime 与镜像加载空间。"""
    project = filesystem_capacity(project_root)
    docker = filesystem_capacity(docker_root) if docker_root is not None else None
    if project is None or docker is None or runtime_bytes <= 0 or image_bytes <= 0:
        return check(
            "storage",
            FAIL,
            {
                "combined_required_bytes": None,
                "docker_free_bytes": docker[1] if docker else None,
                "docker_required_bytes": image_bytes or None,
                "docker_sufficient": False,
                "project_free_bytes": project[1] if project else None,
                "project_required_bytes": runtime_bytes or None,
                "project_sufficient": False,
                "same_filesystem": None,
            },
        )
    same_filesystem = project[0] == docker[0]
    if same_filesystem:
        combined_required = runtime_bytes + image_bytes
        common_free = min(project[1], docker[1])
        project_sufficient = common_free >= combined_required
        docker_sufficient = project_sufficient
    else:
        combined_required = None
        project_sufficient = project[1] >= runtime_bytes
        docker_sufficient = docker[1] >= image_bytes
    status = PASS if project_sufficient and docker_sufficient else FAIL
    return check(
        "storage",
        status,
        {
            "combined_required_bytes": combined_required,
            "docker_free_bytes": docker[1],
            "docker_required_bytes": image_bytes,
            "docker_sufficient": docker_sufficient,
            "project_free_bytes": project[1],
            "project_required_bytes": runtime_bytes,
            "project_sufficient": project_sufficient,
            "same_filesystem": same_filesystem,
        },
    )


def emit(checks: list[dict[str, Any]], *, mode: str | None) -> int:
    """输出脱敏 JSON 并返回整体退出码。"""
    statuses = {item["status"] for item in checks}
    overall = FAIL if FAIL in statuses else WARN if WARN in statuses else PASS
    report = {
        "checks": checks,
        "mode": mode,
        "overall": overall,
        "schema_version": "2",
    }
    print(json.dumps(report, sort_keys=True))
    return 1 if overall == FAIL else 0


def main() -> int:
    """执行参数驱动的零副作用服务器预检。"""
    try:
        runtime, candidate, mode = parse_inputs()
    except ValueError:
        return emit(
            [check("inputs", FAIL, {"valid": False})],
            mode=None,
        )
    project_root = Path(
        os.environ.get("RAG_PREFLIGHT_PROJECT_ROOT", "/data/tyf/RAG")
    )
    values, candidate_valid = candidate_values(candidate)
    docker_result, docker_root = docker_check()
    runtime_result, runtime_bytes, image_bytes = runtime_check(runtime)
    endpoint_result, self_hosted = model_endpoints_check(
        values,
        candidate_present=candidate is not None,
        candidate_valid=candidate_valid,
    )
    checks = [
        docker_result,
        fixed_directory_check(project_root),
        gpu_check(
            values,
            candidate_present=candidate is not None,
            candidate_valid=candidate_valid,
        ),
        endpoint_result,
        ports_check(mode, self_hosted=self_hosted),
        runtime_result,
        runtime_state_check(),
        storage_check(
            project_root,
            docker_root,
            runtime_bytes,
            image_bytes,
        ),
    ]
    return emit(checks, mode=mode)


raise SystemExit(main())
PY
