"""Artifact catalog 与引用生命周期同步端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_app.core.models import BlobCatalogEntry, BlobReference


class ArtifactCatalogPort(Protocol):
    """以引用表为权威并保护共享物理 Blob。"""

    def stage(self, entry: BlobCatalogEntry) -> None:
        """登记本次已经物理创建或验证的 Blob。

        Args:
            entry: staged catalog 行。

        Returns:
            无返回值。

        """
        ...

    def commit_reference(self, reference: BlobReference) -> None:
        """在事务中新增引用并把对象标为 available。

        Args:
            reference: 对 Artifact 的逻辑引用。

        Returns:
            无返回值。

        """
        ...

    def commit_references(self, references: Sequence[BlobReference]) -> None:
        """在单个事务中提交一个 Parser 结果的全部引用。

        Args:
            references: 本次解析产生的全部逻辑引用。

        Returns:
            无返回值。

        """
        ...

    def reference_count(self, artifact_id: str) -> int:
        """从引用表重算对象引用数。

        Args:
            artifact_id: content-addressed Artifact 对象 ID。

        Returns:
            当前权威引用数。

        """
        ...

    def close(self) -> None:
        """幂等关闭 catalog。

        Args:
            无参数；关闭当前 catalog。

        Returns:
            无返回值。

        """
        ...
