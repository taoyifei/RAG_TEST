"""同步 Embedding 与显式 slot 路由端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentCapabilities, ComponentDescriptor
from rag_app.core.models import (
    EmbeddingCoverage,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingRouteDecision,
    ProviderHealth,
)
from rag_app.core.policies import EgressPolicy


class EmbeddingPort(Protocol):
    """同步批量端口；网络只能由策略授权且实现负责有限重试。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Provider 身份。

        Args:
            无参数；读取当前 Provider。

        Returns:
            可审计组件描述符。

        """
        ...

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回批量、网络、角色和维度能力。

        Args:
            无参数；读取当前 Provider。

        Returns:
            组合阶段能力声明。

        """
        ...

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """按请求 slot 和角色生成向量。

        Args:
            request: 文本批次、slot 和 DOCUMENT/QUERY 角色。

        Returns:
            绑定相同 slot、顺序与策略身份的向量结果。

        """
        ...

    def health(self, *, network: bool = False) -> ProviderHealth:
        """读取健康；默认禁止网络探测。

        Args:
            network: 是否明确允许实际网络探测。

        Returns:
            不含响应正文的健康状态。

        """
        ...


class EmbeddingRouteRequest(Protocol):
    """Router 所需状态的最小只读视图。"""

    @property
    def coverages(self) -> tuple[EmbeddingCoverage, ...]:
        """返回 active revision 的 slot 覆盖。

        Args:
            无参数；读取当前请求视图。

        Returns:
            每个 slot 的覆盖证据。

        """
        ...

    @property
    def egress_policy(self) -> EgressPolicy:
        """返回请求作用域的出网策略。

        Args:
            无参数；读取当前请求视图。

        Returns:
            默认拒绝的出网策略。

        """
        ...


class EmbeddingRouterPort(Protocol):
    """每次请求只选择一个 Dense slot 的同步 Router。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Router 身份。

        Args:
            无参数；读取当前 Router。

        Returns:
            可审计组件描述符。

        """
        ...

    def route(self, request: EmbeddingRouteRequest) -> EmbeddingRouteDecision:
        """在读取正文前完成出网与兼容性选择。

        Args:
            request: 覆盖、circuit 与出网策略视图。

        Returns:
            请求内保持粘性的 slot 决策。

        """
        ...
