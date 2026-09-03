"""P09 SDK、API 与 P06—P08.5 能力的唯一组合根。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rag_app.adapters.stores import SqliteControlStore, SqliteLifecycleStore
from rag_app.application.durable_jobs import DurableJobRunner
from rag_app.application.lifecycle import LifecycleService
from rag_app.composition.p07_runtime import P07Runtime, build_p07_runtime
from rag_app.composition.profiles import (
    ComponentsProfile,
    RagProfile,
    load_profile,
)
from rag_app.core.events import TraceEvent
from rag_app.core.models import SystemStatus
from rag_app.core.models.common import freeze_json_object
from rag_app.sdk import RagSdk


@dataclass(slots=True)
class P09Runtime:
    """显式拥有 P09 Store、SDK 和底层 P07 runtime。"""

    retrieval_runtime: P07Runtime
    store: SqliteLifecycleStore
    lifecycle: LifecycleService
    sdk: RagSdk
    jobs: DurableJobRunner
    data_dir: Path
    _closed: bool = False

    @property
    def control(self) -> SqliteControlStore:
        """返回 P06 控制面以支持阶段验收。

        Args:
            无参数；读取底层 runtime。

        Returns:
            SQLite 控制面。

        """
        return self.retrieval_runtime.persistence.control

    def close(self) -> None:
        """幂等关闭 SDK 与全部底层资源。

        Args:
            无参数；关闭当前 runtime。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self.sdk.close()

    def __enter__(self) -> P09Runtime:
        """进入资源作用域。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开资源作用域并关闭资源。"""
        del args
        self.close()


def build_p09_runtime(
    profile: str | Path | RagProfile,
    *,
    data_dir: str | Path | None = None,
    max_job_workers: int = 1,
    max_pending_jobs: int = 64,
) -> P09Runtime:
    """构造默认离线并使用持久队列的 P09 runtime。

    Args:
        profile: 严格 Profile 或 JSON 文件路径。
        data_dir: 可选受控数据根覆盖。
        max_job_workers: 单进程 Worker 并发硬上限。
        max_pending_jobs: SQLite 队列 queued/running 总量上限。

    Returns:
        持有 SDK 与 P08.5 检索服务的 runtime。

    """
    resolved_data_dir = Path(data_dir or ".data").resolve()
    requested = (
        profile if isinstance(profile, RagProfile) else load_profile(profile)
    )
    retrieval_runtime = build_p07_runtime(
        _persistent_profile(requested), data_dir=resolved_data_dir
    )
    persistence = retrieval_runtime.persistence
    store = SqliteLifecycleStore(
        persistence.connections, max_pending_jobs=max_pending_jobs
    )
    components = persistence.components
    lifecycle = LifecycleService(
        store=store,
        control=persistence.control,
        builder=persistence.builder,
        blob_store=components.blob_store,
        profile_id=components.profile.profile_id,
        index_fingerprint=components.index_fingerprint,
        budgets=persistence.default_budgets(),
    )

    def _system_status() -> SystemStatus:
        integrity, pending_gc, reconciliation = store.system_integrity()
        lexical_schema, analyzer_id, reindex_required = store.lexical_status()
        return SystemStatus(
            profile_id=components.profile.profile_id,
            index_fingerprint=components.index_fingerprint,
            serving_fingerprint=components.serving_fingerprint,
            lexical_schema=lexical_schema,
            analyzer_id=analyzer_id,
            reindex_required=reindex_required,
            integrity_status=integrity,
            pending_gc_items=pending_gc,
            reconciliation_summary=freeze_json_object(reconciliation),
            remote_dense_confidence_calibrated=False,
            remote_production_profile_ready=False,
            components=tuple(
                freeze_json_object(item.model_dump(mode="json"))
                for item in components.descriptors
            ),
        )

    jobs = DurableJobRunner(
        lifecycle.run_ingestion,
        store.pending_ingestion_jobs,
        max_workers=max_job_workers,
    )

    def _close() -> None:
        jobs.close()
        retrieval_runtime.close()

    def _trace_events(trace_id: str) -> tuple[TraceEvent, ...]:
        reader = cast(
            Callable[[str], tuple[TraceEvent, ...]] | None,
            getattr(components.trace_sink, "events", None),
        )
        return () if reader is None else reader(trace_id)

    sdk = RagSdk(
        lifecycle=lifecycle,
        retrieval=retrieval_runtime.retrieval,
        get_job=store.get_job,
        cancel_job=store.request_job_cancellation,
        retry_job=store.retry_job,
        submit_job=jobs.submit,
        trace_events=_trace_events,
        system_status=_system_status,
        close=_close,
    )
    runtime = P09Runtime(
        retrieval_runtime=retrieval_runtime,
        store=store,
        lifecycle=lifecycle,
        sdk=sdk,
        jobs=jobs,
        data_dir=resolved_data_dir,
    )
    jobs.recover()
    return runtime


def _persistent_profile(profile: RagProfile) -> RagProfile:
    """把离线 legacy 别名显式提升到 P06—P09 持久组件。

    Args:
        profile: 调用方选择的严格 Profile。

    Returns:
        保留 Provider/策略选择的 P09 持久 Profile。

    """
    components = profile.components
    if (
        components.metadata_store == "sqlite-control"
        and components.blob_store == "filesystem-blob"
        and components.lexical_store == "sqlite-fts5"
        and components.vector_store in {"memory-vector", "qdrant-local"}
    ):
        return profile
    if (
        components.embedding_topology != "deterministic-single"
        or components.embedding_primary != "deterministic"
    ):
        raise ValueError(
            "P09 legacy Profile 提升只允许离线 deterministic single。"
        )
    return profile.model_copy(
        update={
            "components": ComponentsProfile(
                parser="docx-ooxml-v4",
                chunker="docx-structural-v3",
                embedding_topology=components.embedding_topology,
                embedding_primary=components.embedding_primary,
                embedding_standby=None,
                embedding_router="embedding-router-single",
                reranker=components.reranker,
                vector_store="memory-vector",
                lexical_store="sqlite-fts5",
                metadata_store="sqlite-control",
                blob_store="filesystem-blob",
                generator=components.generator,
                trace_sink=components.trace_sink,
            )
        }
    )


__all__ = ["P09Runtime", "build_p09_runtime"]
