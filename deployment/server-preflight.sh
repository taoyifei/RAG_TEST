#!/usr/bin/env bash
set -euo pipefail

exec python3 - <<'PY'
"""执行服务器部署前的零副作用只读检查。"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
MIN_FREE_BYTES = 80 * 1024**3
PROJECT_ROOT = Path(
    os.environ.get("RAG_PREFLIGHT_PROJECT_ROOT", "/data/tyf/RAG")
)
ENDPOINT_KEYS = (
    "RAG_EMBEDDING_ENDPOINTS",
    "RAG_RERANKER_ENDPOINTS",
    "RAG_LLM_ENDPOINTS",
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


def docker_check() -> dict[str, Any]:
    """检查 Docker、Compose 与 image store 模式。"""
    version_ok, version = command(
        ["docker", "version", "--format", "{{.Server.Version}}"]
    )
    compose_ok, compose = command(["docker", "compose", "version", "--short"])
    driver_ok, driver_text = command(
        ["docker", "info", "--format", "{{json .DriverStatus}}"]
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
    status = PASS if version_ok and compose_ok and driver_ok else FAIL
    return check(
        "docker",
        status,
        {
            "compose_version": compose if compose_ok else None,
            "store_mode": store_mode,
            "version": version if version_ok else None,
        },
    )


def gpu_check() -> dict[str, Any]:
    """检查 NVIDIA runtime、GPU 索引和显存。"""
    runtime_ok, runtimes_text = command(
        ["docker", "info", "--format", "{{json .Runtimes}}"]
    )
    has_runtime = False
    if runtime_ok:
        try:
            runtimes = json.loads(runtimes_text)
            has_runtime = isinstance(runtimes, dict) and "nvidia" in runtimes
        except json.JSONDecodeError:
            runtime_ok = False
    smi_ok, smi_text = command(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ]
    )
    devices: list[dict[str, int]] = []
    if smi_ok:
        try:
            for row in smi_text.splitlines():
                index, total, free = (int(value.strip()) for value in row.split(","))
                devices.append(
                    {"free_mib": free, "index": index, "total_mib": total}
                )
        except (TypeError, ValueError):
            smi_ok = False
            devices = []
    status = PASS if runtime_ok and has_runtime and smi_ok and devices else FAIL
    return check(
        "gpu",
        status,
        {"devices": devices, "nvidia_runtime": has_runtime},
    )


def existing_ancestor(path: Path) -> Path:
    """查找用于 statvfs 的最近既有祖先。"""
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def storage_check() -> dict[str, Any]:
    """检查固定目录所在文件系统的可用容量。"""
    try:
        stats = os.statvfs(existing_ancestor(PROJECT_ROOT))
        free_bytes = stats.f_bavail * stats.f_frsize
    except OSError:
        return check("storage", FAIL, {"free_bytes": None})
    status = PASS if free_bytes >= MIN_FREE_BYTES else FAIL
    return check(
        "storage",
        status,
        {"free_bytes": free_bytes, "minimum_bytes": MIN_FREE_BYTES},
    )


def fixed_directory_check() -> dict[str, Any]:
    """检查固定项目目录，不创建或修复它。"""
    if not PROJECT_ROOT.exists():
        return check("fixed_directory", WARN, {"state": "absent"})
    if PROJECT_ROOT.is_symlink() or not PROJECT_ROOT.is_dir():
        return check("fixed_directory", FAIL, {"state": "unsafe"})
    return check("fixed_directory", PASS, {"state": "directory"})


def ports_check() -> dict[str, Any]:
    """检查三个固定端口的监听占用。"""
    success, output = command(["ss", "-H", "-lnt"])
    if not success:
        return check("ports", FAIL, {"occupied": []})
    occupied = [
        port
        for port in (8088, 8091, 8092)
        if re.search(rf":{port}(?:\s|$)", output) is not None
    ]
    return check(
        "ports",
        WARN if occupied else PASS,
        {"occupied": occupied},
    )


def runtime_state_check() -> dict[str, Any]:
    """列出既有 rag 容器与网络。"""
    containers_ok, containers_text = command(
        ["docker", "ps", "-a", "--format", "{{.Names}}"]
    )
    networks_ok, networks_text = command(
        ["docker", "network", "ls", "--format", "{{.Name}}"]
    )
    if not containers_ok or not networks_ok:
        return check("runtime_state", FAIL, {"container_count": 0, "network_count": 0})
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


def env_file_values() -> dict[str, str]:
    """只读取可选 env 文件中的端点数组，不解析或返回密钥。"""
    path_value = os.environ.get("RAG_PREFLIGHT_ENV_FILE")
    if not path_value:
        return {}
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    for key in ENDPOINT_KEYS:
        matches = [line.partition("=")[2] for line in lines if line.startswith(f"{key}=")]
        if len(matches) == 1:
            values[key] = matches[0]
    return values


def endpoint_values() -> list[str] | None:
    """加载并验证端点 JSON 数组，返回空表示未配置。"""
    file_values = env_file_values()
    endpoints: list[str] = []
    for key in ENDPOINT_KEYS:
        raw = os.environ.get(key, file_values.get(key))
        if raw is None:
            return None
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("endpoint JSON invalid") from None
        if not isinstance(values, list) or not values:
            raise ValueError("endpoint array invalid")
        for value in values:
            if not isinstance(value, str):
                raise ValueError("endpoint item invalid")
            endpoints.append(value)
    return endpoints


def model_endpoints_check() -> dict[str, Any]:
    """对模型端点执行不带 token 的 TCP 基础连通性检查。"""
    try:
        endpoints = endpoint_values()
    except ValueError:
        return check("model_endpoints", FAIL, {"configured": True, "reachable": 0})
    if endpoints is None:
        return check("model_endpoints", WARN, {"configured": False, "reachable": 0})
    reachable = 0
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return check("model_endpoints", FAIL, {"configured": True, "reachable": reachable})
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((parsed.hostname, port), timeout=2):
                reachable += 1
        except OSError:
            continue
    status = PASS if reachable == len(endpoints) else FAIL
    return check(
        "model_endpoints",
        status,
        {"configured": True, "count": len(endpoints), "reachable": reachable},
    )


checks = [
    docker_check(),
    fixed_directory_check(),
    gpu_check(),
    model_endpoints_check(),
    ports_check(),
    runtime_state_check(),
    storage_check(),
]
statuses = {item["status"] for item in checks}
overall = FAIL if FAIL in statuses else WARN if WARN in statuses else PASS
print(json.dumps({"checks": checks, "overall": overall, "schema_version": "1"}, sort_keys=True))
raise SystemExit(1 if overall == FAIL else 0)
PY
