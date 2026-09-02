"""同步 Generator 端口。"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from rag_app.core.capabilities import ComponentCapabilities, ComponentDescriptor
from rag_app.core.models import AnswerDraft, EvidenceItem, ProviderHealth
from rag_app.core.models.common import FrozenModel


class GenerationRequest(FrozenModel):
    """格式中立且有明确证据集的生成请求。"""

    query: str = Field(min_length=1, repr=False)
    evidence: tuple[EvidenceItem, ...]
    citation_protocol: str = Field(min_length=1)


class GeneratorPort(Protocol):
    """同步生成端口；网络和 secret 由 adapter/策略边界处理。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Generator 身份。

        Args:
            无参数；读取当前 Generator。

        Returns:
            可审计组件描述符。

        """
        ...

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回生成能力。

        Args:
            无参数；读取当前 Generator。

        Returns:
            组合阶段能力声明。

        """
        ...

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """从显式证据生成回答草稿。

        Args:
            request: 查询、证据与引用协议。

        Returns:
            尚未发布的回答草稿。

        """
        ...

    def health(self, *, network: bool = False) -> ProviderHealth:
        """读取健康；默认禁止网络探测。

        Args:
            network: 是否明确允许实际网络探测。

        Returns:
            不含正文的健康状态。

        """
        ...
