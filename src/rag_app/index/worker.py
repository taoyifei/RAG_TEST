"""单索引 worker 的可恢复增量执行循环。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rag_app.index.coordinator import IndexCoordinator
from rag_app.index.planner import SyncAction, SyncActionKind
from rag_app.index.qdrant import IndexedChunk, QdrantIndex
from rag_app.state import Job, SourceVersion, StateStore
from rag_app.state.lease import LeaseHeartbeat, LeaseLostError
from rag_app.state.plans import SyncItemState, SyncPlanStore

__all__ = ["SyncChunkBuilder", "SyncWorker", "WorkerResult"]

SyncChunkBuilder = Callable[
    [str, SourceVersion],
    Sequence[IndexedChunk],
]


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """一个 job 的单 worker 执行结果。"""

    job_id: str
    succeeded_items: int
    failed_items: int


@dataclass(frozen=True, slots=True)
class _WorkerLease:
    heartbeat: LeaseHeartbeat
    outer_guard: Callable[[], None]

    def guard(self) -> None:
        """依次检查 control 与 local 租约。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        self.outer_guard()
        self.heartbeat.raise_if_failed()

    def close(self) -> None:
        """停止 local heartbeat 后执行最后一次双层检查。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        self.heartbeat.close()
        self.guard()


class SyncWorker:
    """顺序执行 SQLite 中已冻结的同步计划。"""

    def __init__(
        self,
        state: StateStore,
        plans: SyncPlanStore,
        index: QdrantIndex,
    ) -> None:
        """保存状态、计划和索引依赖。

        Args:
            state: 任务与来源版本状态库。
            plans: 持久同步任务项。
            index: 当前 pipeline 的物理 collection。

        """
        self._state = state
        self._plans = plans
        self._index = index

    def run_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        build_chunks: SyncChunkBuilder,
        max_item_attempts: int = 3,
        lease_guard: Callable[[], None] | None = None,
    ) -> WorkerResult | None:
        """领取并运行一个任务，失败项不阻塞后续项。

        Args:
            worker_id: 单 worker 身份。
            lease_seconds: 任务租约秒数。
            build_chunks: 解析、切块及 dense/sparse 编码函数。
            max_item_attempts: 单项最大尝试次数。
            lease_guard: control job 的外层租约检查。

        Returns:
            执行汇总；没有可领取任务时返回 None。

        """
        job = self._state.claim_next_job(
            worker_id=worker_id,
            now=datetime.now(UTC),
            lease_seconds=lease_seconds,
        )
        if job is None:
            return None
        heartbeat = LeaseHeartbeat.for_job(
            store=self._state,
            job_id=job.job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        lease = _WorkerLease(
            heartbeat=heartbeat,
            outer_guard=lease_guard or _noop,
        )
        with heartbeat:
            if job.pipeline_fingerprint != self._index.pipeline_fingerprint:
                self._finish_owned(
                    job,
                    worker_id=worker_id,
                    error_code="PIPELINE_INCOMPATIBLE",
                    lease=lease,
                )
                return WorkerResult(
                    job_id=job.job_id,
                    succeeded_items=0,
                    failed_items=0,
                )
            return self._run_claimed(
                job,
                worker_id=worker_id,
                build_chunks=build_chunks,
                max_item_attempts=max_item_attempts,
                lease=lease,
            )

    def _run_claimed(
        self,
        job: Job,
        *,
        worker_id: str,
        build_chunks: SyncChunkBuilder,
        max_item_attempts: int,
        lease: _WorkerLease,
    ) -> WorkerResult:
        """执行已领取任务的剩余计划项并结束本地任务。

        单项业务异常会转为重试或最终失败，不阻塞后续计划项；租约丢失则
        立即终止，避免继续写入 Qdrant 或 SQLite。

        Args:
            job: 已由当前 worker 领取的本地同步任务。
            worker_id: 当前本地租约所有者。
            build_chunks: 将来源版本构建为索引分块的函数。
            max_item_attempts: 单个计划项允许的最大尝试次数。
            lease: control 与 local 两层租约检查器。

        Returns:
            成功和失败计划项数量的任务摘要。

        Raises:
            LeaseLostError: 任一执行边界检测到租约丢失。

        """
        coordinator = IndexCoordinator(
            self._state,
            self._index,
            lease_guard=lease.guard,
        )
        while True:
            lease.guard()
            item = self._plans.claim_next(job.job_id)
            if item is None:
                break
            lease.guard()
            try:
                self._execute(
                    item.action,
                    job,
                    build_chunks,
                    coordinator=coordinator,
                    lease_guard=lease.guard,
                )
            except LeaseLostError:
                raise
            except Exception as error:
                self._plans.retry_or_fail(
                    item.item_id,
                    error_code=type(error).__name__,
                    max_attempts=max_item_attempts,
                )
            else:
                self._plans.succeed(item.item_id)
            lease.guard()

        failed = self._plans.has_failures(job.job_id)
        self._finish_owned(
            job,
            worker_id=worker_id,
            error_code="SYNC_ITEM_FAILED" if failed else None,
            lease=lease,
        )
        items = self._plans.list_items(job.job_id)
        return WorkerResult(
            job_id=job.job_id,
            succeeded_items=sum(
                item.state == SyncItemState.SUCCEEDED for item in items
            ),
            failed_items=sum(
                item.state == SyncItemState.FAILED for item in items
            ),
        )

    def _execute(
        self,
        action: SyncAction,
        job: Job,
        build_chunks: SyncChunkBuilder,
        *,
        coordinator: IndexCoordinator,
        lease_guard: Callable[[], None],
    ) -> None:
        """按冻结动作类型执行一次来源同步。

        Args:
            action: 已持久化的同步动作。
            job: 动作所属的本地同步任务。
            build_chunks: ADD 或 UPDATE 使用的分块构建函数。
            coordinator: 协调 SQLite 与 Qdrant 状态迁移的执行器。
            lease_guard: rename 两端写入边界的租约检查函数。

        Returns:
            无返回值。

        Raises:
            ValueError: 动作缺少必要身份字段或类型未知。

        """
        if action.kind in (SyncActionKind.ADD, SyncActionKind.UPDATE):
            if action.source_path is None:
                raise ValueError("add/update 动作缺少 source_path。")
            source_path = action.source_path
            coordinator.index_source(
                job_id=job.job_id,
                source_path=source_path,
                content_sha256=action.content_sha256,
                source_id_hint=action.source_id_hint,
                build_chunks=lambda version: build_chunks(
                    source_path,
                    version,
                ),
            )
            return
        if action.kind == SyncActionKind.RENAME:
            self._rename(action, lease_guard=lease_guard)
            return
        if action.kind == SyncActionKind.DELETE:
            if action.source_id is None:
                raise ValueError("delete 动作缺少 source_id。")
            coordinator.delete_source(action.source_id)
            return
        if action.kind != SyncActionKind.UNCHANGED:
            raise ValueError("未知同步动作。")

    def _rename(
        self,
        action: SyncAction,
        *,
        lease_guard: Callable[[], None],
    ) -> None:
        """在租约保护下同步修改 Qdrant 与 SQLite 的来源路径。

        Args:
            action: 包含原来源身份和新路径的 rename 动作。
            lease_guard: 两端持久化写入前后的租约检查函数。

        Returns:
            无返回值。

        Raises:
            LeaseLostError: rename 边界检测到租约丢失。
            ValueError: 动作缺少来源身份或新路径。
            RuntimeError: SQLite 解析出的来源身份与计划不一致。

        """
        if action.source_id is None or action.source_path is None:
            raise ValueError("rename 动作缺少来源身份或新路径。")
        lease_guard()
        self._index.rename_source(action.source_id, action.source_path)
        lease_guard()
        renamed_source_id = self._state.apply_rename_if_unique(
            new_path=action.source_path,
            content_sha256=action.content_sha256,
        )
        if renamed_source_id != action.source_id:
            raise RuntimeError("rename 计划与当前活动来源不一致。")

    def _finish_owned(
        self,
        job: Job,
        *,
        worker_id: str,
        error_code: str | None,
        lease: _WorkerLease,
    ) -> None:
        """停止 heartbeat，并仅由仍持有租约的 worker 结束任务。

        Args:
            job: 待结束的本地同步任务。
            worker_id: 预期租约所有者。
            error_code: 最终失败码；成功时为 None。
            lease: 待关闭并做最后检查的双层租约。

        Returns:
            无返回值。

        Raises:
            LeaseLostError: heartbeat 已失败或终态写入时所有权已变化。

        """
        lease.close()
        try:
            self._state.finish_job(
                job_id=job.job_id,
                worker_id=worker_id,
                error_code=error_code,
            )
        except (LookupError, sqlite3.Error):
            raise LeaseLostError("LEASE_LOST") from None


def _noop() -> None:
    return
