"""把管理 API 任务串行执行为可恢复的 Qdrant 索引发布。"""

from __future__ import annotations

import hashlib
import sqlite3
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
from rag_app.index.planner import plan_full_rebuild, plan_incremental_sync
from rag_app.index.publisher import FullIndexPublisher
from rag_app.index.qdrant import QdrantIndex
from rag_app.index.worker import SyncChunkBuilder, SyncWorker
from rag_app.manifest import ManifestRepository, ManifestState, StoredManifest
from rag_app.state import Job, JobKind, JobState, StateStore
from rag_app.state.lease import LeaseHeartbeat, LeaseLostError
from rag_app.state.models import ActiveSource, CollectionStateIdentity
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


@dataclass(frozen=True, slots=True)
class _FrozenBase:
    stored: StoredManifest
    sources: tuple[ActiveSource, ...]
    active_count: int | None


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
        heartbeat = LeaseHeartbeat.for_job(
            store=self._services.control,
            job_id=job.job_id,
            worker_id=worker_id,
            lease_seconds=self._config.lease_seconds,
        )
        try:
            with heartbeat:
                return self._run_with_control_lease(
                    job,
                    worker_id=worker_id,
                    heartbeat=heartbeat,
                )
        except LeaseLostError:
            self._mark_lease_lost_if_owned(job, worker_id)
            return JobRunResult(
                job_id=job.job_id,
                collection_name=self._collection_name(job),
                source_count=0,
                state=JobState.FAILED,
                error_code="LEASE_LOST",
            )

    def _run_with_control_lease(
        self,
        job: Job,
        *,
        worker_id: str,
        heartbeat: LeaseHeartbeat,
    ) -> JobRunResult:
        """在控制租约保护下执行任务并持久化终态。

        pipeline 不兼容和普通执行异常会转换为失败摘要。写入任务终态前会
        停止 heartbeat 并再次确认租约，避免失去所有权后覆盖其他 worker。

        Args:
            job: 已由当前 worker 领取的控制任务。
            worker_id: 当前租约所有者。
            heartbeat: 该控制任务的续租器。

        Returns:
            成功或失败的任务执行摘要。

        Raises:
            LeaseLostError: 执行期间失去任务租约。

        """
        guard = heartbeat.raise_if_failed
        if job.pipeline_fingerprint != self._fingerprint:
            guard()
            heartbeat.close()
            guard()
            self._finish_owned(
                job,
                worker_id,
                error_code="PIPELINE_INCOMPATIBLE",
            )
            return JobRunResult(
                job.job_id,
                "",
                0,
                JobState.FAILED,
                "PIPELINE_INCOMPATIBLE",
            )
        try:
            result = self._run_claimed(job, lease_guard=guard)
        except LeaseLostError:
            raise
        except Exception as error:
            guard()
            error_code = f"INDEX_{type(error).__name__.upper()}"
            heartbeat.close()
            guard()
            self._finish_owned(
                job,
                worker_id,
                error_code=error_code,
            )
            return JobRunResult(
                job.job_id,
                "",
                0,
                JobState.FAILED,
                error_code,
            )
        guard()
        heartbeat.close()
        guard()
        self._finish_owned(job, worker_id, error_code=None)
        return result

    def _run_claimed(
        self,
        job: Job,
        *,
        lease_guard: Callable[[], None],
    ) -> JobRunResult:
        """重入或执行已领取任务，直至目标 collection 发布完成。

        此过程可能创建或克隆物理 collection、更新独立状态库、执行同步计划，
        并在最终一致性检查后切换 alias 与活动 manifest。

        Args:
            job: 已领取且 pipeline 兼容的控制任务。
            lease_guard: 各持久化边界前后的租约检查函数。

        Returns:
            已发布目标 collection 的成功摘要。

        Raises:
            LeaseLostError: 任一持久化边界检测到租约丢失。
            LookupError: 增量任务没有可冻结的活动基线。
            ValueError: 活动基线、target 身份或索引契约不一致。
            RuntimeError: 物理同步或发布后的三方一致性检查失败。

        """
        collection_name = self._collection_name(job)
        index = QdrantIndex(
            self._services.qdrant,
            collection_name=collection_name,
            dense_dimension=self._services.pipeline.embedding_dimension,
            pipeline_fingerprint=self._fingerprint,
        )
        lease_guard()
        published = self._published_result(job, index)
        lease_guard()
        if published is not None:
            return published
        base = self._freeze_base(job, collection_name, index)
        lease_guard()
        if job.kind == JobKind.FULL:
            state = self._prepare_full_target(
                job,
                index,
                base,
                lease_guard=lease_guard,
            )
            previous_sources = () if base is None else base.sources
        else:
            if base is None:
                raise LookupError("增量任务要求已有活动 manifest。")
            state = self._prepare_incremental_target(
                job,
                index,
                base,
                lease_guard=lease_guard,
            )
            previous_sources = None
        plans = SyncPlanStore(state.path)
        plans.initialize()
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
            discovered = discover_docx_sources(self._config.input_root)
            plan = (
                plan_full_rebuild(discovered, active_sources)
                if job.kind == JobKind.FULL
                else plan_incremental_sync(discovered, active_sources)
            )
            plans.save(local_job.job_id, plan)
        if local_job.state in {JobState.PENDING, JobState.RUNNING}:
            SyncWorker(state, plans, index).run_next(
                worker_id=f"inner:{job.job_id}",
                lease_seconds=self._config.lease_seconds,
                build_chunks=self._services.build_chunks_factory(state),
                lease_guard=lease_guard,
            )
        completed = state.get_job(local_job.job_id)
        if completed.state != JobState.SUCCEEDED:
            raise RuntimeError("物理索引同步任务未成功。")

        lease_guard()
        manifest = self._manifest(job, collection_name, state)
        self._require_base_unchanged(base, collection_name, index)
        lease_guard()
        FullIndexPublisher(
            self._services.manifests,
            index,
            alias_name=self._config.alias_name,
        ).publish(manifest, lease_guard=lease_guard)
        lease_guard()
        active = self._services.manifests.get_active()
        if active is None or active.manifest != manifest:
            raise RuntimeError("发布后 active manifest 与 target 不一致。")
        return JobRunResult(
            job_id=job.job_id,
            collection_name=collection_name,
            source_count=len(manifest.sources),
            state=JobState.SUCCEEDED,
            error_code=None,
        )

    def _prepare_full_target(
        self,
        job: Job,
        index: QdrantIndex,
        base: _FrozenBase | None,
        *,
        lease_guard: Callable[[], None],
    ) -> StateStore:
        """为全量任务准备绑定当前控制任务的空 target。

        Args:
            job: 当前控制任务。
            index: 任务对应的 target collection。
            base: 发布前冻结的活动基线；首发时为 None。
            lease_guard: Qdrant 与 SQLite 写入边界的租约检查函数。

        Returns:
            已初始化并绑定 staging 身份的 target 状态库。

        Raises:
            LeaseLostError: 准备期间失去控制任务租约。
            ValueError: 已存在 target 的 staging 身份不一致。

        """
        base_digest = None if base is None else base.stored.manifest_sha256
        lease_guard()
        index.prepare_staging_collection(
            control_job_id=job.job_id,
            base_manifest_sha256=base_digest,
        )
        lease_guard()
        state = self._collection_state(index.collection_name)
        state.bind_collection_identity(
            control_job_id=job.job_id,
            pipeline_fingerprint=self._fingerprint,
            base_manifest_sha256=base_digest,
        )
        return state

    def _prepare_incremental_target(
        self,
        job: Job,
        index: QdrantIndex,
        base: _FrozenBase,
        *,
        lease_guard: Callable[[], None],
    ) -> StateStore:
        """从冻结基线克隆增量任务的 Qdrant 与 SQLite target。

        Args:
            job: 当前增量控制任务。
            index: 任务对应的 target collection。
            base: 已验证 snapshot、来源和活动点数的冻结基线。
            lease_guard: 两类存储克隆边界的租约检查函数。

        Returns:
            已初始化并绑定 staging 身份的 target 状态库。

        Raises:
            LeaseLostError: 克隆期间失去控制任务租约。
            FileNotFoundError: 活动 collection 状态库不存在或不安全。
            ValueError: snapshot、来源列表或 staging 身份不一致。
            RuntimeError: Qdrant 未确认恢复或活动点数发生变化。

        """
        lease_guard()
        index.clone_registered_snapshot(
            source_collection_name=base.stored.manifest.collection_name,
            snapshot_name=base.stored.snapshot_name,
            checksum=base.stored.snapshot_checksum,
            control_job_id=job.job_id,
            base_manifest_sha256=base.stored.manifest_sha256,
        )
        lease_guard()
        source_path = self._collection_state_path(
            base.stored.manifest.collection_name
        )
        target_path = self._collection_state_path(index.collection_name)
        lease_guard()
        state = StateStore.clone_collection_state(
            source_path=source_path,
            target_path=target_path,
            identity=CollectionStateIdentity(
                control_job_id=job.job_id,
                pipeline_fingerprint=self._fingerprint,
                base_manifest_sha256=base.stored.manifest_sha256,
            ),
            expected_sources=base.sources,
        )
        lease_guard()
        state.initialize()
        return state

    def _freeze_base(
        self,
        job: Job,
        collection_name: str,
        target_index: QdrantIndex,
    ) -> _FrozenBase | None:
        """冻结发布前活动索引的身份、来源和精确点数。

        全量首发可以没有活动基线。增量任务还会验证 pipeline、已登记
        snapshot 及独立状态库，并保存活动点数供发布前再次比较。

        Args:
            job: 当前控制任务。
            collection_name: 当前任务的 target collection 名称。
            target_index: 用于读取 alias 和活动 collection 的索引客户端。

        Returns:
            冻结的活动基线；全量首发时返回 None。

        Raises:
            ValueError: alias、pipeline、collection 或 snapshot 身份不一致。
            RuntimeError: 活动状态库的来源列表与 manifest 不一致。

        """
        active = self._services.manifests.get_active()
        alias_target = target_index.alias_target(self._config.alias_name)
        if active is None:
            if not self._alias_is_recoverable(
                alias_target,
                collection_name,
                base_collection=None,
            ):
                raise ValueError("alias 与空活动 manifest 不一致。")
            return None
        if not self._alias_is_recoverable(
            alias_target,
            collection_name,
            base_collection=active.manifest.collection_name,
        ):
            raise ValueError("活动 alias 与 manifest collection 不一致。")
        sources = tuple(
            ActiveSource(
                source_id=source.source_id,
                current_path=source.current_path,
                content_sha256=source.content_sha256,
                doc_version=source.doc_version,
            )
            for source in active.manifest.sources
        )
        active_count = None
        if job.kind == JobKind.INCREMENTAL:
            if active.manifest.pipeline_fingerprint != self._fingerprint:
                raise ValueError("增量任务 pipeline 与活动 manifest 不一致。")
            source_index = QdrantIndex(
                self._services.qdrant,
                collection_name=active.manifest.collection_name,
                dense_dimension=self._services.pipeline.embedding_dimension,
                pipeline_fingerprint=self._fingerprint,
            )
            source_index.require_compatible_collection()
            source_index.require_registered_snapshot(
                collection_name=active.manifest.collection_name,
                snapshot_name=active.snapshot_name,
                checksum=active.snapshot_checksum,
            )
            active_count = source_index.count_active_exact()
            self._require_base_state_sources(active, sources)
        return _FrozenBase(
            stored=active,
            sources=sources,
            active_count=active_count,
        )

    def _alias_is_recoverable(
        self,
        alias_target: str | None,
        collection_name: str,
        *,
        base_collection: str | None,
    ) -> bool:
        """判断 alias 是否仍对应基线或可恢复的 staging target。

        Args:
            alias_target: 当前 alias 指向的 collection；不存在时为 None。
            collection_name: 当前控制任务的 target collection。
            base_collection: 冻结基线 collection；首发时为 None。

        Returns:
            alias 指向基线，或指向同一任务的 staging target 时返回 True。

        """
        if alias_target == base_collection:
            return True
        if alias_target != collection_name:
            return False
        target = self._services.manifests.get(collection_name)
        return target is not None and target.state == ManifestState.STAGING

    def _require_base_unchanged(
        self,
        base: _FrozenBase | None,
        collection_name: str,
        target_index: QdrantIndex,
    ) -> None:
        """在发布前确认活动基线未被其他任务替换或改写。

        Args:
            base: 构建开始时冻结的活动基线；首发时为 None。
            collection_name: 当前任务的 target collection。
            target_index: 用于复核 alias、snapshot 和活动点数的索引客户端。

        Returns:
            无返回值。

        Raises:
            RuntimeError: manifest、alias、活动点数或来源列表发生变化。
            ValueError: 已登记 snapshot 不再存在或与冻结身份不一致。

        """
        active = self._services.manifests.get_active()
        if base is None:
            if active is not None:
                raise RuntimeError("构建期间 active manifest 已改变。")
        elif active != base.stored:
            raise RuntimeError("构建期间 active manifest 或 snapshot 已改变。")
        alias_target = target_index.alias_target(self._config.alias_name)
        base_collection = (
            None if base is None else base.stored.manifest.collection_name
        )
        if not self._alias_is_recoverable(
            alias_target,
            collection_name,
            base_collection=base_collection,
        ):
            raise RuntimeError("构建期间活动 alias 已改变。")
        if base is not None and base.active_count is not None:
            source_index = QdrantIndex(
                self._services.qdrant,
                collection_name=base_collection or "",
                dense_dimension=self._services.pipeline.embedding_dimension,
                pipeline_fingerprint=self._fingerprint,
            )
            source_index.require_registered_snapshot(
                collection_name=base.stored.manifest.collection_name,
                snapshot_name=base.stored.snapshot_name,
                checksum=base.stored.snapshot_checksum,
            )
            if source_index.count_active_exact() != base.active_count:
                raise RuntimeError("构建期间活动 collection 点数已改变。")
            self._require_base_state_sources(base.stored, base.sources)

    def _published_result(
        self,
        job: Job,
        index: QdrantIndex,
    ) -> JobRunResult | None:
        """识别并校验同一任务已经完成发布的重入状态。

        只有 alias、活动 manifest、物理 collection 和独立状态库全部对应
        当前 target 时才返回成功摘要。

        Args:
            job: 当前控制任务。
            index: 当前任务的 target collection。

        Returns:
            已发布任务的成功摘要；target 尚未发布时返回 None。

        Raises:
            ValueError: collection 契约或 staging 身份不一致。
            RuntimeError: manifest 与独立状态库的来源列表不一致。

        """
        active = self._services.manifests.get_active()
        if (
            active is None
            or active.manifest.collection_name != index.collection_name
            or index.alias_target(self._config.alias_name)
            != index.collection_name
        ):
            return None
        index.require_compatible_collection()
        base_digest = index.staging_base_manifest_sha256(
            control_job_id=job.job_id
        )
        state = self._collection_state(index.collection_name)
        state.require_collection_identity(
            control_job_id=job.job_id,
            pipeline_fingerprint=self._fingerprint,
            base_manifest_sha256=base_digest,
        )
        if tuple(
            (source.source_id, source.current_path, source.content_sha256)
            for source in state.list_active_sources()
        ) != tuple(
            (source.source_id, source.current_path, source.content_sha256)
            for source in active.manifest.sources
        ):
            raise RuntimeError("已发布 target 的 manifest 与 state 不一致。")
        return JobRunResult(
            job_id=job.job_id,
            collection_name=index.collection_name,
            source_count=len(active.manifest.sources),
            state=JobState.SUCCEEDED,
            error_code=None,
        )

    def _collection_state(self, collection_name: str) -> StateStore:
        state = StateStore(self._collection_state_path(collection_name))
        state.initialize()
        return state

    def _require_base_state_sources(
        self,
        stored: StoredManifest,
        expected_sources: tuple[ActiveSource, ...],
    ) -> None:
        state = StateStore(
            self._collection_state_path(stored.manifest.collection_name)
        )
        if state.list_active_sources() != expected_sources:
            raise RuntimeError(
                "构建期间活动 collection state 来源列表已改变。"
            )

    def _collection_state_path(self, collection_name: str) -> Path:
        digest = hashlib.sha256(collection_name.encode()).hexdigest()[:24]
        return self._config.index_state_dir / f"index-{digest}.sqlite3"

    def _collection_name(self, job: Job) -> str:
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

    def _finish_owned(
        self,
        job: Job,
        worker_id: str,
        *,
        error_code: str | None,
    ) -> None:
        try:
            self._services.control.finish_job(
                job_id=job.job_id,
                worker_id=worker_id,
                error_code=error_code,
            )
        except (LookupError, sqlite3.Error):
            raise LeaseLostError("LEASE_LOST") from None

    def _mark_lease_lost_if_owned(
        self,
        job: Job,
        worker_id: str,
    ) -> None:
        try:
            self._services.control.finish_job(
                job_id=job.job_id,
                worker_id=worker_id,
                error_code="LEASE_LOST",
            )
        except (LookupError, sqlite3.Error):
            return


def _utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(UTC)
