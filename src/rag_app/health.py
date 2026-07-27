"""进程存活与外部依赖就绪状态聚合。"""

from __future__ import annotations

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
        """执行有界只读健康检查。"""
        ...


class _FreezableConfiguration(Protocol):
    """具有冻结状态的配置对象。"""

    @property
    def status(self) -> object:
        """返回 provisional 或 frozen 状态。"""
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
        """仅当状态明确为 frozen 时返回 ready。"""
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
        """读取 collection 清单，不创建或修改服务端对象。"""
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
        """只在 alias 与活动 manifest 双向一致时就绪。"""
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
        """逐端点执行有界只读检查。"""
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
    """只有全部必需组件满足策略时才报告 ready。"""

    def __init__(self, probes: tuple[HealthProbe, ...]) -> None:
        """冻结必需组件探针。

        Args:
            probes: 至少一个有界只读探针。

        Raises:
            ValueError: 没有配置必需组件。

        """
        if not probes:
            raise ValueError("至少配置一个 readiness 探针。")
        self._probes = probes

    def check(self) -> ReadinessReport:
        """依次执行探针并汇总严格 AND 结果。"""
        components = tuple(probe.check() for probe in self._probes)
        return ReadinessReport(
            ready=all(component.ready for component in components),
            components=components,
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
