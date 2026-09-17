"""验证私网转发器不会被半关闭连接耗尽并发槽。"""

import asyncio

from deployment.wanshitong import tcp_forward


def test_upstream_eof_releases_slot_before_client_closes() -> None:
    async def scenario() -> None:
        async def backend(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readexactly(4)
            writer.write(b"PONG")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(backend, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        semaphore = asyncio.Semaphore(1)

        async def relay(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await tcp_forward._relay(
                reader,
                writer,
                target=("127.0.0.1", upstream_port),
                semaphore=semaphore,
            )

        proxy = await asyncio.start_server(relay, "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        first_writer: asyncio.StreamWriter | None = None
        second_writer: asyncio.StreamWriter | None = None
        try:
            first_reader, first_writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port
            )
            first_writer.write(b"PING")
            await first_writer.drain()
            assert await first_reader.readexactly(4) == b"PONG"
            assert await asyncio.wait_for(first_reader.read(), 1) == b""
            # 客户端故意不关闭；上游 EOF 必须已经释放唯一的并发槽。
            second_reader, second_writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port
            )
            second_writer.write(b"PING")
            await second_writer.drain()
            assert await asyncio.wait_for(
                second_reader.readexactly(4), 1
            ) == b"PONG"
            assert await asyncio.wait_for(second_reader.read(), 1) == b""
        finally:
            for writer in (first_writer, second_writer):
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
            proxy.close()
            upstream.close()
            await proxy.wait_closed()
            await upstream.wait_closed()

    asyncio.run(scenario())


def test_client_half_close_preserves_delayed_upstream_response() -> None:
    async def scenario() -> None:
        async def backend(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            assert await reader.read() == b"PING"
            await asyncio.sleep(0.02)
            writer.write(b"PONG")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(backend, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        semaphore = asyncio.Semaphore(1)

        async def relay(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await tcp_forward._relay(
                reader,
                writer,
                target=("127.0.0.1", upstream_port),
                semaphore=semaphore,
            )

        proxy = await asyncio.start_server(relay, "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        client_writer: asyncio.StreamWriter | None = None
        try:
            reader, client_writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port
            )
            client_writer.write(b"PING")
            await client_writer.drain()
            client_writer.write_eof()
            assert await asyncio.wait_for(reader.readexactly(4), 1) == b"PONG"
        finally:
            if client_writer is not None:
                client_writer.close()
                await client_writer.wait_closed()
            proxy.close()
            upstream.close()
            await proxy.wait_closed()
            await upstream.wait_closed()

    asyncio.run(scenario())


def test_idle_connection_releases_slot() -> None:
    async def scenario() -> None:
        async def backend(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.read()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(backend, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        semaphore = asyncio.Semaphore(1)

        async def relay(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await tcp_forward._relay(
                reader,
                writer,
                target=("127.0.0.1", upstream_port),
                semaphore=semaphore,
                idle_timeout=0.05,
            )

        proxy = await asyncio.start_server(relay, "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port
            )
            assert await asyncio.wait_for(reader.read(), 1) == b""
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            proxy.close()
            upstream.close()
            await proxy.wait_closed()
            await upstream.wait_closed()

    asyncio.run(scenario())
