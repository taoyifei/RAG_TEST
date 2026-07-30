"""索引任务的有界后台租约续租。"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal, Protocol

__all__ = ["LeaseHeartbeat", "LeaseLostError"]

_MIN_INTERVAL_SECONDS = 0.1
_MAX_INTERVAL_SECONDS = 30.0
_THREAD_SEQUENCE = itertools.count(1)

LeaseRenewal = Callable[[datetime], object]


class _JobLeaseStore(Protocol):
    def renew_job_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> object:
        """续租一个由当前 worker 持有的任务。

        Args:
            job_id: 运行中任务标识。
            worker_id: 当前租约所有者。
            now: 带时区 UTC 时间。
            lease_seconds: 正数任务租约秒数。

        Returns:
            续租后的任务对象。

        """
        ...


class LeaseLostError(RuntimeError):
    """表示后台续租失败，当前执行者不得继续发布。"""


class LeaseHeartbeat:
    """用非 daemon 线程持续续租，并把失败交还主线程。"""

    def __init__(
        self,
        *,
        renew: LeaseRenewal,
        lease_seconds: int,
    ) -> None:
        """保存续租函数并计算有界 heartbeat 间隔。

        Args:
            renew: 接收带时区 UTC 时间的单次续租函数。
            lease_seconds: 正数任务租约秒数。

        Raises:
            ValueError: 租约不是正整数。

        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正数。")
        self._renew = renew
        self._interval_seconds = min(
            max(lease_seconds / 4, _MIN_INTERVAL_SECONDS),
            _MAX_INTERVAL_SECONDS,
            lease_seconds / 3,
        )
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failure = False
        self._closed = False
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"rag-lease-heartbeat-{next(_THREAD_SEQUENCE)}",
            daemon=False,
        )

    @classmethod
    def for_job(
        cls,
        *,
        store: _JobLeaseStore,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> LeaseHeartbeat:
        """为一个 JobStore 任务创建 heartbeat。

        Args:
            store: 提供严格 owner 校验续租的任务存储。
            job_id: 已领取的运行中任务。
            worker_id: 当前租约所有者。
            lease_seconds: 正数任务租约秒数。

        Returns:
            尚未启动的 heartbeat。

        """

        def renew(now: datetime) -> object:
            """使用固定任务身份执行一次续租。

            Args:
                now: 带时区 UTC 时间。

            Returns:
                存储返回的续租任务对象。

            """
            return store.renew_job_lease(
                job_id=job_id,
                worker_id=worker_id,
                now=now,
                lease_seconds=lease_seconds,
            )

        return cls(renew=renew, lease_seconds=lease_seconds)

    @property
    def interval_seconds(self) -> float:
        """返回实际 heartbeat 间隔秒数。

        Args:
            无参数。

        Returns:
            同时满足租约比例和固定上下界的秒数。

        """
        return self._interval_seconds

    @property
    def thread_is_daemon(self) -> bool:
        """返回后台线程 daemon 标记。

        Args:
            无参数。

        Returns:
            后台线程是否为 daemon。

        """
        return self._thread.daemon

    def __enter__(self) -> LeaseHeartbeat:
        """同步确认首轮续租后启动后台线程。

        Returns:
            当前 heartbeat。

        Raises:
            LeaseLostError: 初次续租失败或 heartbeat 已关闭。

        """
        with self._lock:
            if self._closed or self._started:
                raise LeaseLostError("LEASE_LOST")
            self._started = True
        try:
            self._renew(datetime.now(UTC))
        except Exception:
            self._record_failure()
            self.close()
            raise LeaseLostError("LEASE_LOST") from None
        self._thread.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """停止并 join 线程，不覆盖 context 内已有异常。

        Args:
            exception_type: context 内异常类型。
            exception: context 内异常实例。
            traceback: context 内异常回溯。

        Returns:
            始终返回 False，不抑制异常。

        Raises:
            LeaseLostError: context 正常结束但后台续租已经失败。

        """
        del exception, traceback
        self.close()
        if exception_type is None:
            self.raise_if_failed()
        return False

    def close(self) -> None:
        """幂等停止并 join 后台线程。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            started = self._thread.ident is not None
        self._stop.set()
        if started and self._thread is not threading.current_thread():
            self._thread.join()

    def raise_if_failed(self) -> None:
        """在主线程中以稳定错误暴露续租失败。

        Args:
            无参数。

        Returns:
            无返回值。

        Raises:
            LeaseLostError: 初始或后台续租失败。

        """
        with self._lock:
            failed = self._failure
        if failed:
            raise LeaseLostError("LEASE_LOST")

    def _run(self) -> None:
        deadline = time.monotonic() + self._interval_seconds
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if self._stop.wait(remaining):
                return
            try:
                self._renew(datetime.now(UTC))
            except Exception:
                self._record_failure()
                self._stop.set()
                return
            deadline += self._interval_seconds
            now = time.monotonic()
            if deadline <= now:
                deadline = now + self._interval_seconds

    def _record_failure(self) -> None:
        with self._lock:
            self._failure = True
