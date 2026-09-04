"""构建、验证并验收 P11 候选发布物。"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[1]
_IMAGE = "docx-rag:v1-candidate"
_QDRANT_IMAGE = "qdrant/qdrant:v1.18.3"
_QDRANT_TEST_KEY = "test-only-qdrant-key"
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX = 300
_TRIVY_IMAGE = (
    "aquasec/trivy@sha256:"
    "62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
)
_SYFT_IMAGE = (
    "anchore/syft@sha256:"
    "95fe0835e5bebc6f8b1f8acef68d47d63d594ef4c0f25c097ff853b23cbac74c"
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


def _capture(command: Sequence[str], *, cwd: Path = _ROOT) -> str:
    """运行只读命令并返回去除行尾的标准输出。

    Args:
        command: 不经过 shell 的参数列表。
        cwd: 命令工作目录。

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


def _artifact_directory() -> Path:
    """创建并返回忽略跟踪的 P11 安全证据目录。

    Args:
        无参数；路径固定在 artifacts 下。

    Returns:
        已创建的安全证据目录。

    """
    directory = _ROOT / "artifacts" / "p11" / "security"
    directory.mkdir(parents=True, exist_ok=True)
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
    if labels.get("org.opencontainers.image.revision") != _revision():
        raise RuntimeError("候选镜像 Git SHA 与当前 checkout 不一致。")
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


def _verify() -> None:
    """执行依赖、镜像、Secret、SBOM 与许可证门禁。

    Args:
        无参数；验证固定候选镜像和锁文件。

    Returns:
        全部门禁通过时无返回值。

    """
    docker = _required_executable("docker")
    npm = _required_executable("npm")
    output = _artifact_directory()
    _run(
        (
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            "requirements.runtime.lock",
        )
    )
    _run((npm, "audit", "--audit-level=moderate"), cwd=_ROOT / "frontend")
    _verify_image_contract(docker)
    _run(
        (
            sys.executable,
            "scripts/secret_scan.py",
            "--docker-image",
            _IMAGE,
            "--path",
            "src",
            "--path",
            "frontend/dist",
        )
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
                ),
                environment=environment,
            )
            _qdrant_restart_probe(docker, source_name, source_port)
        finally:
            _cleanup_container(docker, target_name)
            _cleanup_container(docker, source_name)
            _cleanup_volume(docker, target_volume)
            _cleanup_volume(docker, source_volume)


def _acceptance() -> None:
    """执行不需要真实 Provider 凭据的 P11 验收。

    Args:
        无参数；真实 Provider Live Gate 单独受用户授权控制。

    Returns:
        全部离线、浏览器、升级与 Qdrant 门禁通过时无返回值。

    """
    for command in (
        "doctor",
        "check",
        "smoke",
        "product-check",
        "product-smoke",
        "web-e2e",
    ):
        _run((sys.executable, "scripts/dev.py", command))
    _run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/upgrade/test_p11_upgrade.py",
        )
    )
    _qdrant_acceptance()
    print("OK release-acceptance live_provider=NOT_RUN")


def _parser() -> argparse.ArgumentParser:
    """构建三个稳定发布入口。

    Args:
        无参数；命令固定为 build、verify、acceptance。

    Returns:
        发布参数解析器。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify", "acceptance"))
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """执行发布命令并透明返回失败状态。

    Args:
        arguments: 可选命令参数；默认读取进程参数。

    Returns:
        成功为 0，缺少工具为 2，子命令失败为其原始返回码。

    """
    command = _parser().parse_args(arguments).command
    try:
        if command == "build":
            _build()
        elif command == "verify":
            _verify()
        else:
            _acceptance()
    except OSError as error:
        print(f"BLOCKED release: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        return error.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
