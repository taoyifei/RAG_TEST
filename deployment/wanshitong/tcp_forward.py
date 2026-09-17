#!/usr/bin/env python3
"""在缺少 socat 时提供有界、可逆的私网 TCP 转发。"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import signal
from contextlib import suppress
from dataclasses import dataclass

BUFFER_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 5
IDLE_TIMEOUT_SECONDS = 120
CLOSE_TIMEOUT_SECONDS = 2
DEFAULT_MAX_CONNECTIONS = 32
MAX_CONNECTIONS = 256
MAX_PORT = 65535


def _private_or_loopback_ip(value: str) -> str:
    """只接受精确私网或回环 IP，拒绝通配监听与主机名。"""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须提供精确 IP 地址。") from None
    if address.is_unspecified or not (
        address.is_private or address.is_loopback
    ):
        raise argparse.ArgumentTypeError("只允许私网或回环 IP。")
    return str(address)


async def _copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    activity: _Activity,
) -> None:
    """单向复制字节，EOF 后只关闭对应写方向。"""
    try:
        while chunk := await reader.read(BUFFER_BYTES):
            writer.write(chunk)
            await writer.drain()
            activity.last_bytes_at = asyncio.get_running_loop().time()
        with suppress(OSError, RuntimeError):
            writer.write_eof()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass


@dataclass
class _Activity:
    """记录两个方向最近一次成功转发字节的时间。"""

    last_bytes_at: float


async def _wait_for_transfer(
    client_to_target: asyncio.Task[None],
    target_to_client: asyncio.Task[None],
    activity: _Activity,
    idle_timeout: float,
) -> None:
    """目标端 EOF 后释放连接；客户端半关闭时仍等待目标端响应。"""
    pending = {client_to_target, target_to_client}
    loop = asyncio.get_running_loop()
    while pending:
        remaining = max(
            0.0, idle_timeout - (loop.time() - activity.last_bytes_at)
        )
        done, pending = await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            if loop.time() - activity.last_bytes_at >= idle_timeout:
                return
            continue
        for task in done:
            task.result()
        if target_to_client in done:
            return


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    """限制关闭耗时，避免异常连接长期占用并发槽。"""
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT_SECONDS)
    except (ConnectionError, OSError, TimeoutError):
        writer.transport.abort()


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    target: tuple[str, int],
    semaphore: asyncio.Semaphore,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
) -> None:
    """在并发上限内建立一次双向 TCP relay。"""
    async with semaphore:
        try:
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(*target),
                CONNECT_TIMEOUT_SECONDS,
            )
        except (ConnectionError, OSError, TimeoutError):
            await _close_writer(client_writer)
            return
        activity = _Activity(asyncio.get_running_loop().time())
        client_to_target = asyncio.create_task(
            _copy_stream(client_reader, target_writer, activity)
        )
        target_to_client = asyncio.create_task(
            _copy_stream(target_reader, client_writer, activity)
        )
        try:
            await _wait_for_transfer(
                client_to_target,
                target_to_client,
                activity,
                idle_timeout,
            )
        finally:
            for task in (client_to_target, target_to_client):
                task.cancel()
            await asyncio.gather(
                client_to_target, target_to_client, return_exceptions=True
            )
            await _close_writer(target_writer)
            await _close_writer(client_writer)


async def _serve(arguments: argparse.Namespace) -> None:
    """监听一个精确地址，直到收到 TERM 或 INT。"""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(event, stop.set)
    semaphore = asyncio.Semaphore(arguments.max_connections)

    async def accept(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _relay(
            reader,
            writer,
            target=(arguments.target_host, arguments.target_port),
            semaphore=semaphore,
        )

    server = await asyncio.start_server(
        accept,
        arguments.listen_host,
        arguments.listen_port,
        reuse_address=True,
    )
    print(
        "tcp-forward=ready "
        f"listen={arguments.listen_host}:{arguments.listen_port} "
        f"target={arguments.target_host}:{arguments.target_port}",
        flush=True,
    )
    async with server:
        await stop.wait()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把一个精确私网监听地址转发到精确私网/回环目标。"
    )
    parser.add_argument(
        "--listen-host", type=_private_or_loopback_ip, required=True
    )
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument(
        "--target-host", type=_private_or_loopback_ip, required=True
    )
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument(
        "--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """校验端口并运行前台 relay，交由受控 nohup/systemd 管理。"""
    arguments = _parser().parse_args(argv)
    ports = (arguments.listen_port, arguments.target_port)
    if any(port < 1 or port > MAX_PORT for port in ports):
        raise SystemExit("端口必须位于 1..65535。")
    if (
        arguments.max_connections < 1
        or arguments.max_connections > MAX_CONNECTIONS
    ):
        raise SystemExit("max-connections 必须位于 1..256。")
    asyncio.run(_serve(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
