"""以 SQLite 队列为事实源的有限并发 P09 Worker。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from threading import RLock


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

    def recover(self) -> None:
        """重新调度数据库中的 interrupted/queued Job。

        Args:
            无参数；读取持久队列。

        Returns:
            无返回值。

        """
        for job_id in self._pending_jobs():
            self.submit(job_id)

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
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _discard(self, job_id: str, future: Future[None]) -> None:
        del future
        with self._lock:
            self._futures.pop(job_id, None)


__all__ = ["DurableJobRunner"]
