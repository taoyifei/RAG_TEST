"""以 SQLite 队列为事实源的有限并发 P09 Worker。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from threading import Event, RLock, Thread

_LOGGER = logging.getLogger(__name__)


class DurableJobRunner:
    """调度持久 Job，不把进程内 Future 当作恢复事实源。"""

    def __init__(
        self,
        run_job: Callable[[str], None],
        pending_jobs: Callable[[], Sequence[str]],
        *,
        max_workers: int = 1,
    ) -> None:
        """保存持久队列回调并建立有界线程池。

        Args:
            run_job: 领取并执行一个持久 Job 的回调。
            pending_jobs: 启动恢复时读取 queued Job 的回调。
            max_workers: 单进程最大并发数。

        Returns:
            无返回值。

        """
        if max_workers <= 0:
            raise ValueError("Job Worker 并发数必须为正数。")
        self._run_job = run_job
        self._pending_jobs = pending_jobs
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="rag-p09-job",
        )
        self._futures: dict[str, Future[None]] = {}
        self._lock = RLock()
        self._closed = False
        self._requested: set[str] = set()
        self._stop_polling = Event()
        self._poller: Thread | None = None

    def recover(self) -> None:
        """重新调度数据库中的 interrupted/queued Job。

        Args:
            无参数；读取持久队列。

        Returns:
            无返回值。

        """
        with self._lock:
            if self._closed:
                return
            self._start_polling()
            self._requested.update(self._pending_jobs())
        self._schedule_pending()

    def submit(self, job_id: str) -> None:
        """幂等调度一个已持久化 Job。

        Args:
            job_id: 目标 Job ID。

        Returns:
            无返回值。

        Raises:
            RuntimeError: Runner 已关闭。

        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Job Runner 已关闭。")
            self._requested.add(job_id)
            self._start_polling()
            current = self._futures.get(job_id)
            if current is not None and not current.done():
                return
            future = self._executor.submit(self._run_job, job_id)
            self._futures[job_id] = future
            future.add_done_callback(partial(self._discard, job_id))

    def close(self) -> None:
        """停止接收任务并等待已提交 Job 到达持久终态。

        Args:
            无参数；关闭当前 Runner。

        Returns:
            无返回值。

        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_polling.set()
        if self._poller is not None:
            self._poller.join()
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _discard(self, job_id: str, future: Future[None]) -> None:
        error = future.exception()
        if error is not None:
            _LOGGER.error(
                "持久 Job 执行器收到未处理异常：%s",
                job_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        with self._lock:
            if self._futures.get(job_id) is future:
                self._futures.pop(job_id, None)

    def _start_polling(self) -> None:
        """跨进程占用释放后继续领取，关闭启动恢复时只调度显式提交项。"""
        if self._poller is None:
            self._poller = Thread(
                target=self._poll, name="rag-p09-queue", daemon=True
            )
            self._poller.start()

    def _poll(self) -> None:
        while not self._stop_polling.wait(0.25):
            try:
                self._schedule_pending()
            except Exception as error:  # 后续轮询可以恢复短暂 SQLite 不可用。
                _LOGGER.error("持久作业调度暂不可用：%s", type(error).__name__)

    def _schedule_pending(self) -> None:
        for job_id in self._pending_jobs():
            with self._lock:
                if self._closed:
                    return
                if job_id in self._requested:
                    self.submit(job_id)


__all__ = ["DurableJobRunner"]
