"""公共问答四运行、八等待的有界准入合同。"""

from __future__ import annotations

import asyncio

import pytest

from wanshitong_gateway.admission import (
    BoundedQaQueue,
    QueueClientDisconnectedError,
    QueueFullError,
    QueueWaitTimeoutError,
)


def test_four_active_eight_waiting_fifo_and_thirteenth_rejected() -> None:
    async def scenario() -> None:
        queue = BoundedQaQueue()
        active = [await queue.acquire() for _ in range(4)]
        waiting = [asyncio.create_task(queue.acquire()) for _ in range(8)]
        await asyncio.sleep(0)
        assert queue.snapshot() == {
            "active": 4,
            "waiting": 8,
            "max_active": 4,
            "max_waiting": 8,
        }
        with pytest.raises(QueueFullError):
            await queue.acquire()
        for index, held in enumerate(active):
            held.release()
            granted = await asyncio.wait_for(waiting[index], timeout=1)
            assert queue.snapshot()["active"] == 4
            granted.release()
        grants = await asyncio.gather(*waiting[4:])
        assert queue.snapshot()["active"] == 4
        for grant in grants:
            grant.release()
        assert queue.snapshot()["active"] == 0
        assert queue.snapshot()["waiting"] == 0

    asyncio.run(scenario())


def test_wait_timeout_and_cancellation_remove_waiters() -> None:
    async def scenario() -> None:
        queue = BoundedQaQueue(
            max_active=1, max_waiting=1, wait_timeout_seconds=0.02
        )
        held = await queue.acquire()
        with pytest.raises(QueueWaitTimeoutError):
            await queue.acquire()
        assert queue.snapshot()["waiting"] == 0
        task = asyncio.create_task(queue.acquire())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert queue.snapshot()["waiting"] == 0
        held.release()
        held.release()
        assert queue.snapshot()["active"] == 0

    asyncio.run(scenario())


def test_disconnected_waiter_releases_transferred_slot() -> None:
    async def scenario() -> None:
        queue = BoundedQaQueue(max_active=1, max_waiting=1)
        held = await queue.acquire()
        disconnected = False

        async def is_disconnected() -> bool:
            return disconnected

        task = asyncio.create_task(
            queue.acquire(is_disconnected=is_disconnected)
        )
        await asyncio.sleep(0)
        disconnected = True
        held.release()
        with pytest.raises(QueueClientDisconnectedError):
            await task
        assert queue.snapshot()["active"] == 0
        assert queue.snapshot()["waiting"] == 0

    asyncio.run(scenario())
