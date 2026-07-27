"""把管理 API 任务串行执行为可恢复的 Qdrant 索引发布。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from qdrant_client import QdrantClient

from rag_app.contracts import (
    IndexManifest,
    PipelineSpec,
    SourceRecord,
)
from rag_app.index.build import discover_docx_sources
from rag_app.index.planner import plan_incremental_sync
from rag_app.index.publisher import FullIndexPublisher
from rag_app.index.qdrant import QdrantIndex
from rag_app.index.worker import SyncChunkBuilder, SyncWorker
from rag_app.manifest import ManifestRepository
from rag_app.state import Job, JobKind, JobState, StateStore
from rag_app.state.models import ActiveSource
from rag_app.state.plans import SyncPlanStore

__all__ = [
    "IndexJobRunner",
    "JobRunResult",
    "JobRunnerConfig",
    "JobRunnerServices",
]


@dataclass(frozen=True, slots=True)
class JobRunnerConfig:
    """单索引 worker 的本地路径和租约配置。"""

    alias_name: str
    input_root: Path
    index_state_dir: Path
    collection_prefix: str = "rag-docx"
    lease_seconds: int = 300

    def __post_init__(self) -> None:
        """拒绝空名称和无效租约。"""
        if not self.alias_name or not self.collection_prefix:
            raise ValueError("alias 与 collection 前缀不能为空。")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正数。")


@dataclass(frozen=True, slots=True)
class JobRunnerServices:
    """任务执行所需的持久服务。"""

    control: StateStore
    manifests: ManifestRepository
    qdrant: QdrantClient
    pipeline: PipelineSpec
    build_chunks_factory: Callable[[StateStore], SyncChunkBuilder]


@dataclass(frozen=True, slots=True)
class JobRunResult:
    """一个控制任务的非敏感执行摘要。"""

    job_id: str
    collection_name: str
    source_count: int
    state: JobState
    error_code: str | None


class IndexJobRunner:
    """一次只领取一个控制任务，并在物理索引状态库中重入。"""

    def __init__(
        self,
        *,
        config: JobRunnerConfig,
        services: JobRunnerServices,
    ) -> None:
        """保存 worker 配置与服务。

        Args:
            config: 输入、状态目录、alias 与租约。
            services: 控制库、manifest、Qdrant、pipeline 和构建函数。

        """
        self._config = config
        self._services = services
        self._fingerprint = services.pipeline.fingerprint()

    def run_next(self, *, worker_id: str) -> JobRunResult | None:
        """领取并完成一个全量或增量任务。

        Args:
            worker_id: 单 worker 的稳定身份。

        Returns:
            成功摘要；没有待领取任务时返回 None。

        """
        job = self._services.control.claim_next_job(
            worker_id=worker_id,
            now=_utc_now(),
            lease_seconds=self._config.lease_seconds,
        )
        if job is None:
            return None
        if job.pipeline_fingerprint != self._fingerprint:
            self._finish_failed(job, worker_id, "PIPELINE_INCOMPATIBLE")
            return JobRunResult(
                job.job_id,
                "",
                0,
                JobState.FAILED,
                "PIPELINE_INCOMPATIBLE",
            )
        try:
            result = self._run_claimed(job)
        except Exception as error:
            error_code = f"INDEX_{type(error).__name__.upper()}"
            self._finish_failed(
                job,
                worker_id,
                error_code,
            )
            return JobRunResult(
                job.job_id,
                "",
                0,
                JobState.FAILED,
                error_code,
            )
        self._services.control.finish_job(
            job_id=job.job_id,
            worker_id=worker_id,
            error_code=None,
        )
        return result

    def _run_claimed(self, job: Job) -> JobRunResult:
        if job.kind == JobKind.FULL:
            collection_name = self._full_collection_name(job)
            previous_sources: tuple[ActiveSource, ...] | None = ()
        else:
            active = self._services.manifests.get_active()
            if active is None:
                raise LookupError("增量任务要求已有活动 manifest。")
            collection_name = active.manifest.collection_name
            previous_sources = None

        state = self._collection_state(collection_name)
        plans = SyncPlanStore(state.path)
        plans.initialize()
        index = QdrantIndex(
            self._services.qdrant,
            collection_name=collection_name,
            dense_dimension=self._services.pipeline.embedding_dimension,
            pipeline_fingerprint=self._fingerprint,
        )
        index.create_collection()
        local_job = state.create_job(
            idempotency_key=f"control:{job.job_id}",
            kind=job.kind,
            pipeline_fingerprint=self._fingerprint,
        )
        if not plans.has_plan(local_job.job_id):
            active_sources = (
                previous_sources
                if previous_sources is not None
                else state.list_active_sources()
            )
            plans.save(
                local_job.job_id,
                plan_incremental_sync(
                    discover_docx_sources(self._config.input_root),
                    active_sources,
                ),
            )
        if local_job.state in {JobState.PENDING, JobState.RUNNING}:
            SyncWorker(state, plans, index).run_next(
                worker_id=f"inner:{job.job_id}",
                lease_seconds=self._config.lease_seconds,
                build_chunks=self._services.build_chunks_factory(state),
            )
        completed = state.get_job(local_job.job_id)
        if completed.state != JobState.SUCCEEDED:
            raise RuntimeError("物理索引同步任务未成功。")

        manifest = self._manifest(job, collection_name, state)
        if job.kind == JobKind.FULL:
            FullIndexPublisher(
                self._services.manifests,
                index,
                alias_name=self._config.alias_name,
            ).publish(manifest)
        else:
            current = self._services.manifests.get_active()
            if current is None or current.manifest != manifest:
                snapshot = index.create_snapshot()
                if snapshot.checksum is None:
                    raise RuntimeError("Qdrant snapshot 缺少 checksum。")
                self._services.manifests.record_active_revision(
                    manifest,
                    snapshot_name=snapshot.name,
                    snapshot_checksum=snapshot.checksum,
                )
        return JobRunResult(
            job_id=job.job_id,
            collection_name=collection_name,
            source_count=len(manifest.sources),
            state=JobState.SUCCEEDED,
            error_code=None,
        )

    def _collection_state(self, collection_name: str) -> StateStore:
        digest = hashlib.sha256(collection_name.encode()).hexdigest()[:24]
        state = StateStore(
            self._config.index_state_dir / f"index-{digest}.sqlite3"
        )
        state.initialize()
        return state

    def _full_collection_name(self, job: Job) -> str:
        fingerprint = self._fingerprint.removeprefix("sha256:")[:12]
        job_suffix = job.job_id.removeprefix("job_")[:12]
        return f"{self._config.collection_prefix}-{fingerprint}-{job_suffix}"

    def _manifest(
        self,
        job: Job,
        collection_name: str,
        state: StateStore,
    ) -> IndexManifest:
        sources = tuple(
            SourceRecord(
                source_id=source.source_id,
                current_path=source.current_path,
                content_sha256=source.content_sha256,
                doc_version=source.doc_version,
            )
            for source in state.list_active_sources()
        )
        return IndexManifest(
            manifest_version="1",
            collection_name=collection_name,
            created_at=job.created_at,
            pipeline=self._services.pipeline,
            pipeline_fingerprint=self._fingerprint,
            sources=sources,
        )

    def _finish_failed(
        self,
        job: Job,
        worker_id: str,
        error_code: str,
    ) -> None:
        self._services.control.finish_job(
            job_id=job.job_id,
            worker_id=worker_id,
            error_code=error_code,
        )


def _utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(UTC)
