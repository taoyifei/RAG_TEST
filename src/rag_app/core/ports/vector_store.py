"""显式 named-vector 的同步 Vector Store 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models import (
    IndexRevisionRef,
    NamedVectorPoint,
    RevisionVectorSpec,
    SearchHit,
    VectorRevisionValidation,
    VectorSearchRequest,
    VectorSearchResult,
    VectorWriteRequest,
)


class VectorStorePort(Protocol):
    """按 revision/slot 隔离写查并可幂等关闭的 Store。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Store 身份。

        Args:
            无参数；读取当前 Store。

        Returns:
            可审计组件描述符。

        """
        ...

    def write(self, request: VectorWriteRequest) -> None:
        """幂等写入一个显式 slot 的向量。

        Args:
            request: revision、slot、vector name、chunks 与向量。

        Returns:
            无返回值。

        """
        ...

    def create_revision(self, spec: RevisionVectorSpec) -> None:
        """创建不可变 named-vector namespace。

        Args:
            spec: revision 与全部 required slot schema。

        Returns:
            无返回值。

        """
        ...

    def upsert_complete_points(
        self,
        spec: RevisionVectorSpec,
        points: tuple[NamedVectorPoint, ...],
    ) -> None:
        """一次写入每个 Point 的全部 required named vectors。

        Args:
            spec: 不可变 revision schema。
            points: 完整 Point 序列。

        Returns:
            无返回值。

        """
        ...

    def fetch_points(
        self,
        spec: RevisionVectorSpec,
        point_ids: tuple[str, ...],
    ) -> tuple[NamedVectorPoint, ...]:
        """带全部向量回读 Point。

        Args:
            spec: 目标 revision schema。
            point_ids: 稳定 UUIDv5 Point IDs。

        Returns:
            仅含匹配 revision 的 Point。

        """
        ...

    def search_named(
        self,
        spec: RevisionVectorSpec,
        *,
        slot_id: str,
        vector_name: str,
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[VectorSearchResult, ...]:
        """拒绝 slot/vector name 交叉并执行精确空间查询。

        Args:
            spec: 目标 revision schema。
            slot_id: 查询 slot。
            vector_name: 查询 named vector。
            query_vector: 与 slot 同维度的向量。
            limit: 最大命中数。

        Returns:
            分数降序且稳定 tie-break 的命中。

        """
        ...

    def count_vectors(self, spec: RevisionVectorSpec, vector_name: str) -> int:
        """从实际 Store 统计一个 named vector。

        Args:
            spec: 目标 revision schema。
            vector_name: 必须属于 spec 的 named vector。

        Returns:
            有效向量数量。

        """
        ...

    def validate_vector_revision(
        self,
        spec: RevisionVectorSpec,
    ) -> VectorRevisionValidation:
        """回读并校验完整 Point、维度和 payload 身份。

        Args:
            spec: 目标 revision schema。

        Returns:
            实际存储证据。

        """
        ...

    def delete_revision(self, spec: RevisionVectorSpec) -> None:
        """仅供已验证 GC Plan 删除整个不可变 namespace。

        Args:
            spec: 待删除 revision schema。

        Returns:
            无返回值。

        """
        ...

    def search(self, request: VectorSearchRequest) -> tuple[SearchHit, ...]:
        """只搜索请求指定的 slot 和 vector name。

        Args:
            request: 显式向量空间和查询向量。

        Returns:
            Store 无关的有序命中。

        """
        ...

    def validate_revision(self, revision: IndexRevisionRef) -> None:
        """失败关闭地验证 revision 身份。

        Args:
            revision: 待验证 revision。

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
