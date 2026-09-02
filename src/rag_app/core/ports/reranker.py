"""同步 Reranker 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentCapabilities, ComponentDescriptor
from rag_app.core.models import ProviderHealth, RerankRequest, RerankResult


class RerankerPort(Protocol):
    """同步批量重排；实现不得用 Embedding 冒充 Reranker。"""

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
        """返回重排能力。

        Args:
            无参数；读取当前 Provider。

        Returns:
            组合阶段能力声明。

        """
        ...

    def rerank(self, request: RerankRequest) -> RerankResult:
        """重排有限候选批次。

        Args:
            request: 查询、候选文本和上限。

        Returns:
            有序分数或显式旁路结果。

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
