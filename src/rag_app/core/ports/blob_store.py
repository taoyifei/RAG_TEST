"""同步 Blob Store 端口。"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import Field

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models.common import FrozenModel


class BlobWriteRequest(FrozenModel):
    """带内容摘要的受控二进制写入。"""

    blob_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)
    content: bytes = Field(repr=False)


class BlobReadResult(FrozenModel):
    """读取后仍绑定内容摘要的二进制结果。"""

    blob_id: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    content: bytes = Field(repr=False)


class BlobPutResult(StrEnum):
    """幂等 Blob 写入是否创建了新对象。"""

    CREATED = "created"
    EXISTING = "existing"


class BlobStorePort(Protocol):
    """同步、幂等且不解释文档内容的 Blob Store。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Store 身份。

        Args:
            无参数；读取当前 Store。

        Returns:
            可审计组件描述符。

        """
        ...

    def put_if_absent(self, request: BlobWriteRequest) -> BlobPutResult:
        """按 blob ID 与摘要写入，并区分新建或已存在。

        Args:
            request: blob 身份、摘要、媒体类型和字节。

        Returns:
            CREATED 或 EXISTING。

        """
        ...

    def read(self, blob_id: str) -> BlobReadResult | None:
        """读取一个 blob。

        Args:
            blob_id: 受控 blob 身份。

        Returns:
            找到的结果，否则为 None。

        """
        ...

    def exists(self, blob_id: str) -> bool:
        """判断一个 blob 是否存在。

        Args:
            blob_id: 受控 blob 身份。

        Returns:
            存在时为 True。

        """
        ...

    def delete(self, blob_id: str) -> None:
        """幂等删除一个本阶段尚未提交引用的 blob。

        Args:
            blob_id: 受控 blob 身份。

        Returns:
            无返回值。

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
