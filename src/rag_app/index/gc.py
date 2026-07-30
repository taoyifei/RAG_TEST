"""保守规划并执行物理索引、state 与 snapshot 垃圾回收。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ApiException

from rag_app.index.qdrant import QdrantIndex
from rag_app.manifest import (
    ManifestRepository,
    ManifestState,
    ReadOnlyManifestRepository,
)
from rag_app.state import Job, JobState, StateStore
from rag_app.state.jobs import ReadOnlyJobStore
from rag_app.state.models import CollectionStateIdentity

__all__ = [
    "GarbageApplyReport",
    "GarbageApplyResult",
    "GarbageCollectionPlan",
    "GarbageCollectorConfig",
    "GarbageItemKind",
    "GarbagePlanItem",
    "IndexGarbageCollector",
]

_RETIRED_REVISIONS_TO_KEEP = 2


class GarbageItemKind(StrEnum):
    """可回收对象类型。"""

    SNAPSHOT = "snapshot"
    COLLECTION = "collection"
    STATE = "state"


@dataclass(frozen=True, slots=True)
class GarbagePlanItem:
    """不含正文或本地路径的单个回收计划项。"""

    kind: GarbageItemKind
    collection_name: str
    reason: str
    snapshot_name: str | None = None
    collection_identity: CollectionStateIdentity | None = None
    state_identity: CollectionStateIdentity | None = None

    @property
    def stable_id(self) -> str:
        """返回可重复输出的非敏感计划标识。

        Args:
            无参数。

        Returns:
            由对象类型和 Qdrant 安全名称组成的稳定标识。

        """
        if self.kind == GarbageItemKind.SNAPSHOT:
            return (
                f"snapshot:{self.collection_name}:{self.snapshot_name or ''}"
            )
        return f"{self.kind.value}:{self.collection_name}"


@dataclass(frozen=True, slots=True)
class _ControlSnapshot:
    alias_target: str | None
    manifests: tuple[tuple[str, str, str, str], ...]
    snapshot_references: tuple[tuple[str, str], ...]
    jobs: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class _StateSnapshot:
    present: bool
    quiescent: bool
    identity: CollectionStateIdentity | None


@dataclass(frozen=True, slots=True)
class GarbageCollectionPlan:
    """一次 dry-run 生成的稳定计划及控制面快照。"""

    items: tuple[GarbagePlanItem, ...]
    control_snapshot: _ControlSnapshot


@dataclass(frozen=True, slots=True)
class GarbageApplyResult:
    """一个计划项的实际执行结果。"""

    stable_id: str
    reason: str
    status: str


@dataclass(frozen=True, slots=True)
class GarbageApplyReport:
    """显式 apply 的逐项真实结果。"""

    results: tuple[GarbageApplyResult, ...]


@dataclass(frozen=True, slots=True)
class GarbageCollectorConfig:
    """索引垃圾回收使用的当前部署契约。"""

    alias_name: str
    index_state_dir: Path
    collection_prefix: str
    dense_dimension: int
    pipeline_fingerprint: str
    index_revision: str


class IndexGarbageCollector:
    """只清理可证明不再被活动、回滚或任务引用的索引对象。"""

    def __init__(
        self,
        *,
        client: QdrantClient,
        manifests: ManifestRepository | ReadOnlyManifestRepository,
        control: StateStore | ReadOnlyJobStore,
        config: GarbageCollectorConfig,
    ) -> None:
        """冻结 GC 所需的控制面与当前索引契约。

        Args:
            client: 仅面向内部 Qdrant 的已鉴权客户端。
            manifests: 当前和历史 manifest 仓库。
            control: 管理 API 使用的控制任务库。
            config: 当前 alias、state 路径与索引版本契约。

        Returns:
            无返回值。

        """
        if not config.collection_prefix:
            raise ValueError("collection_prefix 不能为空。")
        self._client = client
        self._manifests = manifests
        self._control = control
        self._config = config

    def plan(self) -> GarbageCollectionPlan:
        """生成零副作用的稳定 dry-run 计划。

        Args:
            无参数。

        Returns:
            已绑定 alias、manifest 和 job 快照的排序计划。

        Raises:
            RuntimeError: 任一 pending 或 running 控制任务存在。

        """
        control_snapshot = self._control_snapshot()
        jobs = self._control.list_jobs()
        self._require_no_live_jobs(jobs)
        stored_manifests = self._manifests.list_all()
        retired = sorted(
            (
                stored
                for stored in stored_manifests
                if stored.state == ManifestState.RETIRED
            ),
            key=lambda stored: (
                stored.manifest.created_at,
                stored.manifest.collection_name,
            ),
            reverse=True,
        )
        kept_retired = {
            stored.manifest.collection_name
            for stored in retired[:_RETIRED_REVISIONS_TO_KEEP]
        }
        expired_retired = {
            stored.manifest.collection_name
            for stored in retired[_RETIRED_REVISIONS_TO_KEEP:]
        }
        manifest_protected = {
            stored.manifest.collection_name
            for stored in stored_manifests
            if stored.state in {
                ManifestState.ACTIVE,
                ManifestState.STAGING,
            }
        } | kept_retired
        protected = set(manifest_protected)
        if control_snapshot.alias_target is not None:
            protected.add(control_snapshot.alias_target)
        jobs_by_id = {job.job_id: job for job in jobs}
        for job in jobs:
            if job.state != JobState.FAILED:
                protected.add(self._target_name(job))
        items: list[GarbagePlanItem] = []
        candidate_collections: set[str] = set()
        for collection_name in sorted(self._collection_names()):
            candidate = self._collection_candidate(
                collection_name,
                protected=protected,
                expired_retired=expired_retired,
                jobs_by_id=jobs_by_id,
            )
            if candidate is None:
                continue
            reason, collection_identity = candidate
            state_snapshot = self._state_snapshot(collection_name)
            if not state_snapshot.quiescent:
                continue
            if (
                state_snapshot.identity is not None
                and state_snapshot.identity != collection_identity
            ):
                raise RuntimeError("GC_STATE_IDENTITY_MISMATCH")
            candidate_collections.add(collection_name)
            items.append(
                GarbagePlanItem(
                    kind=GarbageItemKind.COLLECTION,
                    collection_name=collection_name,
                    reason=reason,
                    collection_identity=collection_identity,
                    state_identity=state_snapshot.identity,
                )
            )
            if state_snapshot.present:
                items.append(
                    GarbagePlanItem(
                        kind=GarbageItemKind.STATE,
                        collection_name=collection_name,
                        reason=reason,
                        collection_identity=collection_identity,
                        state_identity=state_snapshot.identity,
                    )
                )
        references = self._manifests.snapshot_references()
        for collection_name in sorted(manifest_protected):
            if (
                collection_name in candidate_collections
                or not self._client.collection_exists(collection_name)
            ):
                continue
            items.extend(
                GarbagePlanItem(
                    kind=GarbageItemKind.SNAPSHOT,
                    collection_name=collection_name,
                    snapshot_name=snapshot.name,
                    reason="unreferenced_snapshot",
                )
                for snapshot in self._client.list_snapshots(collection_name)
                if (collection_name, snapshot.name) not in references
            )
        return GarbageCollectionPlan(
            items=tuple(sorted(items, key=_plan_sort_key)),
            control_snapshot=control_snapshot,
        )

    def apply(self, plan: GarbageCollectionPlan) -> GarbageApplyReport:
        """在每项前后复核控制面后执行显式 GC 计划。

        Args:
            plan: 由同一 collector 的 dry-run 生成的稳定计划。

        Returns:
            每个对象的 deleted、already_absent 或稳定失败状态。

        Raises:
            RuntimeError: alias、manifest 或 job 在 apply 前后发生漂移。

        """
        self._require_control_unchanged(plan.control_snapshot)
        results: list[GarbageApplyResult] = []
        for item in plan.items:
            self._require_control_unchanged(plan.control_snapshot)
            try:
                status = self._apply_item(item)
            except (ApiException, OSError):
                status = "delete_failed"
            results.append(
                GarbageApplyResult(
                    stable_id=item.stable_id,
                    reason=item.reason,
                    status=status,
                )
            )
            self._require_control_unchanged(plan.control_snapshot)
        return GarbageApplyReport(results=tuple(results))

    def _collection_candidate(
        self,
        collection_name: str,
        *,
        protected: set[str],
        expired_retired: set[str],
        jobs_by_id: dict[str, Job],
    ) -> tuple[str, CollectionStateIdentity] | None:
        if collection_name in protected:
            return None
        identity = self._collection_identity(collection_name)
        if identity is None:
            return None
        if collection_name in expired_retired:
            return "retired_beyond_rollback_window", identity
        if not collection_name.startswith(
            f"{self._config.collection_prefix}-"
        ):
            return None
        reason = self._managed_staging_reason(
            collection_name,
            identity=identity,
            jobs_by_id=jobs_by_id,
        )
        if reason is None:
            return None
        return reason, identity

    def _managed_staging_reason(
        self,
        collection_name: str,
        *,
        identity: CollectionStateIdentity,
        jobs_by_id: dict[str, Job],
    ) -> str | None:
        control_job_id = identity.control_job_id
        if collection_name != self._target_name_from_id(
            control_job_id
        ):
            return None
        job = jobs_by_id.get(control_job_id)
        if job is None:
            return "orphan_control_job_missing"
        if job.state == JobState.FAILED:
            return "failed_job_terminal"
        return None

    def _state_snapshot(self, collection_name: str) -> _StateSnapshot:
        paths = self._state_paths(collection_name)
        present: list[Path] = []
        for path in paths:
            if path.is_symlink():
                raise RuntimeError("GC_UNSAFE_STATE_SYMLINK")
            if path.exists():
                if not path.is_file():
                    raise RuntimeError("GC_UNSAFE_STATE_FILE")
                present.append(path)
        if not present:
            return _StateSnapshot(
                present=False,
                quiescent=True,
                identity=None,
            )
        if paths[0] not in present:
            raise RuntimeError("GC_STATE_MAIN_MISSING")
        state = ReadOnlyJobStore(paths[0])
        state.require_integrity()
        identity = state.collection_identity()
        jobs = state.list_jobs()
        return _StateSnapshot(
            present=True,
            quiescent=all(
                job.state not in {JobState.PENDING, JobState.RUNNING}
                for job in jobs
            ),
            identity=identity,
        )

    def _apply_item(self, item: GarbagePlanItem) -> str:
        if item.kind == GarbageItemKind.COLLECTION:
            return self._apply_collection(item)
        if item.kind == GarbageItemKind.SNAPSHOT:
            return self._apply_snapshot(item)
        return self._apply_state(item)

    def _apply_collection(self, item: GarbagePlanItem) -> str:
        if not self._client.collection_exists(item.collection_name):
            return "already_absent"
        if (
            item.collection_identity is None
            or self._collection_identity(item.collection_name)
            != item.collection_identity
        ):
            return "identity_changed"
        try:
            state_snapshot = self._state_snapshot(item.collection_name)
        except RuntimeError as error:
            if str(error).startswith("GC_UNSAFE_STATE"):
                return "unsafe_state"
            return "identity_changed"
        if (
            state_snapshot.identity != item.state_identity
            or state_snapshot.present != (item.state_identity is not None)
            or not state_snapshot.quiescent
        ):
            return "identity_changed"
        deleted = self._client.delete_collection(item.collection_name)
        return "deleted" if deleted else "delete_failed"

    def _apply_snapshot(self, item: GarbagePlanItem) -> str:
        if not self._client.collection_exists(item.collection_name):
            return "already_absent"
        snapshot_name = item.snapshot_name or ""
        existing = {
            snapshot.name
            for snapshot in self._client.list_snapshots(item.collection_name)
        }
        if snapshot_name not in existing:
            return "already_absent"
        snapshot_deleted = self._client.delete_snapshot(
            item.collection_name,
            snapshot_name,
            wait=True,
        )
        return (
            "deleted"
            if snapshot_deleted is not False
            else "delete_failed"
        )

    def _apply_state(self, item: GarbagePlanItem) -> str:
        if self._client.collection_exists(item.collection_name):
            return "still_referenced"
        try:
            state_snapshot = self._state_snapshot(item.collection_name)
        except RuntimeError as error:
            if str(error).startswith("GC_UNSAFE_STATE"):
                return "unsafe_state"
            return "identity_changed"
        if not state_snapshot.present:
            return "already_absent"
        if (
            item.state_identity is None
            or state_snapshot.identity != item.state_identity
            or not state_snapshot.quiescent
        ):
            return "identity_changed"
        failed = False
        for path in (
            self._state_paths(item.collection_name)[1],
            self._state_paths(item.collection_name)[2],
            self._state_paths(item.collection_name)[0],
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                failed = True
        remaining = any(
            path.exists() or path.is_symlink()
            for path in self._state_paths(item.collection_name)
        )
        return "delete_failed" if failed or remaining else "deleted"

    def _require_control_unchanged(
        self,
        expected: _ControlSnapshot,
    ) -> None:
        if self._control_snapshot() != expected:
            raise RuntimeError("GC_CONTROL_DRIFT")

    def _control_snapshot(self) -> _ControlSnapshot:
        return _ControlSnapshot(
            alias_target=self._alias_target(),
            manifests=tuple(
                (
                    stored.manifest.collection_name,
                    stored.state.value,
                    stored.manifest_sha256,
                    stored.snapshot_name,
                )
                for stored in self._manifests.list_all()
            ),
            snapshot_references=tuple(
                sorted(self._manifests.snapshot_references())
            ),
            jobs=tuple(
                (
                    job.job_id,
                    job.state.value,
                    job.pipeline_fingerprint,
                )
                for job in self._control.list_jobs()
            ),
        )

    def _require_no_live_jobs(self, jobs: tuple[Job, ...]) -> None:
        if any(
            job.state in {JobState.PENDING, JobState.RUNNING}
            for job in jobs
        ):
            raise RuntimeError("存在 pending 或 running 任务，拒绝 index GC。")

    def _alias_target(self) -> str | None:
        return next(
            (
                item.collection_name
                for item in self._client.get_aliases().aliases
                if item.alias_name == self._config.alias_name
            ),
            None,
        )

    def _collection_names(self) -> set[str]:
        return {
            item.name for item in self._client.get_collections().collections
        }

    def _index(self, collection_name: str) -> QdrantIndex:
        return QdrantIndex(
            self._client,
            collection_name=collection_name,
            dense_dimension=self._config.dense_dimension,
            pipeline_fingerprint=self._config.pipeline_fingerprint,
            index_revision=self._config.index_revision,
        )

    def _collection_identity(
        self,
        collection_name: str,
    ) -> CollectionStateIdentity | None:
        index = self._index(collection_name)
        try:
            index.require_compatible_collection()
            control_job_id, base_manifest_sha256 = (
                index.staging_identity()
            )
        except (LookupError, RuntimeError, ValueError):
            return None
        return CollectionStateIdentity(
            control_job_id=control_job_id,
            pipeline_fingerprint=self._config.pipeline_fingerprint,
            base_manifest_sha256=base_manifest_sha256,
        )

    def _target_name(self, job: Job) -> str:
        return self._target_name_from_id(
            job.job_id,
            pipeline_fingerprint=job.pipeline_fingerprint,
        )

    def _target_name_from_id(
        self,
        job_id: str,
        *,
        pipeline_fingerprint: str | None = None,
    ) -> str:
        fingerprint = (
            pipeline_fingerprint or self._config.pipeline_fingerprint
        )
        return (
            f"{self._config.collection_prefix}-"
            f"{fingerprint.removeprefix('sha256:')[:12]}-"
            f"{job_id.removeprefix('job_')[:12]}"
        )

    def _state_path(self, collection_name: str) -> Path:
        digest = hashlib.sha256(collection_name.encode()).hexdigest()[:24]
        return self._config.index_state_dir / f"index-{digest}.sqlite3"

    def _state_paths(
        self,
        collection_name: str,
    ) -> tuple[Path, Path, Path]:
        main_path = self._state_path(collection_name)
        return (
            main_path,
            Path(f"{main_path}-wal"),
            Path(f"{main_path}-shm"),
        )


def _plan_sort_key(item: GarbagePlanItem) -> tuple[int, str]:
    order = {
        GarbageItemKind.SNAPSHOT: 0,
        GarbageItemKind.COLLECTION: 1,
        GarbageItemKind.STATE: 2,
    }
    return order[item.kind], item.stable_id
