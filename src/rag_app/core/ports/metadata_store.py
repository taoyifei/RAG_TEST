"""同步 Metadata Store 端口。"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models.common import FrozenModel, JsonObject


class MetadataRecord(FrozenModel):
    """不含 SQL 或基础设施对象的元数据记录。"""

    namespace: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    key: str = Field(min_length=1, max_length=256)
    value: JsonObject


class MetadataStorePort(Protocol):
    """同步、幂等且项目作用域由 key 明确表达的元数据 Store。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Store 身份。

        Args:
            无参数；读取当前 Store。

        Returns:
            可审计组件描述符。

        """
        ...

    def put(self, record: MetadataRecord) -> None:
        """幂等写入一条结构化记录。

        Args:
            record: 命名空间、键和值。

        Returns:
            无返回值。

        """
        ...

    def get(self, namespace: str, key: str) -> MetadataRecord | None:
        """读取一条记录。

        Args:
            namespace: 受控命名空间。
            key: 记录键。

        Returns:
            找到的记录，否则为 None。

        """
        ...

    def close(self) -> None:
        """幂等释放 Store 资源。

        Args:
            无参数；关闭当前 Store。

        Returns:
            无返回值。

        """
        ...
