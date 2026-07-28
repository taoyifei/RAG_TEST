"""进程存活与外部依赖就绪状态聚合。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
from qdrant_client import QdrantClient

from rag_app.index import QdrantIndex
from rag_app.manifest import ManifestRepository

__all__ = [
    "ComponentStatus",
    "FrozenConfigurationProbe",
    "HealthProbe",
    "HttpEndpointProbe",
    "ManifestAliasProbe",
    "QdrantServiceProbe",
    "ReadinessReport",
    "ReadinessService",
]


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """一个必需组件的非敏感健康摘要。"""

    name: str
    ready: bool
    detail: str
    healthy_endpoints: int
    total_endpoints: int


class HealthProbe(Protocol):
    """必需组件健康探针。"""

    def check(self) -> ComponentStatus:
        """执行有界只读健康检查。

        Args:
            无参数；检查当前探针绑定的组件。

        Returns:
            非敏感组件健康摘要。

        """
        ...


class _FreezableConfiguration(Protocol):
    """具有冻结状态的配置对象。"""

    @property
    def status(self) -> object:
        """返回 provisional 或 frozen 状态。

        Args:
            无参数；读取当前配置状态。

        Returns:
            配置管理状态。

        """
        ...


class FrozenConfigurationProbe:
    """拒绝把尚未由冻结集确定的参数报告为生产就绪。"""

    def __init__(self, configuration: _FreezableConfiguration) -> None:
        """保存待检查配置。

        Args:
            configuration: 具有 `status` 字段的配置。

        """
        self._configuration = configuration

    def check(self) -> ComponentStatus:
        """仅当状态明确为 frozen 时返回 ready。

        Args:
            无参数；检查构造时传入的配置。

        Returns:
            冻结配置健康摘要。

        """
        ready = str(self._configuration.status) == "frozen"
        return ComponentStatus(
            name="retrieval_configuration",
            ready=ready,
            detail=(
                "ready"
                if ready
                else "retrieval parameters are not frozen"
            ),
            healthy_endpoints=1 if ready else 0,
            total_endpoints=1,
        )


class QdrantServiceProbe:
    """通过 Qdrant API key 客户端执行只读可达性检查。"""

    def __init__(self, client: QdrantClient) -> None:
        """保存已配置 API key 与超时的客户端。

        Args:
            client: 项目独立 Qdrant 客户端。

        """
        self._client = client

    def check(self) -> ComponentStatus:
        """读取 collection 清单，不创建或修改服务端对象。

        Args:
            无参数；探测构造时传入的 Qdrant 客户端。

        Returns:
            Qdrant 可达性健康摘要。

        """
        try:
            self._client.get_collections()
        except Exception:
            return ComponentStatus(
                name="qdrant",
                ready=False,
                detail="qdrant unavailable",
                healthy_endpoints=0,
                total_endpoints=1,
            )
        return ComponentStatus(
            name="qdrant",
            ready=True,
            detail="ready",
            healthy_endpoints=1,
            total_endpoints=1,
        )


class ManifestAliasProbe:
    """校验 alias、活动 manifest 与运行时 pipeline 一致。"""

    def __init__(
        self,
        *,
        index: QdrantIndex,
        alias_name: str,
        manifests: ManifestRepository,
        pipeline_fingerprint: str,
    ) -> None:
        """保存索引发布契约。

        Args:
            index: 可读取 alias 的 Qdrant 索引。
            alias_name: 业务活动索引别名。
            manifests: 全局 manifest 历史库。
            pipeline_fingerprint: 当前进程配置指纹。

        """
        self._index = index
        self._alias_name = alias_name
        self._manifests = manifests
        self._pipeline_fingerprint = pipeline_fingerprint

    def check(self) -> ComponentStatus:
        """只在 alias 与活动 manifest 双向一致时就绪。

        Args:
            无参数；检查当前活动 alias 与 manifest。

        Returns:
            活动索引兼容性健康摘要。

        """
        try:
            target = self._index.alias_target(self._alias_name)
            if target is None:
                raise ValueError("missing alias")
            self._manifests.require_compatible(
                collection_name=target,
                pipeline_fingerprint=self._pipeline_fingerprint,
            )
        except (ValueError, LookupError, RuntimeError):
            return ComponentStatus(
                name="active_index",
                ready=False,
                detail="active alias or manifest is incompatible",
                healthy_endpoints=0,
                total_endpoints=1,
            )
        return ComponentStatus(
            name="active_index",
            ready=True,
            detail="ready",
            healthy_endpoints=1,
            total_endpoints=1,
        )


class HttpEndpointProbe:
    """按端点执行 health，并可校验 OpenAI models。"""

    def __init__(
        self,
        *,
        name: str,
        endpoints: tuple[str, ...],
        client: httpx.Client,
        minimum_healthy: int,
        expected_model: str | None = None,
    ) -> None:
        """冻结健康策略。

        Args:
            name: 响应中使用的非敏感组件名。
            endpoints: 固定基础 URL。
            client: 已设置该组件独立超时的 HTTP 客户端。
            minimum_healthy: ready 所需最少健康端点数。
            expected_model: 可选 `/v1/models` 精确 model ID。

        Raises:
            ValueError: 名称、端点或最少健康数无效。

        """
        if not name or not endpoints:
            raise ValueError("健康组件名与端点不能为空。")
        if not 0 < minimum_healthy <= len(endpoints):
            raise ValueError("minimum_healthy 超出端点数量。")
        self._name = name
        self._endpoints = tuple(endpoint.rstrip("/") for endpoint in endpoints)
        self._client = client
        self._minimum_healthy = minimum_healthy
        self._expected_model = expected_model

    def check(self) -> ComponentStatus:
        """逐端点执行有界只读检查。

        Args:
            无参数；检查当前探针的全部模型端点。

        Returns:
            端点健康数量与就绪结论。

        """
        healthy = sum(
            self._check_endpoint(endpoint) for endpoint in self._endpoints
        )
        ready = healthy >= self._minimum_healthy
        return ComponentStatus(
            name=self._name,
            ready=ready,
            detail="ready" if ready else "insufficient healthy endpoints",
            healthy_endpoints=healthy,
            total_endpoints=len(self._endpoints),
        )

    def _check_endpoint(self, endpoint: str) -> bool:
        try:
            health = self._client.get(endpoint + "/health")
            if not health.is_success:
                return False
            if self._expected_model is None:
                return True
            models_response = self._client.get(endpoint + "/v1/models")
            if not models_response.is_success:
                return False
            return _contains_model(
                models_response.json(),
                self._expected_model,
            )
        except (httpx.HTTPError, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """所有必需组件的一次就绪快照。"""

    ready: bool
    components: tuple[ComponentStatus, ...]


class ReadinessService:
    """后台刷新探针，并向请求线程提供有时效的内存快照。"""

    def __init__(
        self,
        probes: tuple[HealthProbe, ...],
        *,
        refresh_interval_seconds: float = 10.0,
        max_staleness_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """冻结必需组件探针。

        Args:
            probes: 至少一个有界只读探针。
            refresh_interval_seconds: 后台固定刷新间隔。
            max_staleness_seconds: 快照允许的最大年龄。
            clock: 可测试的单调时钟。

        Raises:
            ValueError: 没有探针或刷新时间参数无效。

        """
        if not probes:
            raise ValueError("至少配置一个 readiness 探针。")
        if min(
            refresh_interval_seconds,
            max_staleness_seconds,
        ) <= 0:
            raise ValueError("readiness 刷新间隔与时效必须为正数。")
        self._probes = probes
        self._refresh_interval_seconds = refresh_interval_seconds
        self._max_staleness_seconds = max_staleness_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: ReadinessReport | None = None
        self._refreshed_at: float | None = None

    def refresh_once(self) -> None:
        """执行一次有界探针检查并原子替换内存快照。

        Args:
            无参数；刷新当前服务绑定的全部探针。

        Returns:
            无返回值。

        """
        try:
            components = tuple(probe.check() for probe in self._probes)
        except Exception:
            components = (
                ComponentStatus(
                    name="readiness_refresh",
                    ready=False,
                    detail="readiness refresh failed",
                    healthy_endpoints=0,
                    total_endpoints=1,
                ),
            )
        report = ReadinessReport(
            ready=all(component.ready for component in components),
            components=components,
        )
        refreshed_at = self._clock()
        with self._lock:
            self._snapshot = report
            self._refreshed_at = refreshed_at

    def start(self) -> None:
        """同步刷新一次，再启动唯一后台刷新线程。

        Args:
            无参数；启动当前就绪服务。

        Returns:
            无返回值。

        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
        self.refresh_once()
        self._stop.clear()
        thread = threading.Thread(
            target=self._refresh_loop,
            name="rag-readiness-refresh",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def check(self) -> ReadinessReport:
        """只读取线程安全快照，过期或未刷新时严格拒绝。

        Args:
            无参数；读取当前缓存快照。

        Returns:
            当前有效快照或缓存失败报告。

        """
        now = self._clock()
        with self._lock:
            snapshot = self._snapshot
            refreshed_at = self._refreshed_at
        if snapshot is None or refreshed_at is None:
            return _cache_failure("readiness snapshot was never refreshed")
        if now - refreshed_at > self._max_staleness_seconds:
            return _cache_failure("readiness snapshot is stale")
        return snapshot

    def close(self) -> None:
        """停止并 join 后台刷新线程。

        Args:
            无参数；关闭当前就绪服务。

        Returns:
            无返回值。

        """
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._lock:
            self._thread = None

    def is_running(self) -> bool:
        """返回后台刷新线程是否仍存活。

        Args:
            无参数；检查当前后台线程。

        Returns:
            刷新线程存活时为 `True`。

        """
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self._refresh_interval_seconds):
            self.refresh_once()


def _cache_failure(detail: str) -> ReadinessReport:
    return ReadinessReport(
        ready=False,
        components=(
            ComponentStatus(
                name="readiness_cache",
                ready=False,
                detail=detail,
                healthy_endpoints=0,
                total_endpoints=1,
            ),
        ),
    )


def _contains_model(payload: object, expected_model: str) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, list):
        return False
    return any(
        isinstance(item, dict) and item.get("id") == expected_model
        for item in data
    )
