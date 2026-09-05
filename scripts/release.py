"""构建、验证并验收 P11 候选发布物。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import cast

from rag_app.product.live_acceptance import run_acceptance
from rag_app.product.release_evidence import (
    build_report,
    component_identity,
    evidence_identity,
    provider_operation_identities,
    vulnerability_report,
    write_report,
)

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_build_budget_plan = cast(
    Callable[[dict[str, object]], dict[str, object]],
    import_module("rag_app.product.budget_plan").build_p11_budget_plan,
)
_IMAGE = "docx-rag:v1-candidate"
_QDRANT_IMAGE = "qdrant/qdrant:v1.18.3"
_QDRANT_TEST_KEY = "test-only-qdrant-key"
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX = 300
_NPM_LOCKFILE_VERSION = 3
_TRIVY_IMAGE = (
    "aquasec/trivy@sha256:"
    "62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
)
_SYFT_IMAGE = (
    "anchore/syft@sha256:"
    "95fe0835e5bebc6f8b1f8acef68d47d63d594ef4c0f25c097ff853b23cbac74c"
)
_AUDIT_TRANSPORT_MARKERS = (
    "audit endpoint returned an error",
    "client network socket",
    "connectionerror",
    "connecttimeout",
    "econnrefused",
    "econnreset",
    "name resolution",
    "readtimeout",
    "socket hang up",
    "sslerror",
    "temporary failure",
    "timed out",
    "unexpected_eof_while_reading",
)


def _run(
    command: Sequence[str],
    *,
    cwd: Path = _ROOT,
    environment: dict[str, str] | None = None,
) -> None:
    """运行命令并保留原始返回码。

    Args:
        command: 不经过 shell 的参数列表。
        cwd: 命令工作目录。
        environment: 可选完整环境。

    Returns:
        命令成功时无返回值。

    Raises:
        subprocess.CalledProcessError: 子命令失败。

    """
    print("RUN " + " ".join(command), flush=True)
    subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        env=environment,
        check=True,
    )


def _capture(
    command: Sequence[str],
    *,
    cwd: Path = _ROOT,
    input_text: str | None = None,
) -> str:
    """运行只读命令并返回去除行尾的标准输出。

    Args:
        command: 不经过 shell 的参数列表。
        cwd: 命令工作目录。
        input_text: 可选标准输入；只传递不含 Secret 的验收配置。

    Returns:
        标准输出文本。

    Raises:
        subprocess.CalledProcessError: 子命令失败。

    """
    return subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    ).stdout.strip()


def _required_executable(name: str) -> str:
    """解析必须存在的命令。

    Args:
        name: PATH 中的命令名。

    Returns:
        已解析的命令路径。

    Raises:
        OSError: 命令不存在。

    """
    executable = shutil.which(name)
    if executable is None:
        raise OSError(f"缺少发布命令：{name}")
    return executable


def _revision() -> str:
    """返回当前候选提交 SHA。

    Args:
        无参数；读取当前 Git checkout。

    Returns:
        完整 Git SHA。

    """
    return _capture((_required_executable("git"), "rev-parse", "HEAD"))


def _build() -> None:
    """构建带当前 Git 身份的候选镜像。

    Args:
        无参数；使用固定候选镜像名。

    Returns:
        构建和 Compose 配置都成功时无返回值。

    """
    docker = _required_executable("docker")
    revision = _revision()
    _run(
        (
            docker,
            "build",
            "--build-arg",
            f"VCS_REF={revision}",
            "--tag",
            _IMAGE,
            ".",
        )
    )
    _run((docker, "compose", "config", "--quiet"))


def _artifact_directory(evidence_path: Path) -> Path:
    """创建并返回忽略跟踪的 P11 安全证据目录。

    Args:
        evidence_path: 本轮执行记录路径；每次扫描另存完整原始证据。

    Returns:
        已创建的安全证据目录。

    """
    directory = (
        evidence_path.parent
        / "security"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _verify_image_contract(docker: str) -> None:
    """验证候选镜像身份、用户和构建工具边界。

    Args:
        docker: Docker CLI 绝对路径。

    Returns:
        合同满足时无返回值。

    Raises:
        RuntimeError: 镜像身份或运行用户不符合候选合同。

    """
    payload = json.loads(_capture((docker, "image", "inspect", _IMAGE)))[0]
    config = cast(dict[str, object], payload["Config"])
    labels = cast(dict[str, str], config.get("Labels") or {})
    if config.get("User") != "rag:rag":
        raise RuntimeError("候选镜像必须使用 rag:rag 非 root 用户。")
    built_revision = labels.get("org.opencontainers.image.revision", "")
    if not re.fullmatch(r"[0-9a-f]{40}", built_revision):
        raise RuntimeError("候选镜像缺少可信 Git 身份。")
    changed_assets = _capture(
        (
            _required_executable("git"),
            "diff",
            "--name-only",
            built_revision,
            "--",
            "src",
            "frontend",
            ":(exclude)frontend/e2e/**",
            "migrations",
            "evaluation",
            "Dockerfile",
            ".dockerignore",
            "Dockerfile.dockerignore",
            "requirements.runtime.lock",
            "pyproject.toml",
            "compatibility-manifest.json",
            "docs/public/openapi-v1.json",
            "compose.yaml",
        )
    )
    if changed_assets:
        raise RuntimeError("候选镜像业务资产与当前 checkout 不一致。")
    _run(
        (
            docker,
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            _IMAGE,
            "-c",
            "test ! -e /usr/local/bin/node && "
            "test ! -e /usr/local/bin/pip && "
            "test ! -e /usr/local/bin/wheel",
        )
    )


def _write_license_inventory(sbom: Path, output: Path) -> None:
    """从 CycloneDX SBOM 生成确定性的许可证清单。

    Args:
        sbom: Syft 生成的 CycloneDX JSON。
        output: 清单输出路径。

    Returns:
        写入成功时无返回值。

    """
    payload = cast(
        dict[str, object], json.loads(sbom.read_text(encoding="utf-8"))
    )
    components = cast(list[dict[str, object]], payload.get("components", []))
    inventory: list[dict[str, object]] = []
    for component in components:
        licenses = cast(list[dict[str, object]], component.get("licenses", []))
        names: list[str] = []
        for item in licenses:
            license_value = cast(dict[str, object], item.get("license", {}))
            value = license_value.get("id") or license_value.get("name")
            if isinstance(value, str):
                names.append(value)
        inventory.append(
            {
                "licenses": sorted(set(names)),
                "name": component.get("name", ""),
                "purl": component.get("purl", ""),
                "version": component.get("version", ""),
            }
        )
    inventory.sort(
        key=lambda item: (
            str(item["name"]),
            str(item["version"]),
            str(item["purl"]),
        )
    )
    output.write_text(
        json.dumps(
            {"components": inventory, "schema_version": 1},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _locked_python_dependencies(lock_file: Path) -> list[tuple[str, str]]:
    """读取只允许精确版本的 Runtime 锁文件。

    Args:
        lock_file: `name==version` 格式的锁文件。

    Returns:
        按锁文件顺序排列的包名和版本。

    Raises:
        RuntimeError: 锁文件包含非精确版本或为空。

    """
    dependencies: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(
        lock_file.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise RuntimeError(
                f"{lock_file}:{line_number} 不是精确的 name==version。"
            )
        name, version = (item.strip() for item in line.split("==", 1))
        if not name or not version:
            raise RuntimeError(f"{lock_file}:{line_number} 缺少包名或版本。")
        dependencies.append((name, version))
    if not dependencies:
        raise RuntimeError(f"Runtime 锁文件为空：{lock_file}")
    return dependencies


def _locked_npm_dependencies(lock_file: Path) -> list[tuple[str, str]]:
    """读取 npm V3 lockfile 中的全部精确依赖版本。

    Args:
        lock_file: npm `package-lock.json` 文件。

    Returns:
        排序并去重后的包名和版本。

    Raises:
        RuntimeError: lockfile 合同无效或没有依赖。

    """
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("lockfileVersion") != _NPM_LOCKFILE_VERSION
    ):
        raise RuntimeError("前端依赖审计只接受 npm V3 lockfile。")
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise RuntimeError("npm lockfile 缺少 packages object。")
    dependencies: set[tuple[str, str]] = set()
    for location, metadata in packages.items():
        if not location:
            continue
        if not isinstance(location, str) or not isinstance(metadata, dict):
            raise RuntimeError("npm lockfile package 项无效。")
        version = metadata.get("version")
        name = metadata.get("name")
        if not isinstance(name, str):
            name = location.rsplit("node_modules/", 1)[-1]
        if not name or not isinstance(version, str) or not version:
            raise RuntimeError(f"npm lockfile 依赖缺少精确版本：{location}")
        dependencies.add((name, version))
    if not dependencies:
        raise RuntimeError("npm lockfile 没有可审计依赖。")
    return sorted(dependencies)


def _assert_osv_audit_clean(
    dependencies: Sequence[tuple[str, str]],
    payload: object,
) -> None:
    """验证 OSV 批量查询完整且没有漏洞结果。

    Args:
        dependencies: 本次提交给 OSV 的精确包版本。
        payload: OSV `querybatch` 返回的 JSON 值。

    Returns:
        每个依赖都有结果且均无漏洞时无返回值。

    Raises:
        RuntimeError: 响应合同不完整或发现漏洞。

    """
    if not isinstance(payload, dict):
        raise RuntimeError("OSV 依赖审计响应不是 JSON object。")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != len(dependencies):
        raise RuntimeError("OSV 依赖审计响应数量与锁文件不一致。")
    findings: list[str] = []
    for (name, version), result in zip(dependencies, results, strict=True):
        if not isinstance(result, dict):
            raise RuntimeError("OSV 依赖审计结果项不是 JSON object。")
        vulnerabilities = result.get("vulns", [])
        if not isinstance(vulnerabilities, list):
            raise RuntimeError("OSV 依赖审计漏洞字段不是数组。")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise RuntimeError("OSV 依赖审计漏洞项不是 JSON object。")
            identifier = vulnerability.get("id", "unknown")
            findings.append(f"{name}=={version}:{identifier}")
    if findings:
        raise RuntimeError("OSV 发现依赖漏洞：" + ", ".join(findings))


def _run_osv_dependency_audit(
    dependencies: Sequence[tuple[str, str]],
    ecosystem: str,
) -> None:
    """通过 OSV 官方批量接口审计锁定依赖。

    Args:
        dependencies: 需要检查的全部精确包版本。
        ecosystem: OSV 生态名称，例如 `PyPI` 或 `npm`。

    Returns:
        OSV 完整查询成功且无漏洞时无返回值。

    Raises:
        subprocess.CalledProcessError: curl 传输失败。
        RuntimeError: OSV 响应无效或发现漏洞。

    """
    curl = _required_executable("curl")
    request_payload = json.dumps(
        {
            "queries": [
                {
                    "package": {"ecosystem": ecosystem, "name": name},
                    "version": version,
                }
                for name, version in dependencies
            ]
        }
    )
    command = (
        curl,
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--header",
        "Content-Type: application/json",
        "--data-binary",
        "@-",
        "https://api.osv.dev/v1/querybatch",
    )
    print("RUN " + " ".join(command), flush=True)
    result = subprocess.run(  # noqa: S603
        command,
        cwd=_ROOT,
        input=request_payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr, flush=True)
        raise subprocess.CalledProcessError(result.returncode, command)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("OSV 依赖审计未返回有效 JSON。") from error
    _assert_osv_audit_clean(dependencies, payload)
    print(
        "OK osv-dependency-audit "
        f"packages={len(dependencies)} vulnerable_packages=0",
        flush=True,
    )


def _run_audit_command(
    command: Sequence[str],
    *,
    cwd: Path,
    blocked_status: str,
) -> bool:
    """运行原生审计并区分漏洞结果与传输故障。

    Args:
        command: 不经过 shell 的审计命令。
        cwd: 审计工作目录。
        blocked_status: 传输故障时输出的状态名称。

    Returns:
        原生审计成功为 True，可切换到等价审计为 False。

    Raises:
        subprocess.CalledProcessError: 审计失败且不是传输故障。

    """
    print("RUN " + " ".join(command), flush=True)
    result = subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = result.stdout or ""
    if output:
        print(output.rstrip(), flush=True)
    if result.returncode == 0:
        return True
    folded_output = output.casefold()
    if not any(marker in folded_output for marker in _AUDIT_TRANSPORT_MARKERS):
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=output,
        )
    print(f"{blocked_status}=BLOCKED; 尝试 OSV 官方等价审计。", flush=True)
    return False


def _audit_python_dependencies(lock_file: Path) -> None:
    """先运行 pip-audit，仅在传输失败时改用 OSV 等价审计。

    Args:
        lock_file: Runtime 精确依赖锁文件。

    Returns:
        任一真实审计路径完整成功且无漏洞时无返回值。

    """
    command = (sys.executable, "-m", "pip_audit", "-r", str(lock_file))
    if _run_audit_command(
        command,
        cwd=_ROOT,
        blocked_status="PIP_AUDIT_TRANSPORT",
    ):
        return
    _run_osv_dependency_audit(_locked_python_dependencies(lock_file), "PyPI")


def _audit_frontend_dependencies(lock_file: Path, npm: str) -> None:
    """先运行 npm audit，仅在传输失败时改用 OSV 等价审计。

    Args:
        lock_file: npm V3 lockfile。
        npm: npm CLI 路径。

    Returns:
        任一真实审计路径完整成功且无漏洞时无返回值。

    """
    if _run_audit_command(
        (
            npm,
            "audit",
            "--audit-level=moderate",
            "--fetch-retries=0",
            "--fetch-timeout=15000",
        ),
        cwd=lock_file.parent,
        blocked_status="NPM_AUDIT_TRANSPORT",
    ):
        return
    _run_osv_dependency_audit(_locked_npm_dependencies(lock_file), "npm")


def _verify(evidence_path: Path, review_overlay: Path) -> None:
    """执行依赖、镜像、Secret、SBOM 与许可证门禁。

    Args:
        evidence_path: 本轮独立执行证据汇总文件。
        review_overlay: 绑定完整扫描及实际处置证据的本地审查文件。

    Returns:
        全部门禁通过时无返回值。

    """
    docker = _required_executable("docker")
    npm = _required_executable("npm")
    output = _artifact_directory(evidence_path)
    _record_action(
        lambda: _audit_python_dependencies(_ROOT / "requirements.runtime.lock"),
        ("python_dependency_audit",),
        ("scripts/release.py", "verify", "internal:python-audit"),
        evidence_path,
    )
    _record_action(
        lambda: _audit_frontend_dependencies(
            _ROOT / "frontend/package-lock.json", npm
        ),
        ("npm_dependency_audit",),
        ("scripts/release.py", "verify", "internal:npm-audit"),
        evidence_path,
    )
    _verify_image_contract(docker)
    scan_command = (
        sys.executable,
        "scripts/secret_scan.py",
        "--docker-image",
        _IMAGE,
        "--path",
        "src",
        "--path",
        "frontend/dist",
    )
    _record_action(
        lambda: _run(scan_command),
        ("secret_scan", "image_secret_scan"),
        scan_command,
        evidence_path,
    )
    docker_socket = "/var/run/docker.sock:/var/run/docker.sock"
    artifact_mount = f"{output}:/output"
    trivy_cache = output / "trivy-cache"
    trivy_cache.mkdir(exist_ok=True)
    trivy_cache_mount = f"{trivy_cache}:/root/.cache/trivy"
    _run(
        (
            docker,
            "run",
            "--rm",
            "-v",
            docker_socket,
            "-v",
            artifact_mount,
            "-v",
            trivy_cache_mount,
            _TRIVY_IMAGE,
            "image",
            "--scanners",
            "vuln",
            "--format",
            "json",
            "--output",
            "/output/trivy-all.json",
            _IMAGE,
        )
    )
    risks = vulnerability_report(
        json.loads(
            (output / "trivy-all.json").read_text(encoding="utf-8"),
        ),
        overlay=_load_evidence(review_overlay)
        if review_overlay.is_file()
        else None,
        root=_ROOT,
        scan_path=output / "trivy-all.json",
    )
    if risks.get("image_id") != _current_identity()["image_id"]:
        risks.update(status="BLOCKED", reason="OS_SCAN_IMAGE_IDENTITY_MISMATCH")
    _save_evidence(output / "os-risk-review.json", risks)
    evidence = _load_evidence(evidence_path)
    checks = cast(dict[str, object], evidence.setdefault("checks", {}))
    review_path = output / "os-risk-review.json"
    checks["os_risk"] = {
        "status": risks["status"],
        "reason": risks["reason"],
        "identity": evidence_identity("os_risk", _current_identity()),
        "origin": "本次执行",
        "evidence": str(review_path),
        "exit_code": 0,
        "sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        "details": {
            "raw_scan": {
                "path": str(output / "trivy-all.json"),
                "sha256": hashlib.sha256(
                    (output / "trivy-all.json").read_bytes()
                ).hexdigest(),
            },
            "review_overlay": {
                "path": str(review_overlay),
                "sha256": hashlib.sha256(
                    review_overlay.read_bytes()
                ).hexdigest()
                if review_overlay.is_file()
                else None,
            },
            "risk_summary": {
                name: value
                for name, value in risks.items()
                if name != "findings"
            },
        },
    }
    _save_evidence(evidence_path, evidence)
    _run(
        (
            docker,
            "run",
            "--rm",
            "-v",
            docker_socket,
            "-v",
            artifact_mount,
            "-v",
            trivy_cache_mount,
            _TRIVY_IMAGE,
            "image",
            "--skip-db-update",
            "--scanners",
            "vuln",
            "--ignore-unfixed",
            "--exit-code",
            "1",
            "--severity",
            "HIGH,CRITICAL",
            "--format",
            "json",
            "--output",
            "/output/trivy-fixable.json",
            _IMAGE,
        )
    )
    sbom = output / "sbom-image.cdx.json"
    _run(
        (
            docker,
            "run",
            "--rm",
            "-v",
            docker_socket,
            "-v",
            artifact_mount,
            _SYFT_IMAGE,
            _IMAGE,
            "-o",
            "cyclonedx-json=/output/sbom-image.cdx.json",
        )
    )
    _write_license_inventory(sbom, output / "licenses-image.json")
    if risks["status"] != "PASS":
        raise RuntimeError(
            f"SECURITY_READY={risks['status']}: "
            f"全部 High/Critical={risks['all_high_critical']}，"
            f"可修复={risks['fixable_high_critical']}，"
            f"无修复={risks['without_fix']}；风险评估见 os-risk-review.json。"
        )
    print(f"OK release-verify evidence={output}")


def _free_loopback_port() -> int:
    """预留后返回一个当前空闲的 loopback TCP 端口。

    Args:
        无参数；由操作系统分配端口。

    Returns:
        当前空闲端口。

    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _qdrant_request(
    port: int,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    """向隔离 Qdrant 发起带测试 Key 的有限请求。

    Args:
        port: loopback 映射端口。
        method: HTTP 方法。
        path: Qdrant API 路径。
        body: 可选 JSON object。

    Returns:
        JSON object 响应。

    Raises:
        RuntimeError: 响应不是成功状态或 JSON object。

    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"api-key": _QDRANT_TEST_KEY}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        content = response.read()
    finally:
        connection.close()
    if not _HTTP_SUCCESS_MIN <= response.status < _HTTP_SUCCESS_MAX:
        raise RuntimeError(f"Qdrant 请求失败：HTTP {response.status}")
    parsed = json.loads(content or b"{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Qdrant 响应不是 JSON object。")
    return cast(dict[str, object], parsed)


def _wait_qdrant(port: int) -> None:
    """等待隔离 Qdrant 就绪。

    Args:
        port: loopback 映射端口。

    Returns:
        就绪时无返回值。

    Raises:
        TimeoutError: 30 秒内未就绪。

    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            _qdrant_request(port, "GET", "/collections")
            return
        except (OSError, RuntimeError, json.JSONDecodeError):
            time.sleep(0.2)
    raise TimeoutError("Qdrant 未在 30 秒内就绪。")


def _start_qdrant(
    docker: str,
    name: str,
    port: int,
    storage_volume: str,
) -> None:
    """启动只暴露 loopback 的隔离 Qdrant。

    Args:
        docker: Docker CLI 路径。
        name: 唯一容器名。
        port: loopback 端口。
        storage_volume: 本次验收唯一命名卷。

    Returns:
        容器就绪时无返回值。

    """
    _run(
        (
            docker,
            "run",
            "--detach",
            "--name",
            name,
            "--env",
            f"QDRANT__SERVICE__API_KEY={_QDRANT_TEST_KEY}",
            "--publish",
            f"127.0.0.1:{port}:6333",
            "--volume",
            f"{storage_volume}:/qdrant/storage",
            _QDRANT_IMAGE,
        )
    )
    _wait_qdrant(port)


def _qdrant_restart_probe(docker: str, name: str, port: int) -> None:
    """证明 Point 在 Qdrant Server 重启后仍存在。

    Args:
        docker: Docker CLI 路径。
        name: 源 Qdrant 容器名。
        port: loopback 映射端口。

    Returns:
        重启持久性成立时无返回值。

    """
    collection = "p11_restart_probe"
    _qdrant_request(
        port,
        "PUT",
        f"/collections/{collection}",
        {"vectors": {"dense_primary": {"size": 4, "distance": "Cosine"}}},
    )
    _qdrant_request(
        port,
        "PUT",
        f"/collections/{collection}/points?wait=true",
        {
            "points": [
                {
                    "id": 1,
                    "payload": {"probe": "public-synthetic"},
                    "vector": {"dense_primary": [1.0, 0.0, 0.0, 0.0]},
                }
            ]
        },
    )
    _run((docker, "restart", name))
    _wait_qdrant(port)
    persisted = _qdrant_request(
        port, "GET", f"/collections/{collection}/points/1"
    )
    if not cast(dict[str, object], persisted.get("result", {})).get("id") == 1:
        raise RuntimeError("Qdrant 重启后 Point 不存在。")
    _qdrant_request(port, "DELETE", f"/collections/{collection}")


def _cleanup_container(docker: str, name: str) -> None:
    """尽力删除本次验收创建的精确容器。

    Args:
        docker: Docker CLI 路径。
        name: 本次生成的唯一容器名。

    Returns:
        无返回值。

    """
    subprocess.run(  # noqa: S603
        (docker, "rm", "--force", name),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cleanup_volume(docker: str, name: str) -> None:
    """尽力删除本次验收创建的精确命名卷。

    Args:
        docker: Docker CLI 路径。
        name: 本次生成的唯一命名卷。

    Returns:
        无返回值。

    """
    subprocess.run(  # noqa: S603
        (docker, "volume", "rm", name),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _qdrant_acceptance() -> None:
    """执行双 Qdrant、快照恢复与重启持久性验收。

    Args:
        无参数；所有容器和目录均为本次隔离创建。

    Returns:
        全部 Qdrant 集成通过时无返回值。

    """
    docker = _required_executable("docker")
    suffix = f"{os.getpid()}-{time.time_ns()}"
    source_name = f"rag-p11-source-{suffix}"
    target_name = f"rag-p11-target-{suffix}"
    source_volume = f"rag-p11-source-data-{suffix}"
    target_volume = f"rag-p11-target-data-{suffix}"
    source_port = _free_loopback_port()
    target_port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="rag-p11-qdrant-") as temporary:
        root = Path(temporary)
        key_file = root / "qdrant-api-key"
        key_file.write_text(_QDRANT_TEST_KEY, encoding="utf-8")
        key_file.chmod(0o600)
        try:
            _run((docker, "volume", "create", source_volume))
            _run((docker, "volume", "create", target_volume))
            _start_qdrant(docker, source_name, source_port, source_volume)
            _start_qdrant(docker, target_name, target_port, target_volume)
            environment = dict(os.environ)
            environment.update(
                {
                    "RAG_P11_IMAGE_SIZE_BYTES": _capture(
                        (
                            docker,
                            "image",
                            "inspect",
                            "--format",
                            "{{.Size}}",
                            _IMAGE,
                        )
                    ),
                    "RAG_P11_PERFORMANCE_OUTPUT": str(
                        _ROOT / "artifacts" / "p11" / "performance.json"
                    ),
                    "RAG_QDRANT_API_KEY": _QDRANT_TEST_KEY,
                    "RAG_QDRANT_URL": f"http://127.0.0.1:{source_port}",
                    "RAG_TEST_QDRANT_SOURCE_KEY_FILE": str(key_file),
                    "RAG_TEST_QDRANT_SOURCE_URL": (
                        f"http://127.0.0.1:{source_port}"
                    ),
                    "RAG_TEST_QDRANT_TARGET_KEY_FILE": str(key_file),
                    "RAG_TEST_QDRANT_TARGET_URL": (
                        f"http://127.0.0.1:{target_port}"
                    ),
                }
            )
            _run(
                (
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/integration/test_qdrant_server_p11.py",
                    "tests/integration/test_product_backup_restore.py",
                    "tests/integration/test_p11_performance.py",
                ),
                environment=environment,
            )
            _qdrant_restart_probe(docker, source_name, source_port)
        finally:
            _cleanup_container(docker, target_name)
            _cleanup_container(docker, source_name)
            _cleanup_volume(docker, target_volume)
            _cleanup_volume(docker, source_volume)


def _acceptance(
    evidence_file: Path | None = None,
) -> None:
    """执行不需要真实 Provider 凭据的 P11 验收。

    Args:
        evidence_file: 可选执行记录；真实 Provider Live Gate 单独受授权控制。

    Returns:
        全部离线、浏览器、升级与 Qdrant 门禁通过时无返回值。

    """
    evidence_path = evidence_file or _ROOT / "artifacts/p11-r5/evidence.json"
    for command in (
        "doctor",
        "check",
        "smoke",
        "product-check",
        "product-smoke",
        "web-lint",
        "web-typecheck",
        "web-test",
        "web-e2e",
    ):
        command_line = (sys.executable, "scripts/dev.py", command)
        checks: tuple[str, ...] = (command.replace("-", "_"),)
        if command == "check":
            checks += (
                "authorization_tests",
                "endpoint_tests",
                "connection_tests",
                "conformance_tests",
                "publication_tests",
                "security_matrix",
            )
        _record_action(
            partial(_run, command_line),
            checks,
            command_line,
            evidence_path,
        )
    upgrade_command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/upgrade/test_p11_upgrade.py",
    )
    _record_action(
        lambda: _run(upgrade_command),
        ("upgrade_tests",),
        upgrade_command,
        evidence_path,
    )
    _record_action(
        _qdrant_acceptance,
        ("qdrant", "backup_restore"),
        ("scripts/release.py", "acceptance", "internal:qdrant"),
        evidence_path,
    )
    print("OK release-acceptance live_provider=NOT_RUN")


def _candidate_acceptance() -> None:
    """以正式入口验收隔离候选镜像，Provider 明确使用离线测试传输。"""
    docker = _required_executable("docker")
    _verify_image_contract(docker)
    project = f"rag-p11-candidate-{os.getpid()}-{time.time_ns()}"
    port = _free_loopback_port()
    environment = {
        **os.environ,
        "RAG_APP_IMAGE": _IMAGE,
        "RAG_PORT": str(port),
        "RAG_TRUSTED_ORIGINS": f"http://127.0.0.1:{port}",
        "P10_EXTERNAL_SERVER": "1",
        "P10_BASE_URL": f"http://127.0.0.1:{port}",
    }
    with tempfile.TemporaryDirectory(prefix="rag-p11-candidate-") as temporary:
        override = Path(temporary) / "compose-acceptance.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "app": {
                            "environment": {
                                "RAG_TEST_NETWORK": "offline",
                                "RAG_DEBUG_ENABLED": "true",
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        compose = (
            docker,
            "compose",
            "--project-name",
            project,
            "--file",
            str(_ROOT / "compose.yaml"),
            "--file",
            str(override),
        )
        try:
            _run(
                (
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "app",
                    "init-secrets",
                    "--directory",
                    "/run/rag-secrets",
                ),
                environment=environment,
            )
            # 只为新建的隔离测试卷设置公开合成口令，绝不访问用户 Secret 卷。
            bootstrap = (
                "from pathlib import Path; "
                "Path('/run/rag-secrets/admin-bootstrap-token').write_text("
                "'offline-bootstrap-credential', encoding='utf-8')"
            )
            _run(
                (
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "python",
                    "app",
                    "-c",
                    bootstrap,
                ),
                environment=environment,
            )
            _run(
                (
                    *compose,
                    "up",
                    "--detach",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "120",
                ),
                environment=environment,
            )
            _wait_candidate(port)
            _run(
                (sys.executable, "scripts/dev.py", "web-e2e"),
                environment=environment,
            )
            before = _candidate_inventory(compose)
            _run(
                (*compose, "restart", "app", "qdrant"), environment=environment
            )
            _wait_candidate(port)
            after = _candidate_inventory(compose)
            if before != after:
                raise RuntimeError("候选重启后产品数据或双槽向量身份变化。")
            _save_evidence(
                _ROOT / "artifacts/p11-r5/candidate-instance.json",
                {
                    "image_id": _capture(
                        (
                            docker,
                            "image",
                            "inspect",
                            "--format",
                            "{{.Id}}",
                            _IMAGE,
                        )
                    ),
                    "entrypoint": "rag-app serve",
                    "provider_mode": "offline_mock",
                    "external_provider_http": 0,
                    "inventory": after,
                    "ready": True,
                    "restart_persistence": True,
                },
            )
        finally:
            _run(
                (*compose, "down", "--volumes", "--remove-orphans"),
                environment=environment,
            )


def _wait_candidate(port: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/ready")
            response = connection.getresponse()
            if response.status == _HTTP_SUCCESS_MIN:
                response.read()
                return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.5)
    raise RuntimeError("候选实例未在期限内就绪。")


def _candidate_inventory(compose: Sequence[str]) -> dict[str, object]:
    program = (
        "import json,sqlite3; from pathlib import Path; "
        "from qdrant_client import QdrantClient; "
        "db=sqlite3.connect('file:/data/universal-rag.sqlite3?mode=ro',"
        "uri=True); "
        "counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] "
        "for t in ('projects','knowledge_bases','documents',"
        "'provider_connections')}; "
        "q=QdrantClient(url='http://qdrant:6333',"
        "api_key=Path('/run/rag-secrets/qdrant-api-key').read_text().strip(),"
        "check_compatibility=False); "
        "collections={c.name:q.get_collection(c.name).model_dump(mode='json') "
        "for c in q.get_collections().collections}; "
        "vectors={n:{'vectors':v['config']['params']['vectors'],"
        "'points':v['points_count']} "
        "for n,v in collections.items()}; "
        "assert any('dense_primary' in v['vectors'] and "
        "'dense_standby' in v['vectors'] and v['points']>0 "
        "for v in vectors.values()), 'DUAL_SLOT_INVENTORY_MISSING'; "
        "print(json.dumps({'counts':counts,'collections':vectors},sort_keys=True))"
    )
    return cast(
        dict[str, object],
        json.loads(
            _capture(
                (
                    *compose,
                    "exec",
                    "-T",
                    "app",
                    "python",
                    "-c",
                    program,
                )
            )
        ),
    )


def _current_identity() -> dict[str, str]:
    names = _capture(
        (
            _required_executable("git"),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        )
    ).split("\0")
    identity = component_identity(_ROOT, names)
    operation_identities = provider_operation_identities(_ROOT)
    identity.update(
        provider_jina=operation_identities["jina_connection"],
        provider_aliyun=operation_identities["aliyun_document_canary"],
        image_id="unavailable",
    )
    docker = shutil.which("docker")
    if docker is not None:
        try:
            identity["image_id"] = _capture(
                (
                    docker,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    _IMAGE,
                )
            )
        except subprocess.CalledProcessError:
            identity["image_id"] = "unavailable"
    return identity


def _load_evidence(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"checks": {}}
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _save_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _record_action(
    action: Callable[[], None],
    names: Sequence[str],
    command: Sequence[str],
    evidence_path: Path,
) -> None:
    evidence = _load_evidence(evidence_path)
    receipt: dict[str, object] = {
        "command": list(command),
        "started_at": datetime.now(UTC).isoformat(),
        "identity": _current_identity(),
        "exit_code": None,
    }
    try:
        action()
        receipt["exit_code"] = 0
    except subprocess.CalledProcessError as error:
        receipt["exit_code"] = error.returncode
        raise
    finally:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        receipt_path = evidence_path.parent / f"{names[0]}-execution.json"
        _save_evidence(receipt_path, receipt)
        record = {
            **receipt,
            "origin": "本次执行",
            "evidence": str(receipt_path),
            "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "status": "PASS" if receipt["exit_code"] == 0 else "FAIL",
            "reason": "COMMAND_EXIT_ZERO"
            if receipt["exit_code"] == 0
            else "COMMAND_FAILED",
        }
        checks = cast(dict[str, object], evidence.setdefault("checks", {}))
        for name in names:
            checks[name] = {
                **record,
                "identity": evidence_identity(
                    name, cast(dict[str, str], receipt["identity"])
                ),
            }
        _save_evidence(evidence_path, evidence)


def _campaign_container_metadata(
    docker: str, container: str
) -> dict[str, object]:
    """读取停机和挂载证据，不读取容器环境或健康检查输出。"""
    template = (
        '{"id":{{json .Id}},"image":{{json .Image}},'
        '"running":{{json .State.Running}},"pid":{{json .State.Pid}},'
        '"mounts":{{json .Mounts}}}'
    )
    return cast(
        dict[str, object],
        json.loads(
            _capture((docker, "inspect", "--format", template, container))
        ),
    )


def _stopped_campaign_volume(
    docker: str, container: str, candidate_image: str
) -> str:
    """仅允许已停机且无运行容器共享的命名数据卷进入绑定。"""
    target = _campaign_container_metadata(docker, container)
    if target["running"] is not False or target["pid"] != 0:
        raise RuntimeError("BLOCKED_MAINTENANCE_REQUIRED: 目标容器必须已停止。")
    if target["image"] != candidate_image:
        raise RuntimeError(
            "BLOCKED_CANDIDATE_IMAGE: 目标未使用已验证候选镜像。"
        )
    mounts = cast(list[dict[str, object]], target["mounts"])
    data_mounts = [item for item in mounts if item["Destination"] == "/data"]
    if len(data_mounts) != 1:
        raise RuntimeError("BLOCKED_DATA_VOLUME: 必须存在唯一 /data 命名卷。")
    data = data_mounts[0]
    if data["Type"] != "volume" or not data.get("Name") or not data.get("RW"):
        raise RuntimeError("BLOCKED_DATA_VOLUME: 不支持 bind mount 或只读卷。")
    for container_id in _capture((docker, "ps", "-q")).splitlines():
        peer = _campaign_container_metadata(docker, container_id)
        if peer["running"] is not True:
            continue
        for mount in cast(list[dict[str, object]], peer["mounts"]):
            if mount.get("Name") == data["Name"] or (
                data.get("Source") and mount.get("Source") == data["Source"]
            ):
                raise RuntimeError(
                    "BLOCKED_SHARED_DATA: 仍有运行容器挂载目标数据卷。"
                )
    # 检查期间不得替换或重新启动目标；不根据配置字段推定维护窗口。
    if _campaign_container_metadata(docker, container) != target:
        raise RuntimeError("BLOCKED_MAINTENANCE_CHANGED: 目标状态已变化。")
    return str(data["Name"])


def _campaign_binding_command(
    docker: str, container: str, config: dict[str, object]
) -> tuple[str, ...]:
    """为首绑构建断网辅助容器，只挂载已验证的产品数据卷。"""
    state_path = PurePosixPath(
        str(config.get("state_path", "/data/p11-live-state.sqlite3"))
    )
    if (
        config.get("data_dir") != "/data"
        or config.get("ledger_path", "/data/provider-budget.sqlite3")
        != "/data/provider-budget.sqlite3"
        or PurePosixPath("/data") not in state_path.parents
        or ".." in state_path.parts
    ):
        raise RuntimeError("BLOCKED_DATA_PATH: 绑定状态必须位于 /data 数据卷。")
    inspect_image = (docker, "image", "inspect", "--format", "{{.Id}}", _IMAGE)
    candidate_image = _capture(inspect_image)
    target_id = str(_campaign_container_metadata(docker, container)["id"])
    _stopped_campaign_volume(docker, target_id, candidate_image)
    _verify_image_contract(docker)
    if _capture(inspect_image) != candidate_image:
        raise RuntimeError("BLOCKED_CANDIDATE_IMAGE: 镜像验证期间标签已变化。")
    volume = _stopped_campaign_volume(docker, target_id, candidate_image)
    config["maintenance_confirmed"] = True
    return (
        docker,
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--no-healthcheck",
        "--mount",
        f"type=volume,src={volume},dst=/data",
        "--entrypoint",
        "python",
        candidate_image,
    )


@contextmanager
def _campaign_maintenance(
    docker: str, container: str
) -> Iterator[dict[str, object]]:
    """只恢复进入维护前运行的目标 App，失败也不遗留额外停机。"""
    original = _campaign_container_metadata(docker, container)
    target_id = str(original["id"])
    was_running = original["running"] is True
    receipt: dict[str, object] = {
        "app_was_running": was_running,
        "app_restored": False,
        "qdrant_action": "NONE",
    }
    try:
        if was_running:
            _capture((docker, "stop", target_id))
        yield receipt
    finally:
        # 即使 stop 或辅助容器失败，也按进入时的状态恢复原实例。
        if was_running:
            _capture((docker, "start", target_id))
            restored = _campaign_container_metadata(docker, target_id)
            if restored["running"] is not True:
                raise RuntimeError("APP_RESTORE_FAILED: 原 App 未恢复运行。")
            receipt["app_restored"] = True


def _run_live_acceptance(args: argparse.Namespace) -> dict[str, object]:
    config = (
        None
        if args.config is None
        else json.loads(args.config.read_text(encoding="utf-8"))
    )
    binding = args.bind_campaign or (
        isinstance(config, dict) and config.get("bind_campaign") is True
    )
    if binding and (
        args.live
        or not args.container
        or any(
            step.strip() not in {"config_check", "final_report"}
            for argument in args.steps or ()
            for step in argument.split(",")
            if step.strip()
        )
    ):
        raise RuntimeError(
            "BLOCKED_BIND_ARGUMENTS: 绑定需要指定 --container，"
            "且不能包含 --live 或付费步骤。"
        )
    if binding and not isinstance(config, dict):
        raise RuntimeError("BLOCKED_BIND_CONFIG: 绑定需要非秘密配置。")
    if config is not None:
        config["operation_identities"] = provider_operation_identities(_ROOT)
        if binding:
            config["bind_campaign"] = True
        identity = _current_identity()
        config["candidate_identity"] = hashlib.sha256(
            json.dumps(
                {
                    name: identity[name]
                    for name in ("runtime", "image", "migrations", "evaluation")
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
    steps = (
        None
        if args.steps is None
        else tuple(
            step
            for argument in args.steps
            for step in argument.split(",")
            if step
        )
    )
    if args.container:
        docker = _required_executable("docker")
        command: tuple[str, ...] = (
            docker,
            "exec",
            "-i",
            args.container,
            "python",
        )
        if args.live:
            _verify_image_contract(docker)
            container_image = _capture(
                (
                    docker,
                    "inspect",
                    "--format",
                    "{{.Image}}",
                    args.container,
                )
            )
            candidate_image = _capture(
                (
                    docker,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    _IMAGE,
                )
            )
            if container_image != candidate_image:
                raise RuntimeError("目标实例尚未使用已验证候选镜像。")
        payload = {
            "config": config,
            "steps": steps,
            "resume": False if binding else args.resume,
            "live": False if binding else args.live,
        }
        program = (
            "import json,sys; "
            "from rag_app.product.live_acceptance import run_acceptance; "
            "p=json.load(sys.stdin); "
            "print('P11_ACCEPTANCE_RESULT='+json.dumps(run_acceptance(**p)))"
        )
        if binding:
            with _campaign_maintenance(docker, args.container) as maintenance:
                command = _campaign_binding_command(
                    docker, args.container, cast(dict[str, object], config)
                )
                payload["steps"] = ("config_check",)
                report = _capture_acceptance(command, program, payload)
            report["maintenance"] = maintenance
            return report
        return _capture_acceptance(command, program, payload)
    return run_acceptance(
        config, steps=steps, resume=args.resume, live=args.live
    )


def _capture_acceptance(
    command: Sequence[str], program: str, payload: dict[str, object]
) -> dict[str, object]:
    output = _capture((*command, "-c", program), input_text=json.dumps(payload))
    for line in reversed(output.splitlines()):
        if line.startswith("P11_ACCEPTANCE_RESULT="):
            return cast(dict[str, object], json.loads(line.split("=", 1)[1]))
    raise RuntimeError("容器未生成可验证的验收报告。")


def _write_acceptance_report(args: argparse.Namespace) -> int:
    evidence = _load_evidence(args.evidence_file)
    live_report = _run_live_acceptance(args)
    live_path = args.evidence_file.parent / "live-result.json"
    _save_evidence(live_path, live_report)
    # Runner 会保留全部阶段；未选中项不能被本次选中的成功项覆盖。
    evidence["live_runner"] = live_report
    identity = _current_identity()
    checks = cast(dict[str, object], evidence.setdefault("checks", {}))
    step_records = cast(
        dict[str, dict[str, object]], live_report.get("steps", {})
    )
    for name, value in step_records.items():
        check_name = "campaign_config" if name == "config_check" else name
        checks[check_name] = {
            "status": value["status"],
            "reason": value["reason"],
            "identity": evidence_identity(check_name, identity),
            "step_identity": value.get("identity"),
            "origin": value.get("provenance", "未执行"),
            "evidence": str(live_path),
            "sha256": hashlib.sha256(live_path.read_bytes()).hexdigest(),
            "exit_code": 0 if value["status"] == "PASS" else None,
            "details": value.get("evidence", {}),
        }
    configuration = cast(
        dict[str, object],
        step_records.get("config_check", {}).get("evidence", {}),
    )
    for name in (
        "endpoint_contract",
        "connection_configuration",
        "campaign_binding",
    ):
        status = str(configuration.get(name, "NOT_RUN"))
        checks[name] = {
            **cast(dict[str, object], checks.get("campaign_config", {})),
            "status": status,
            "reason": "LOCAL_DIAGNOSTIC_PASSED"
            if status == "PASS"
            else ",".join(
                str(item["reason_code"])
                for item in cast(
                    list[dict[str, object]], configuration.get("issues", [])
                )
                if item["blocking_scope"] == name
            )
            or "DIAGNOSTIC_REQUIRED",
            "identity": evidence_identity(name, identity),
            "exit_code": 0 if status == "PASS" else None,
        }
    if "budget" in live_report:
        evidence["budget"] = live_report["budget"]
    _save_evidence(args.evidence_file, evidence)
    report = build_report(_ROOT, evidence, identity)
    report["live_runner"] = live_report
    selected_status = str(live_report.get("selected_steps_status", "NOT_RUN"))
    report["selected_steps_status"] = selected_status
    report["overall_release_status"] = cast(
        dict[str, dict[str, object]], report["gates"]
    )["P11_READY"]["status"]
    report["live_allowed"] = False
    write_report(report, args.report_output)
    print(f"REPORT {args.report_output} P11_READY={report['P11_READY']}")
    if args.exit_scope == "selected":
        return (
            0
            if selected_status == "PASS"
            else 1
            if selected_status == "FAIL"
            else 2
        )
    return 0 if report["P11_READY"] else 2


def _parser() -> argparse.ArgumentParser:
    """构建发布、零调用预算计划与分步验收入口。

    Args:
        无参数；仅解析已实现的子命令与显式作用域。

    Returns:
        发布参数解析器。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("build", "verify", "acceptance", "budget-plan")
    )
    parser.add_argument(
        "--resume", action="store_true", help="复用有效记录，仅续跑选中验收阶段"
    )
    parser.add_argument(
        "--steps", nargs="+", help="config_check、canary 等阶段；支持逗号分隔"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="启用已持久授权 campaign 内的真实调用",
    )
    parser.add_argument(
        "--config", type=Path, help="非秘密验收配置；不接受 API Key"
    )
    parser.add_argument(
        "--container", help="在已有候选容器内使用其页面托管连接"
    )
    parser.add_argument(
        "--bind-campaign",
        action="store_true",
        help="仅停止目标App，断网首绑旧账，finally恢复原运行状态",
    )
    parser.add_argument(
        "--exit-scope",
        choices=("release", "selected"),
        default="release",
        help="默认按整个发布退出；selected只反映所选动作，不能用于发布放行",
    )
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="对当前候选镜像执行隔离Compose、浏览器及持久性验收",
    )
    parser.add_argument(
        "--evidence-file",
        type=Path,
        default=_ROOT / "artifacts/p11-r5/evidence.json",
    )
    parser.add_argument(
        "--risk-review",
        type=Path,
        default=_ROOT / "release/p11-os-risk-review.json",
        help="完整扫描的本地处置overlay；计划和未批准风险保持阻断",
    )
    parser.add_argument(
        "--budget-history",
        type=Path,
        default=_ROOT / "release/p11-blocker-diagnosis.json",
        help="只读诊断historical_budget或现有ledger.summary，不能替代预算批准",
    )
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=_ROOT / "release/p11-budget-plan.json",
        help="仅写PROPOSED预算估计，不修改campaign或尝试记录",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=_ROOT / "release/p11-repair-acceptance.json",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """执行发布命令并透明返回失败状态。

    Args:
        arguments: 可选命令参数；默认读取进程参数。

    Returns:
        成功为 0，缺少工具为 2，子命令失败为其原始返回码。

    """
    args = _parser().parse_args(arguments)
    command = args.command
    try:
        if command == "build":
            _build()
        elif command == "verify":
            _verify(args.evidence_file, args.risk_review)
        elif command == "budget-plan":
            _write_budget_plan(args)
        else:
            config_binding = False
            if args.config is not None:
                config = json.loads(args.config.read_text(encoding="utf-8"))
                config_binding = (
                    isinstance(config, dict)
                    and config.get("bind_campaign") is True
                )
            if (
                not args.resume
                and args.steps is None
                and not args.live
                and not args.bind_campaign
                and not config_binding
            ):
                _acceptance(args.evidence_file)
            if args.candidate:
                _record_action(
                    _candidate_acceptance,
                    ("candidate_startup", "candidate_browser"),
                    ("scripts/release.py", "acceptance", "--candidate"),
                    args.evidence_file,
                )
            return _write_acceptance_report(args)
    except OSError as error:
        print(f"BLOCKED release: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        return error.returncode
    except (RuntimeError, ValueError) as error:
        print(f"BLOCKED release: {error}", file=sys.stderr)
        return 2
    return 0


def _write_budget_plan(args: argparse.Namespace) -> None:
    history_document = _load_evidence(args.budget_history)
    history = history_document.get("historical_budget", history_document)
    if (
        not isinstance(history, dict)
        or not {"reserved", "estimated_input_tokens", "providers"}
        <= history.keys()
    ):
        raise ValueError("BUDGET_HISTORY_REQUIRED: 必须提供已核对的累计账。")
    plan = _build_budget_plan(history)
    plan["history_evidence"] = {
        "path": str(args.budget_history),
        "sha256": hashlib.sha256(args.budget_history.read_bytes()).hexdigest(),
    }
    _save_evidence(args.plan_output, plan)
    print(f"PROPOSED budget-plan {args.plan_output} activated=false")


if __name__ == "__main__":
    raise SystemExit(main())
