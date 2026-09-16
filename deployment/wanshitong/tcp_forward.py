#!/usr/bin/env python3
"""在缺少 socat 时提供有界、可逆的私网 TCP 转发。"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import signal
from contextlib import suppress

BUFFER_BYTES = 64 * 1024
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
) -> None:
    """单向复制字节，EOF 后只关闭对应写方向。"""
    try:
        while chunk := await reader.read(BUFFER_BYTES):
            writer.write(chunk)
            await writer.drain()
        with suppress(OSError, RuntimeError):
            writer.write_eof()
    except (ConnectionError, asyncio.CancelledError):
        pass


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    target_host: str,
    target_port: int,
    semaphore: asyncio.Semaphore,
) -> None:
    """在并发上限内建立一次双向 TCP relay。"""
    async with semaphore:
        try:
            target_reader, target_writer = await asyncio.open_connection(
                target_host, target_port
            )
        except (ConnectionError, OSError):
            client_writer.close()
            with suppress(ConnectionError):
                await client_writer.wait_closed()
            return
        try:
            await asyncio.gather(
                _copy_stream(client_reader, target_writer),
                _copy_stream(target_reader, client_writer),
            )
        finally:
            target_writer.close()
            client_writer.close()
            with suppress(ConnectionError):
                await target_writer.wait_closed()
            with suppress(ConnectionError):
                await client_writer.wait_closed()


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
            target_host=arguments.target_host,
            target_port=arguments.target_port,
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
