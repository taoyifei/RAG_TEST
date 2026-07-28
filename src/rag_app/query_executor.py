"""为同步查询提供进程级固定容量执行器。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

__all__ = [
    "QueryAdmissionError",
    "QueryCapacityError",
    "QueryExecutor",
    "QueryExecutorClosedError",
    "QueryQueueTimeoutError",
]

_MAX_WORKERS = 4
_MAX_QUEUE = 8
_TOTAL_CAPACITY = _MAX_WORKERS + _MAX_QUEUE
_MAX_QUEUE_WAIT_SECONDS = 60.0
_RETRY_AFTER_SECONDS = 5


class QueryAdmissionError(RuntimeError):
    """查询尚未开始前的稳定准入失败。"""


class QueryCapacityError(QueryAdmissionError):
    """活动与排队查询已达到固定总容量。"""


class QueryQueueTimeoutError(QueryAdmissionError):
    """查询在固定排队时限内未获得 worker。"""


class QueryExecutorClosedError(QueryAdmissionError):
    """执行器已经停止接收新查询。"""


@dataclass(slots=True)
class _QueryTask:
    work: Callable[[], None]
    wake: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    state: str = "queued"

    def begin(self) -> bool:
        """把仍在排队的任务原子转换为运行状态。

        Args:
            无参数；更新当前任务状态。

        Returns:
            本次成功取得执行权时返回 `True`。

        """
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "running"
            self.wake.set()
            return True

    def mark_timeout(self) -> bool:
        """把仍在排队的任务原子转换为超时状态。

        Args:
            无参数；更新当前任务状态。

        Returns:
            本次成功取消排队任务时返回 `True`。

        """
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "timed_out"
            self.wake.set()
            return True

    def mark_closed(self) -> None:
        """唤醒因执行器关闭而取消的排队任务。

        Args:
            无参数；更新当前任务状态。

        Returns:
            无返回值。

        """
        with self.lock:
            if self.state != "queued":
                return
            self.state = "closed"
            self.wake.set()

    def current_state(self) -> str:
        """读取线程安全的准入状态。

        Args:
            无参数；读取当前任务状态。

        Returns:
            queued、running、timed_out 或 closed。

        """
        with self.lock:
            return self.state


class QueryExecutor:
    """固定 4 worker、8 排队项和 12 总容量的同步执行器。"""

    def __init__(self, *, queue_wait_seconds: float = 60.0) -> None:
        """创建固定容量线程池。

        Args:
            queue_wait_seconds: 等待 worker 的时限；仅可缩短，不能超过 60 秒。

        Raises:
            ValueError: 排队时限不是 `(0, 60]` 内的有限值。

        """
        if not 0 < queue_wait_seconds <= _MAX_QUEUE_WAIT_SECONDS:
            raise ValueError("查询排队时限必须位于 (0,60] 秒。")
        self._queue_wait_seconds = queue_wait_seconds
        self._pool = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS,
            thread_name_prefix="rag-query-worker",
        )
        self._capacity = threading.BoundedSemaphore(_TOTAL_CAPACITY)
        self._lock = threading.Lock()
        self._closed = False
        self._shutdown_complete = threading.Event()
        self._in_flight = 0

    @property
    def max_workers(self) -> int:
        """返回不可配置的活动 worker 上限。

        Args:
            无参数；读取固定执行器容量。

        Returns:
            固定值 4。

        """
        return _MAX_WORKERS

    @property
    def max_queue(self) -> int:
        """返回不可配置的排队项上限。

        Args:
            无参数；读取固定执行器容量。

        Returns:
            固定值 8。

        """
        return _MAX_QUEUE

    @property
    def retry_after_seconds(self) -> int:
        """返回 HTTP 拒绝响应使用的稳定重试间隔。

        Args:
            无参数；读取固定重试提示。

        Returns:
            固定秒数 5。

        """
        return _RETRY_AFTER_SECONDS

    @property
    def in_flight(self) -> int:
        """返回当前活动与排队查询总数。

        Args:
            无参数；读取进程内非敏感计数。

        Returns:
            `[0,12]` 内的当前查询数。

        """
        with self._lock:
            return self._in_flight

    def submit(self, work: Callable[[], None]) -> None:
        """准入查询并等待固定 worker 真正开始执行。

        Args:
            work: 不含执行器生命周期控制的同步查询函数。

        Returns:
            worker 开始执行后返回，不等待查询完成。

        Raises:
            QueryCapacityError: 12 个容量槽均已占用。
            QueryExecutorClosedError: 执行器已关闭或正在关闭。
            QueryQueueTimeoutError: 排队等待超过配置时限。

        """
        task = _QueryTask(work)
        with self._lock:
            if self._closed:
                raise QueryExecutorClosedError("查询执行器已关闭。")
            if not self._capacity.acquire(blocking=False):
                raise QueryCapacityError("查询容量已满。")
            self._in_flight += 1
            try:
                future = self._pool.submit(self._run, task)
            except RuntimeError as error:
                self._in_flight -= 1
                self._capacity.release()
                raise QueryExecutorClosedError(
                    "查询执行器已关闭。"
                ) from error
        future.add_done_callback(
            lambda completed: self._complete(task, completed)
        )

        if not task.wake.wait(self._queue_wait_seconds):
            if task.mark_timeout():
                future.cancel()
                raise QueryQueueTimeoutError("查询排队等待超时。")
            task.wake.wait()
        state = task.current_state()
        if state == "running":
            return
        if state == "closed":
            raise QueryExecutorClosedError("查询执行器已关闭。")
        if state == "timed_out":
            raise QueryQueueTimeoutError("查询排队等待超时。")
        raise RuntimeError("查询执行器进入未知准入状态。")

    def close(self) -> None:
        """停止准入、取消排队项并等待活动查询结束。

        Args:
            无参数；关闭当前进程级查询执行器。

        Returns:
            无返回值；重复调用安全。

        """
        with self._lock:
            if self._closed:
                shutdown_complete = self._shutdown_complete
                owns_shutdown = False
            else:
                self._closed = True
                shutdown_complete = self._shutdown_complete
                owns_shutdown = True
        if not owns_shutdown:
            shutdown_complete.wait()
            return
        try:
            self._pool.shutdown(wait=True, cancel_futures=True)
        finally:
            shutdown_complete.set()

    @staticmethod
    def _run(task: _QueryTask) -> None:
        if task.begin():
            task.work()

    def _complete(
        self,
        task: _QueryTask,
        future: Future[None],
    ) -> None:
        if future.cancelled():
            task.mark_closed()
        with self._lock:
            self._in_flight -= 1
            self._capacity.release()
