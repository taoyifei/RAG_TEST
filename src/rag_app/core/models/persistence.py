"""P06 持久化、Artifact catalog 和 Embedding cache 公共模型。"""

from __future__ import annotations

import hashlib
import math
import struct
from enum import StrEnum
from typing import Self

from pydantic import Field, StrictInt, field_validator, model_validator

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.provider import (
    EmbeddingRequestRole,
    EmbeddingSlotIdentity,
)


class CacheScope(StrEnum):
    """持久化 Embedding cache 的隔离边界。"""

    KNOWLEDGE_BASE = "knowledge_base"
    PROJECT = "project"
    GLOBAL = "global"


class BlobPhysicalState(StrEnum):
    """Blob 物理对象在 catalog 中的可见状态。"""

    STAGED = "staged"
    AVAILABLE = "available"
    QUARANTINE = "quarantine"


class BlobCatalogEntry(FrozenModel):
    """不暴露绝对路径的 content-addressed Blob catalog 行。"""

    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=0)
    media_type: str = Field(min_length=1)
    physical_state: BlobPhysicalState
    physical_locator: str = Field(
        pattern=r"^blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}$"
    )
    created_by_job_id: str | None = Field(
        default=None,
        pattern=r"^job_[0-9a-f]{32}$",
    )

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        if self.artifact_id != f"sha256:{self.content_sha256}":
            raise ValueError("artifact ID 必须等于 content SHA-256。")
        return self


class BlobReference(FrozenModel):
    """由引用表而非可漂移 ref_count 表达的 Blob 所有权。"""

    reference_id: str = Field(pattern=r"^bref_[0-9a-f]{32}$")
    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    owner_type: str = Field(pattern=r"^(document_version|parsed_media|other)$")
    owner_id: str = Field(min_length=1)
    role: str = Field(pattern=r"^(source_document|embedded_media|other)$")
    revision_id: str | None = Field(
        default=None,
        pattern=r"^irev_[0-9a-f]{32}$",
    )


class BlobPhysicalAudit(FrozenModel):
    """不暴露绝对路径或内容的物理 Blob 盘点项。"""

    blob_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=0)
    reason_code: str = Field(pattern=r"^OK$")


class EmbeddingCacheIdentity(FrozenModel):
    """在 P05.5 语义键外增加合规 scope 的持久化身份。"""

    scope_kind: CacheScope = CacheScope.PROJECT
    scope_id: str = Field(min_length=1)
    slot: EmbeddingSlotIdentity
    role: EmbeddingRequestRole
    role_policy_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def semantic_key(self) -> str:
        """计算包含 P05.5 slot/policy 语义的 cache key。

        Args:
            无参数；读取当前身份。

        Returns:
            不含正文的 SHA-256 身份。

        """
        return canonical_sha256(
            {
                "slot_id": self.slot.slot_id,
                "provider": self.slot.provider_id,
                "model": self.slot.model,
                "dimension": self.slot.dimension,
                "normalization": self.slot.normalization,
                "role": self.role.value,
                "role_policy_identity": self.role_policy_identity,
                "adapter_revision": self.slot.adapter_revision,
                "text_sha256": self.text_sha256,
            }
        )

    @property
    def persistent_key(self) -> str:
        """计算额外绑定 scope 的持久化主键。

        Args:
            无参数；读取当前身份。

        Returns:
            跨重启稳定的 SHA-256 主键。

        """
        return canonical_sha256(
            {
                "scope_kind": self.scope_kind.value,
                "scope_id": self.scope_id,
                "semantic_key": self.semantic_key,
            }
        )


class EmbeddingCacheRecord(FrozenModel):
    """固定 float32 little-endian 编码的 cache 记录。"""

    identity: EmbeddingCacheIdentity
    vector: tuple[float, ...] = Field(repr=False)
    vector_encoding_version: str = Field(
        default="float32-le-v1", pattern=r"^float32-le-v1$"
    )

    @field_validator("vector")
    @classmethod
    def _validate_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not value or any(not math.isfinite(item) for item in value):
            raise ValueError("cache vector 必须非空且只含有限值。")
        if not any(item != 0.0 for item in value):
            raise ValueError("cache vector 禁止全零。")
        return value

    @model_validator(mode="after")
    def _validate_dimension(self) -> Self:
        if len(self.vector) != self.identity.slot.dimension:
            raise ValueError("cache vector 维度与 slot 不一致。")
        return self

    def to_bytes(self) -> bytes:
        """编码为跨平台固定格式。

        Args:
            无参数；读取当前向量。

        Returns:
            float32 little-endian 字节。

        """
        return struct.pack(f"<{len(self.vector)}f", *self.vector)

    @classmethod
    def from_bytes(
        cls,
        identity: EmbeddingCacheIdentity,
        payload: bytes,
    ) -> EmbeddingCacheRecord:
        """校验长度并解码固定格式向量。

        Args:
            identity: cache 与 slot 身份。
            payload: 数据库读取的向量字节。

        Returns:
            已校验的 cache 记录。

        Raises:
            ValueError: 字节长度或向量合同无效。

        """
        expected = identity.slot.dimension * 4
        if len(payload) != expected:
            raise ValueError("cache vector 字节长度与维度不一致。")
        vector = struct.unpack(f"<{identity.slot.dimension}f", payload)
        return cls(identity=identity, vector=vector)


def content_sha256(content: str) -> str:
    """计算正文 cache 身份使用的 SHA-256。

    Args:
        content: 不会被返回或持久化到 cache 表的正文。

    Returns:
        小写十六进制摘要。

    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "BlobCatalogEntry",
    "BlobPhysicalState",
    "BlobPhysicalAudit",
    "BlobReference",
    "CacheScope",
    "EmbeddingCacheIdentity",
    "EmbeddingCacheRecord",
    "content_sha256",
]
