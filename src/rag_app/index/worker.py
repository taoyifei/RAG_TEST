"""单索引 worker 的可恢复增量执行循环。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rag_app.index.coordinator import IndexCoordinator
from rag_app.index.planner import SyncAction, SyncActionKind
from rag_app.index.qdrant import IndexedChunk, QdrantIndex
from rag_app.state import Job, SourceVersion, StateStore
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
        self._coordinator = IndexCoordinator(state, index)

    def run_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        build_chunks: SyncChunkBuilder,
        max_item_attempts: int = 3,
    ) -> WorkerResult | None:
        """领取并运行一个任务，失败项不阻塞后续项。

        Args:
            worker_id: 单 worker 身份。
            lease_seconds: 任务租约秒数。
            build_chunks: 解析、切块及 dense/sparse 编码函数。
            max_item_attempts: 单项最大尝试次数。

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
        if job.pipeline_fingerprint != self._index.pipeline_fingerprint:
            self._state.finish_job(
                job_id=job.job_id,
                worker_id=worker_id,
                error_code="PIPELINE_INCOMPATIBLE",
            )
            return WorkerResult(
                job_id=job.job_id,
                succeeded_items=0,
                failed_items=0,
            )
        return self._run_claimed(
            job,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            build_chunks=build_chunks,
            max_item_attempts=max_item_attempts,
        )

    def _run_claimed(
        self,
        job: Job,
        *,
        worker_id: str,
        lease_seconds: int,
        build_chunks: SyncChunkBuilder,
        max_item_attempts: int,
    ) -> WorkerResult:
        while True:
            self._state.renew_job_lease(
                job_id=job.job_id,
                worker_id=worker_id,
                now=datetime.now(UTC),
                lease_seconds=lease_seconds,
            )
            item = self._plans.claim_next(job.job_id)
            if item is None:
                break
            try:
                self._execute(item.action, job, build_chunks)
            except Exception as error:
                self._plans.retry_or_fail(
                    item.item_id,
                    error_code=type(error).__name__,
                    max_attempts=max_item_attempts,
                )
            else:
                self._plans.succeed(item.item_id)

        failed = self._plans.has_failures(job.job_id)
        self._state.finish_job(
            job_id=job.job_id,
            worker_id=worker_id,
            error_code="SYNC_ITEM_FAILED" if failed else None,
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
    ) -> None:
        if action.kind in (SyncActionKind.ADD, SyncActionKind.UPDATE):
            if action.source_path is None:
                raise ValueError("add/update 动作缺少 source_path。")
            source_path = action.source_path
            self._coordinator.index_source(
                job_id=job.job_id,
                source_path=source_path,
                content_sha256=action.content_sha256,
                build_chunks=lambda version: build_chunks(
                    source_path,
                    version,
                ),
            )
            return
        if action.kind == SyncActionKind.RENAME:
            self._rename(action)
            return
        if action.kind == SyncActionKind.DELETE:
            if action.source_id is None:
                raise ValueError("delete 动作缺少 source_id。")
            self._coordinator.delete_source(action.source_id)
            return
        if action.kind != SyncActionKind.UNCHANGED:
            raise ValueError("未知同步动作。")

    def _rename(self, action: SyncAction) -> None:
        if action.source_id is None or action.source_path is None:
            raise ValueError("rename 动作缺少来源身份或新路径。")
        self._index.rename_source(action.source_id, action.source_path)
        renamed_source_id = self._state.apply_rename_if_unique(
            new_path=action.source_path,
            content_sha256=action.content_sha256,
        )
        if renamed_source_id != action.source_id:
            raise RuntimeError("rename 计划与当前活动来源不一致。")
