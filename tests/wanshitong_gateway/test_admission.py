"""公共问答运行容量配置及有界FIFO准入合同。"""

from __future__ import annotations

import asyncio

import pytest

from wanshitong_gateway.admission import (
    BoundedQaQueue,
    QueueClientDisconnectedError,
    QueueFullError,
    QueueWaitTimeoutError,
)


def test_four_active_eight_waiting_fifo_and_thirteenth_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WXD_QA_MAX_ACTIVE", raising=False)

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


def test_unset_capacity_preserves_four_active_and_waiting_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WXD_QA_MAX_ACTIVE", raising=False)
    queue = BoundedQaQueue()
    assert queue.max_active == 4
    assert queue.max_waiting == 8
    assert queue.wait_timeout_seconds == 300


def test_explicit_capacity_overrides_even_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WXD_QA_MAX_ACTIVE", "invalid")
    assert BoundedQaQueue(max_active=2).max_active == 2


@pytest.mark.parametrize("value", ["", "0", "-1", "four", "4.5", "true"])
def test_invalid_environment_capacity_is_rejected(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WXD_QA_MAX_ACTIVE", value)
    with pytest.raises(ValueError):
        BoundedQaQueue()


def test_configured_eight_active_with_eight_waiting_runs_fifo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WXD_QA_MAX_ACTIVE", "8")

    async def scenario() -> None:
        queue = BoundedQaQueue()
        release = [asyncio.Event() for _ in range(16)]
        ninth_entered = asyncio.Event()
        entered: list[int] = []

        async def worker(index: int) -> None:
            slot = await queue.acquire()
            try:
                entered.append(index)
                if index == 8:
                    ninth_entered.set()
                await release[index].wait()
            finally:
                slot.release()

        tasks = [asyncio.create_task(worker(index)) for index in range(16)]
        try:
            await asyncio.sleep(0)
            assert queue.snapshot() == {
                "active": 8,
                "waiting": 8,
                "max_active": 8,
                "max_waiting": 8,
            }
            assert entered == list(range(8))
            with pytest.raises(QueueFullError):
                await queue.acquire()
            release[0].set()
            await asyncio.wait_for(ninth_entered.wait(), timeout=1)
            assert entered == list(range(9))
            assert queue.snapshot()["active"] == 8
            assert queue.snapshot()["waiting"] == 7
        finally:
            for signal in release:
                signal.set()
            await asyncio.gather(*tasks)
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
