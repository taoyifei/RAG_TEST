#!/usr/bin/env python3
"""切换边缘入口前，只读核对受信代理 IP 与 SSO 回调 Origin。"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import shutil
import subprocess
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

_HTTP_FOUND = 302


def _inspect_container(name: str) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        raise FileNotFoundError("找不到 docker 命令")
    result = subprocess.run(  # noqa: S603 - 参数数组不经过 shell。
        [docker, "inspect", name],
        check=True,
        capture_output=True,
        text=True,
    )
    items = json.loads(result.stdout)
    if len(items) != 1 or items[0]["State"]["Running"] is not True:
        raise ValueError(f"容器未运行或身份不唯一: {name}")
    return items[0]


def _edge_ip(
    edge: dict[str, Any], gateway: dict[str, Any], network: str | None
) -> tuple[str, str]:
    edge_networks = edge["NetworkSettings"]["Networks"]
    gateway_networks = gateway["NetworkSettings"]["Networks"]
    shared = set(edge_networks) & set(gateway_networks)
    if network is None:
        if len(shared) != 1:
            raise ValueError(
                "edge 与 gateway 必须只共享一个网络；多网络时请指定 --network"
            )
        network = shared.pop()
    elif network not in shared:
        raise ValueError(f"edge 与 gateway 未共同连接网络: {network}")
    address = edge_networks[network].get("IPAddress", "")
    if not address:
        raise ValueError(f"edge 在 {network} 上没有 IPv4 地址")
    return network, str(ipaddress.ip_address(address))


def _trusted_proxy_ips(gateway: dict[str, Any]) -> set[str]:
    environment = gateway["Config"].get("Env") or []
    values = [
        item.partition("=")[2]
        for item in environment
        if item.startswith("RAG_TRUSTED_PROXIES=")
    ]
    if len(values) != 1:
        raise ValueError("网关缺少唯一的 RAG_TRUSTED_PROXIES 配置")
    return {
        str(ipaddress.ip_address(item.strip()))
        for item in values[0].split(",")
        if item.strip()
    }


def _check_sso_entry(edge_url: str, origin: str) -> str:
    parsed_edge = urlsplit(edge_url)
    if (
        parsed_edge.scheme != "http"
        or not parsed_edge.hostname
        or parsed_edge.path
        or parsed_edge.query
        or parsed_edge.fragment
    ):
        raise ValueError("--edge-url 必须是本机可访问、无路径的 HTTP 地址")
    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme not in {"http", "https"}
        or not parsed_origin.netloc
        or parsed_origin.path
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise ValueError("--origin 必须是完整、无路径的 HTTP(S) Origin")
    connection = http.client.HTTPConnection(
        parsed_edge.hostname, parsed_edge.port, timeout=5
    )
    try:
        connection.request(
            "GET",
            "/kb/sso/entry?return_to=%2Fkb%2F",
            headers={"Host": parsed_origin.netloc},
        )
        response = connection.getresponse()
        status = response.status
        location = response.getheader("Location", "")
    finally:
        connection.close()
    if status != _HTTP_FOUND:
        raise ValueError(f"SSO 首跳返回 HTTP {status}，预期 302")
    service = parse_qs(urlsplit(location).query).get("service", [None])[0]
    expected = origin + "/kb/sso/callback"
    if service != expected:
        raise ValueError(
            f"SSO 回调地址不符：实际 {service!r}，预期 {expected!r}"
        )
    return expected


def main() -> int:
    """执行只读容器核对与 SSO 首跳核对。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edge", help="待切换的 edge 容器名或 ID")
    parser.add_argument("gateway", help="edge 实际连接的 gateway 容器名或 ID")
    parser.add_argument("--network", help="edge 与 gateway 共享的 Docker 网络")
    parser.add_argument(
        "--edge-url", required=True, help="从本机可访问的 edge HTTP 地址"
    )
    parser.add_argument(
        "--origin", required=True, help="浏览器使用的完整 Origin"
    )
    args = parser.parse_args()
    try:
        edge = _inspect_container(args.edge)
        gateway = _inspect_container(args.gateway)
        network, edge_ip = _edge_ip(edge, gateway, args.network)
        trusted = _trusted_proxy_ips(gateway)
        if edge_ip not in trusted:
            raise ValueError(
                f"{network} 中 edge IP {edge_ip} 未列入 gateway 的 "
                "RAG_TRUSTED_PROXIES"
            )
        callback = _check_sso_entry(args.edge_url, args.origin)
    except (
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"edge_sso_preflight=FAIL reason={error}", file=sys.stderr)
        return 1
    print(
        "edge_sso_preflight=PASS "
        f"network={network} edge_ip={edge_ip} callback={callback}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
